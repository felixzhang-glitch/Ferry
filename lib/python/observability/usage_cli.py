"""Explicit pi usage sync entry point for bin/server; never touches native logs."""
from __future__ import annotations

import argparse
import json
import sys

from observability.config import get_observability_settings


def main() -> int:
    parser = argparse.ArgumentParser(description='Synchronize content-free pi usage history')
    parser.add_argument('command', choices=['sync'])
    parser.parse_args()
    settings = get_observability_settings()
    if not settings.enabled or not settings.pi_usage_enabled or not settings.hmac_key:
        print('pi 历史用量未启用或未配置去标识化密钥', file=sys.stderr)
        return 2
    from observability.pi_usage import PiUsageLedger
    ledger = PiUsageLedger(settings.store_dir, settings.resolved_pi_session_dirs,
                           settings.hmac_key, sync_interval_seconds=settings.pi_scan_interval_seconds)
    try:
        ledger.sync(force=True)
        data = ledger.query()
        print(json.dumps({
            'source': 'pi', 'sessions': data.get('sessions'), 'failed': data.get('failed'),
            'totals': data.get('totals'), 'scan': data.get('scan'), 'quality': data.get('quality'),
        }, ensure_ascii=False, indent=2))
        return 1 if data.get('failed', 0) else 0
    except Exception:
        print('pi 历史用量同步失败；请检查目录权限及可观测配置', file=sys.stderr)
        return 1
    finally:
        ledger.close()


if __name__ == '__main__':
    raise SystemExit(main())
