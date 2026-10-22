"""Single-process, file-backed console appearance, independent of app settings.

The caller supplies ``Path(settings.store_dir) / 'appearance'``. Reads never
repair corrupt storage; callers may supply a public-default fallback themselves.
Pillow is imported only for uploads. Returned icon paths must still be served by
trusted local code (a Path cannot protect against replacement after return).
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import stat
import threading
import unicodedata
import uuid
import warnings
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_NAME = "Ferry"
DEFAULT_HAPTICS_ENABLED = True
# Reserved for packaged assets, independent of settings revisions and uploads.
DEFAULT_REVISION = "00000000000000000000000000000001"
MAX_AVATAR_BYTES = 2097152
MAX_AVATAR_PIXELS = 16_000_000
MAX_AVATAR_DIMENSION = 8192
MAX_ASSET_VERSIONS = 3
ICON_SIZES = {"180": 180, "192": 192, "512": 512, "maskable": 512}

_STATE_VERSION = 1
_MAX_STATE_BYTES = 4096
_STATE_FILE = "state.json"
_REVISION_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)
_MIME_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class AppearanceError(Exception):
    """Safe, structured error for translation by the HTTP layer."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _unavailable() -> AppearanceError:
    return AppearanceError("appearance_unavailable", "Appearance storage is unavailable", 503)


def _invalid_avatar() -> AppearanceError:
    return AppearanceError("invalid_avatar", "Use a valid, non-animated PNG, JPEG or WebP image")


def _name(value: str) -> str:
    error = AppearanceError("invalid_name", "Name must contain 1–32 characters without controls")
    if not isinstance(value, str):
        raise error
    # Check before stripping: even a leading/trailing newline is not acceptable.
    _BIDI = {"\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
             "\u2066", "\u2067", "\u2068", "\u2069"}
    if any(unicodedata.category(char).startswith("C") or char in _BIDI or char in "\u2028\u2029" for char in value):
        raise error
    normalized = unicodedata.normalize("NFC", value).strip()
    if not 1 <= len(normalized) <= 32 or len(normalized.encode("utf-8")) > 128:
        raise error
    return normalized


def _revision(value: str) -> str:
    if not isinstance(value, str) or _REVISION_RE.fullmatch(value) is None:
        raise AppearanceError("invalid_revision", "Invalid icon revision")
    return value


def _icons(revision: str) -> dict[str, str]:
    return {key: f"/static/console-icon-{key}.png?v={revision}" for key in ICON_SIZES}


def _default_state() -> dict:
    return {"version": _STATE_VERSION, "name": DEFAULT_NAME,
            "haptics_enabled": DEFAULT_HAPTICS_ENABLED, "revision": DEFAULT_REVISION,
            "avatar_revision": None}


def _public(state: dict) -> dict:
    return {"name": state["name"], "revision": state["revision"],
            "avatar": state["avatar_revision"] is not None,
            "icons": _icons(state["avatar_revision"] or DEFAULT_REVISION)}


def _snapshot(state: dict) -> dict:
    return {**_public(state), "haptics_enabled": state["haptics_enabled"],
            "defaults": {"name": DEFAULT_NAME, "haptics_enabled": DEFAULT_HAPTICS_ENABLED,
                         "icons": _icons(DEFAULT_REVISION)}}


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate state key")
        result[key] = value
    return result


