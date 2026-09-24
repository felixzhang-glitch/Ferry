"""One-time local credential provisioning. Never store the cleartext password."""
from __future__ import annotations

import os
import secrets
from pathlib import Path


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
        'OBSERVABILITY_SESSION_TTL_SECONDS=86400', '',
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


def main() -> None:
    path = Path(__file__).resolve().parents[3] / 'conf' / '.env.observability'
    result = provision(path)
    if result is None:
        print(f'凭证文件已存在，未覆盖：{path}')
        return
    print(f'已写入私有配置（权限 0600）：{path}')
    print(f"登录密码（仅显示一次，不保存明文）：{result['password']}")
    print(f"LLM 只读 Token：{result['read_token']}")


if __name__ == '__main__':
    main()
