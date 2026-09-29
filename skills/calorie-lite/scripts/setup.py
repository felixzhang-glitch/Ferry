#!/usr/bin/env python3
"""初始化 calorie-lite 数据目录 (每台运行机执行一次)

仓库只含代码; 成分表数据(版权限制, 禁止分发)与个人档案(身体数据/饮食/体重)
统一放在 CALORIE_HOME (默认 ~/.env/calorie-lite/), 目录权限收紧为 700

用法:
  python3 setup.py [--source ~/.agents/skills/calorie-coach]
  # 无本机源目录时(如部署机): 手动把 data 文件放入 ~/.env/calorie-lite/data/ 即可
"""
import argparse, json, os, shutil, sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import common

DATA_FILES = ["foods.csv", "aliases.json", "dishes.json", "portions.json",
              "guidance.json", "purine.json", "raw_cooked.json"]
PROFILE_FILES = ["active.json", "log.jsonl", "weight.jsonl"]

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", default=os.path.join(
        os.path.expanduser("~"), ".agents", "skills", "calorie-coach"),
        help="calorie-coach 原版目录 (提供数据文件与既有个人档案)")
    a = p.parse_args()

    os.makedirs(common.DATA_DIR, exist_ok=True)
    prof_dir = common.PROFILE_DIR
    os.makedirs(prof_dir, exist_ok=True)

    copied, missing, migrated = [], [], []
    for f in DATA_FILES:
        s = os.path.join(a.source, "data", f)
        d = common.data_file(f)
        if os.path.exists(s):
            shutil.copy2(s, d)
            copied.append(f)
        elif os.path.exists(d):
            copied.append(f + "(已有)")
        else:
            missing.append(f)
    # 迁移个人档案 (不覆盖目标位置已有文件)
    for f in PROFILE_FILES:
        s = os.path.join(a.source, "profile", f)
        d = os.path.join(prof_dir, f)
        if os.path.exists(s) and not os.path.exists(d):
            shutil.copy2(s, d)
            migrated.append(f)

    # 隐私: 个人数据目录仅本人可读写
    os.chmod(common.HOME, 0o700)
    os.chmod(prof_dir, 0o700)

    print(json.dumps({"home": common.HOME, "data_copied": copied,
                      "data_missing": missing, "profile_migrated": migrated},
                     ensure_ascii=False, indent=1))
    if missing:
        print(f"⚠ 源目录缺: {missing} — 可手动放入 {common.DATA_DIR}/", file=sys.stderr)

if __name__ == "__main__":
    main()