class AppearanceStore:
    """One instance per process/root; the RLock serializes commits, not decoding.

    Settings revisions change independently from immutable avatar versions.
    ``icon_path(key, None)`` selects current assets; the reserved default revision
    and an avatar-free state return None so the caller serves packaged defaults.
    Pruned/unknown avatar revisions fail with 404, never silently change content.
    """

    def __init__(self, root: Path):
        path = Path(root)
        if ".." in path.parts:
            raise _unavailable()
        self._root = path.absolute()  # Do not resolve away a symlink ancestor.
        if self._root == Path(self._root.anchor):
            raise _unavailable()
        self._lock = threading.RLock()

    @contextmanager
    def _root_directory(self, *, create: bool = False) -> Iterator[int | None]:
        """Walk with no-follow descriptors; never chmod existing ancestors."""
        fd = None
        try:
            fd = os.open(self._root.anchor, _DIRECTORY_FLAGS)
            for part in self._root.parts[1:]:
                try:
                    child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        yield None
                        return
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
            if create:
                os.fchmod(fd, 0o700)
            yield fd
        except OSError:
            raise _unavailable() from None
        finally:
            if fd is not None:
                os.close(fd)

    @contextmanager
    def _directory(self, parent: int, name: str, *, create: bool = False) -> Iterator[int | None]:
        fd = None
        try:
            try:
                fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                try:
                    os.mkdir(name, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
            if create:
                os.fchmod(fd, 0o700)
            yield fd
        finally:
            if fd is not None:
                os.close(fd)

    @staticmethod
    def _read_file(parent: int, name: str, limit: int) -> bytes:
        fd = os.open(name, _READ_FLAGS, dir_fd=parent)
        try:
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                    or metadata.st_mode & 0o077 or not 0 < metadata.st_size <= limit):
                raise _unavailable()
            with os.fdopen(fd, "rb", closefd=False) as file:
                data = file.read(limit + 1)
            if not 0 < len(data) <= limit:
                raise _unavailable()
            return data
        finally:
            os.close(fd)

    def _validate_assets(self, root: int, revision: str) -> None:
        with self._directory(root, "avatars") as avatars:
            if avatars is None:
                raise _unavailable()
            with self._directory(avatars, revision) as version:
                if version is None:
                    raise _unavailable()
                for key in ICON_SIZES:
                    if not self._read_file(version, f"{key}.png", MAX_AVATAR_BYTES).startswith(_PNG_SIGNATURE):
                        raise _unavailable()

    def _read_state(self, root: int | None) -> tuple[dict, bool]:
        if root is None:
            return _default_state(), False
        try:
            data = self._read_file(root, _STATE_FILE, _MAX_STATE_BYTES)
        except FileNotFoundError:
            return _default_state(), False
        try:
            state = json.loads(data, object_pairs_hook=_unique_object)
            if (not isinstance(state, dict) or set(state) != set(_default_state())
                    or type(state["version"]) is not int or state["version"] != _STATE_VERSION
                    or _name(state["name"]) != state["name"]
                    or type(state["haptics_enabled"]) is not bool):
                raise ValueError("Invalid state")
            _revision(state["revision"])
            avatar = state["avatar_revision"]
            if avatar is not None:
                _revision(avatar)
                if avatar == DEFAULT_REVISION:
                    raise ValueError("Reserved revision")
                self._validate_assets(root, avatar)
        except (ValueError, TypeError, UnicodeError, RecursionError, AppearanceError):
            raise _unavailable() from None
        return state, True

    def public(self) -> dict:
        with self._lock, self._root_directory() as root:
            return _public(self._read_state(root)[0])

    def snapshot(self) -> dict:
        with self._lock, self._root_directory() as root:
            return _snapshot(self._read_state(root)[0])

    @staticmethod
    def _write_file(parent: int, name: str, payload: bytes) -> None:
        # A colliding file is never truncated or removed.
        fd = os.open(name, _WRITE_FLAGS, 0o600, dir_fd=parent)
        try:
            os.fchmod(fd, 0o600)
            pending = memoryview(payload)
            while pending:
                count = os.write(fd, pending)
                if count <= 0:
                    raise OSError("Incomplete appearance write")
                pending = pending[count:]
            os.fsync(fd)
            metadata = os.fstat(fd)
            target = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (metadata.st_nlink != 1 or not stat.S_ISREG(target.st_mode)
                    or (metadata.st_dev, metadata.st_ino) != (target.st_dev, target.st_ino)):
                raise _unavailable()
        except BaseException:
            os.unlink(name, dir_fd=parent)
            raise
        finally:
            os.close(fd)

    @staticmethod
    def _encode_state(state: dict) -> bytes:
        return json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _restore_state(self, root: int, old: dict, existed: bool) -> None:
        """Best-effort rollback after a replace succeeded but directory fsync failed."""
        temporary = f".state-{uuid.uuid4().hex}.tmp"
        written = False
        try:
            if existed:
                self._write_file(root, temporary, self._encode_state(old))
                written = True
                os.replace(temporary, _STATE_FILE, src_dir_fd=root, dst_dir_fd=root)
                written = False
            else:
                os.unlink(_STATE_FILE, dir_fd=root)
            os.fsync(root)
        except (OSError, AppearanceError):
            # Do not delete new assets if the new pointer could remain committed.
            pass
        finally:
            if written:
                os.unlink(temporary, dir_fd=root)

    def _commit(self, root: int, old: dict, existed: bool, new: dict) -> None:
        temporary = f".state-{uuid.uuid4().hex}.tmp"
        written = False
        replaced = False
        try:
            self._write_file(root, temporary, self._encode_state(new))
            written = True
            # Recheck before replacing, including hardlinks and malformed state.
            current, current_exists = self._read_state(root)
            if current != old or current_exists != existed:
                raise _unavailable()
            os.replace(temporary, _STATE_FILE, src_dir_fd=root, dst_dir_fd=root)
            written = False
            replaced = True
            os.fsync(root)
        except OSError:
            if replaced:
                self._restore_state(root, old, existed)
            raise _unavailable() from None
        finally:
            if written:
                os.unlink(temporary, dir_fd=root)

    def _change(self, **changes: object) -> dict:
        with self._lock, self._root_directory() as root:
            old, existed = self._read_state(root)
            new = {**old, **changes}
            if new == old:
                return _snapshot(old)
            new["revision"] = uuid.uuid4().hex
            with self._root_directory(create=True) as writable:
                self._commit(writable, old, existed, new)
                self._prune(writable, new["avatar_revision"])
            return _snapshot(new)

    def update(self, name: str | None = None, haptics_enabled: bool | None = None) -> dict:
        changes = {}
        if name is not None:
            changes["name"] = _name(name)
        if haptics_enabled is not None:
            if type(haptics_enabled) is not bool:
                raise AppearanceError("invalid_haptics", "Haptics setting must be a boolean")
            changes["haptics_enabled"] = haptics_enabled
        return self._change(**changes)

    def reset(self) -> dict:
        return self._change(name=DEFAULT_NAME, haptics_enabled=DEFAULT_HAPTICS_ENABLED,
                            avatar_revision=None)

    def reset_avatar(self) -> dict:
        return self._change(avatar_revision=None)

    async def set_avatar(self, chunks: AsyncIterator[bytes], content_type: str) -> dict:
        if not isinstance(content_type, str) or content_type not in _MIME_FORMATS:
            raise AppearanceError("unsupported_avatar_type", "Use PNG, JPEG or WebP", 415)
        data = bytearray()
        try:
            async for chunk in chunks:
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise _invalid_avatar()
                view = memoryview(chunk)
                if len(data) + view.nbytes > MAX_AVATAR_BYTES:
                    raise AppearanceError("avatar_too_large", "Avatar must not exceed 2 MiB", 413)
                data.extend(view.tobytes())
        except (OSError, TypeError, ValueError):
            raise _invalid_avatar() from None
        if not data:
            raise _invalid_avatar()
        icons = await asyncio.to_thread(self._render_icons, bytes(data), _MIME_FORMATS[content_type])
        # Only avatar is applied after decoding; concurrent settings are reread.
        return await asyncio.to_thread(self._install_avatar, icons)

    @staticmethod
    def _render_icons(data: bytes, expected_format: str) -> dict[str, bytes]:
        try:
            from PIL import Image, ImageOps
        except ImportError:
            raise AppearanceError("image_processing_unavailable", "Image processing is unavailable", 503) from None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data), formats=list(_MIME_FORMATS.values())) as image:
                    width, height = image.size
                    if (image.format != expected_format or getattr(image, "is_animated", False)
                            or getattr(image, "n_frames", 1) != 1
                            or not 0 < width <= MAX_AVATAR_DIMENSION
                            or not 0 < height <= MAX_AVATAR_DIMENSION
                            or width * height > MAX_AVATAR_PIXELS):
                        raise _invalid_avatar()
                    image.verify()
                with Image.open(io.BytesIO(data), formats=[expected_format]) as image:
                    image.load()
                    oriented = ImageOps.exif_transpose(image)
                    rgba = oriented.convert("RGBA")
                    square = ImageOps.fit(rgba, (512, 512), method=Image.Resampling.LANCZOS,
                                          centering=(0.5, 0.5))
                icons = {}
                for key, size in ICON_SIZES.items():
                    if key == "maskable":
                        # The entire 280px square fits in the central 80% circle.
                        output = Image.new("RGB", (512, 512), "white")
                        content = square.resize((280, 280), Image.Resampling.LANCZOS)
                        output.paste(content, (116, 116), content)
                    else:
                        output = Image.new("RGBA", (size, size))
                        output.paste(square.resize((size, size), Image.Resampling.LANCZOS), (0, 0))
                    # Fresh canvases carry no source EXIF, ICC, text or other metadata.
                    buffer = io.BytesIO()
                    output.save(buffer, format="PNG")
                    icons[key] = buffer.getvalue()
                return icons
        except AppearanceError:
            raise
        except Exception:
            # Pillow errors often include source metadata; none escapes this API.
            raise _invalid_avatar() from None

    def _install_avatar(self, icons: dict[str, bytes]) -> dict:
        with self._lock, self._root_directory(create=True) as root:
            old, existed = self._read_state(root)
            with self._directory(root, "avatars", create=True) as avatars:
                revision = uuid.uuid4().hex
                # Exclusive directory creation: collisions must not touch old files.
                os.mkdir(revision, 0o700, dir_fd=avatars)
                try:
                    with self._directory(avatars, revision, create=True) as version:
                        for key in ICON_SIZES:
                            self._write_file(version, f"{key}.png", icons[key])
                        os.fsync(version)
                    os.fsync(avatars)
                    new = {**old, "revision": uuid.uuid4().hex, "avatar_revision": revision}
                    self._commit(root, old, existed, new)
                except BaseException:
                    try:
                        current, _ = self._read_state(root)
                        uncommitted = current["avatar_revision"] != revision
                    except (OSError, AppearanceError):
                        uncommitted = False
                    if uncommitted:
                        self._remove_version(avatars, revision)
                    raise
            self._prune(root, revision)
            return _snapshot(new)

    def _remove_version(self, avatars: int, revision: str) -> bool:
        """Only remove recognized, regular, unlinked asset files; never follow links."""
        try:
            with self._directory(avatars, revision) as version:
                if version is None:
                    return False
                names = os.listdir(version)
                if not set(names) <= {f"{key}.png" for key in ICON_SIZES}:
                    return False
                for name in names:
                    metadata = os.stat(name, dir_fd=version, follow_symlinks=False)
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                        return False
                for name in names:
                    os.unlink(name, dir_fd=version)
            os.rmdir(revision, dir_fd=avatars)
            return True
        except (OSError, AppearanceError):
            return False

    def _prune(self, root: int, active: str | None) -> None:
        # Maintenance failure must not turn an already committed write into error.
        # Unexpected/tampered entries are preserved, never recursively deleted.
        try:
            with self._directory(root, "avatars") as avatars:
                if avatars is None:
                    return
                versions = []
                for name in os.listdir(avatars):
                    if name == active or _REVISION_RE.fullmatch(name) is None or name == DEFAULT_REVISION:
                        continue
                    metadata = os.stat(name, dir_fd=avatars, follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        versions.append((metadata.st_mtime_ns, name))
                versions.sort(reverse=True)
                retained = MAX_ASSET_VERSIONS - (1 if active is not None else 0)
                for _, name in versions[retained:]:
                    self._remove_version(avatars, name)
                os.fsync(avatars)
        except (OSError, AppearanceError):
            pass

    def icon_path(self, sizekey: str, revision: str | None = None) -> Path | None:
        if not isinstance(sizekey, str) or sizekey not in ICON_SIZES:
            raise AppearanceError("invalid_icon", "Invalid icon size")
        if revision is not None:
            _revision(revision)
        with self._lock, self._root_directory() as root:
            state, _ = self._read_state(root)
            asset = state["avatar_revision"] if revision is None else revision
            if asset is None or asset == DEFAULT_REVISION:
                return None
            if root is None:
                raise AppearanceError("icon_not_found", "Icon version is unavailable", 404)
            with self._directory(root, "avatars") as avatars:
                if avatars is None:
                    raise AppearanceError("icon_not_found", "Icon version is unavailable", 404)
                with self._directory(avatars, asset) as version:
                    if version is None:
                        raise AppearanceError("icon_not_found", "Icon version is unavailable", 404)
                    try:
                        data = self._read_file(version, f"{sizekey}.png", MAX_AVATAR_BYTES)
                    except FileNotFoundError:
                        raise _unavailable() from None
                    if not data.startswith(_PNG_SIGNATURE):
                        raise _unavailable()
            return self._root / "avatars" / asset / f"{sizekey}.png"
