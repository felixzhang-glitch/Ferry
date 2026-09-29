#!/usr/bin/env python3
"""食物检索: 中文名/别名/英文名 → foodCode 候选

匹配顺序: 精确 → 别名 → 规范化精确(括号全半角) → 双向子串 → difflib 模糊
用法:
  python3 lookup.py 五花肉            # 单个查询, 输出 top-k JSON
  python3 lookup.py 五花肉 米饭 鲈鱼   # 批量
  python3 lookup.py --interactive     # 每行一个查询 (供其他脚本/管道)
"""
import csv, json, os, re, sys, difflib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import common

FOODS_CSV = common.data_file("foods.csv")
ALIASES_JSON = common.data_file("aliases.json")

_foods, _by_code, _aliases = None, None, None

def norm(s):
    for a, b in [("（", "("), ("）", ")"), ("，", ","), ("、", ","),
                 (" ", ""), ("　", ""), ("·", "")]:
        s = s.replace(a, b)
    return s.strip().lower()

def load():
    global _foods, _by_code, _aliases
    if _foods is not None: return
    common.require_data("foods.csv")
    _foods, _by_code = [], {}
    with open(FOODS_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            r["_norm"] = norm(r["foodName"])
            _foods.append(r)
            _by_code[r["foodCode"]] = r
    _aliases = {}
    if os.path.exists(ALIASES_JSON):
        with open(ALIASES_JSON, encoding="utf-8") as f:
            for alias, target in json.load(f).items():
                _aliases[norm(alias)] = target

def get(code):
    load()
    return _by_code.get(code)

def brief(r, score):
    return {"foodCode": r["foodCode"], "foodName": r["foodName"],
            "category": r["category"], "energyKCal": r["energyKCal"],
            "edible": r["edible"], "source": r["source"],
            "confidence": r["confidence"], "score": round(score, 3)}

def lookup(query, topk=5):
    """返回候选列表, 按匹配质量排序"""
    load()
    q = norm(query)
    if not q: return []
    hits, seen = [], set()

    def add(r, score):
        if r["foodCode"] not in seen:
            seen.add(r["foodCode"])
            hits.append(brief(r, score))

    # 0. 直接是 foodCode
    if query in _by_code:
        add(_by_code[query], 1.0)
        return hits
    # 1. 精确
    for r in _foods:
        if r["_norm"] == q: add(r, 1.0)
    if hits: return hits[:topk]
    # 2. 别名
    target = _aliases.get(q)
    if target:
        r = _by_code.get(target) if target in _by_code else None
        if r is None:
            for f in _foods:
                if f["_norm"] == norm(target): r = f; break
        if r: add(r, 0.98)
    # 3. 子串 (双向)。排序: 代表值/均值优先 → 查询词开头匹配优先 → 短名优先
    def rank(r):
        n = r["_norm"]
        rep = 0 if ("代表值" in n or "均值" in n) else 1
        prefix = 0 if n.startswith(q) else (1 if q in n else 2)
        return (rep, prefix, len(n))
    subs = [r for r in _foods if q in r["_norm"] or r["_norm"] in q]
    subs.sort(key=rank)
    for r in subs[:topk]: add(r, 0.9)
    # 3.5 括号修饰词换位: 库名"花生(炒)" ↔ 查询"炒花生" (主名与修饰词都出现在查询中)
    if len(hits) < topk:
        pat = re.compile(r"^([^(]+)\(([^)]+)\)$")
        swap = []
        for r in _foods:
            m = pat.match(r["_norm"])
            if not m: continue
            base, mod = m.group(1), m.group(2)
            if base and base in q and any(p and p in q for p in mod.split(",")):
                swap.append(r)
        swap.sort(key=lambda r: (0 if ("代表值" in r["foodName"] or "均值" in r["foodName"]) else 1,
                                 len(r["_norm"])))
        for r in swap[:topk]: add(r, 0.88)
    # 4. 模糊
    if len(hits) < topk:
        names = {r["_norm"]: r for r in _foods}
        for m in difflib.get_close_matches(q, list(names), n=topk, cutoff=0.55):
            add(names[m], 0.6)
    return hits[:topk]

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--interactive" in sys.argv:
        for line in sys.stdin:
            line = line.strip()
            if line: print(json.dumps({line: lookup(line)}, ensure_ascii=False))
        return
    if not args:
        print(__doc__); sys.exit(1)
    out = {q: lookup(q) for q in args}
    print(json.dumps(out, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
