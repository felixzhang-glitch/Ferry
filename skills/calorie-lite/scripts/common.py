#!/usr/bin/env python3
"""calorie-lite 公共路径解析 —— 代码与数据分离

仓库内只有代码。以下内容一律放在 CALORIE_HOME（默认 ~/.env/calorie-lite/）：
  data/     食物成分表数据（版权属原出版社，仅限个人本地使用，禁止入库/分发）
  profile/  个人档案: active.json(身体数据) log.jsonl(饮食) weight.jsonl(体重)

环境变量覆盖: CALORIE_HOME / CALORIE_DATA_DIR / CALORIE_PROFILE_DIR
首次使用: python3 scripts/setup.py
"""
import json, os, sys

HOME = os.environ.get("CALORIE_HOME") or os.path.join(os.path.expanduser("~"), ".env", "calorie-lite")
DATA_DIR = os.environ.get("CALORIE_DATA_DIR") or os.path.join(HOME, "data")
PROFILE_DIR = os.environ.get("CALORIE_PROFILE_DIR") or os.path.join(HOME, "profile")

def data_file(name):
    return os.path.join(DATA_DIR, name)

def profile_file(name):
    return os.path.join(PROFILE_DIR, name)

def require_data(*names):
    """缺数据文件时给出可操作的报错"""
    missing = [n for n in names if not os.path.exists(data_file(n))]
    if missing:
        sys.exit(f"[calorie-lite] 缺数据文件: {', '.join(missing)}\n"
                 f"  数据目录: {DATA_DIR}\n"
                 f"  初始化: python3 <skill-dir>/scripts/setup.py（或手动放入上述文件，版权原因不随仓库分发）")

def load_data(name):
    require_data(name)
    with open(data_file(name), encoding="utf-8") as f:
        return json.load(f)

def load_profile():
    p = profile_file("active.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
