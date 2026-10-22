"""One-time local credential provisioning. Never store the cleartext password."""
from __future__ import annotations

import os
import secrets
from pathlib import Path


DEFAULT_USERNAME = 'admin'


def provision(path: Path) -> dict[str, str] | None:
    from observability.security import hash_password
    if path.exists():
        return None
    password = secrets.token_urlsafe(18)
    read_token = secrets.token_urlsafe(32)
    ingest_token = secrets.token_urlsafe(32)
    key = secrets.token_urlsafe(32)
    value = '\n'.join([
        '# Private local configuration; ignored by git. Do not share this file.',
        'OBSERVABILITY_ENABLED=true',
        'OBSERVABILITY_HOST=127.0.0.1',
        'OBSERVABILITY_PORT=38080',
        'OBSERVABILITY_STORE_DIR=./runtime/observability',
        'OBSERVABILITY_RETENTION_DAYS=30',
        f"OBSERVABILITY_PASSWORD_HASH='{hash_password(password)}'",
        f'OBSERVABILITY_READ_TOKEN={read_token}',
        f'OBSERVABILITY_INGEST_TOKEN={ingest_token}',
        f'OBSERVABILITY_HMAC_KEY={key}',
        '# HTTP is permitted ONLY over a loopback/SSH tunnel. Use true behind HTTPS.',
        'OBSERVABILITY_COOKIE_SECURE=false',
        'OBSERVABILITY_SESSION_TTL_SECONDS=2592000',
        f'OBSERVABILITY_USERNAME={DEFAULT_USERNAME}',
        f'OBSERVABILITY_CONSOLE_TOKEN={secrets.token_urlsafe(32)}',
        f'OBSERVABILITY_CHAT_TOKEN={secrets.token_urlsafe(32)}', '',
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return None
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    return {'password': password, 'read_token': read_token}


def ensure(path: Path) -> list[str]:
    """Append console keys missing from an existing file; the password is never touched."""
    if path.is_symlink() or not path.is_file():
        return []
    text = path.read_text(encoding='utf-8')
    present = {line.split('=', 1)[0].strip() for line in text.splitlines() if '=' in line and not line.lstrip().startswith('#')}
    additions = []
    if 'OBSERVABILITY_USERNAME' not in present:
        additions.append(f'OBSERVABILITY_USERNAME={DEFAULT_USERNAME}')
    if 'OBSERVABILITY_CONSOLE_TOKEN' not in present:
        additions.append(f'OBSERVABILITY_CONSOLE_TOKEN={secrets.token_urlsafe(32)}')
    if 'OBSERVABILITY_CHAT_TOKEN' not in present:
        additions.append(f'OBSERVABILITY_CHAT_TOKEN={secrets.token_urlsafe(32)}')
    if not additions:
        return []
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    with os.fdopen(fd, 'a', encoding='utf-8') as handle:
        handle.write(('' if text.endswith('\n') else '\n') + '\n'.join(additions) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)
    return [line.split('=', 1)[0] for line in additions]


def main(argv: list[str] | None = None) -> None:
    import sys
    args = sys.argv[1:] if argv is None else argv
    path = Path(__file__).resolve().parents[3] / 'conf' / '.env.observability'
    if args[:1] == ['ensure']:
        added = ensure(path)
        print(f"已补充：{', '.join(added)}" if added else f'无需补充：{path}')
        return
    if args:
        print('用法：credentials [ensure]')
        return
    result = provision(path)
    if result is None:
        print(f'凭证文件已存在，未覆盖：{path}')
        return
    print(f'已写入私有配置（权限 0600）：{path}')
    print(f"登录密码（仅显示一次，不保存明文）：{result['password']}")
    print(f"LLM 只读 Token：{result['read_token']}")


if __name__ == '__main__':
    main()
