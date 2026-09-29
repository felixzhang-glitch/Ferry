#!/usr/bin/env python3
"""营养计算引擎 (lite) —— 本 skill 中唯一产出热量数字的地方

输入 JSON (stdin / --file / 位置参数):
{
  "label": "晚餐",
  "items": [
    {"code": "012001",  "grams": 200},                       # 直接给 foodCode
    {"query": "米饭"},                                        # 无 grams → 默认份量估算 (≈标记)
    {"query": "鲈鱼",   "grams": 120, "basis": "edible"},    # basis=edible 表示已是可食部重量
    {"query": "五花肉", "portion": "少"},                     # 少0.7 / 正常1.0 / 多1.3
    {"dish":  "宫保鸡丁"}                                     # 复合菜走配方, 缺省 = 整份(一盘)
  ],
  "oil_g": 15                              # 烹调用油独立成行; 配方菜已含油, 额外加油才传
}

规则 (lite 单发估算, 不追问份量):
- grams 缺失 → 默认份量: dish 取配方整份; 食物按名称关键词/成分表大类查表
  (肉一掌心100g · 饭一碗150g · 菜一捧150g · 水果200g · 奶250g · 蛋60g · 油/坚果10g ...),
  该行标 grams_source="default", 区间 ±30%
- item.portion "少|正常|多" → 克重 ×0.7/1.0/1.3; 用户给了 grams 时区间 ±15%
- 炒菜类条目且未给 oil_g → 自动计烹调用油 10g (配方菜已含油, 不重复)
- --strict → 缺 grams 报错 (精确模式)
- basis 默认 purchase: 营养素 × 食部系数; basis=edible 时不乘
- 'Tr' 按 0 计; 无数据字段计入 missing 并在输出中声明

用法: calc.py '{"items":[...]}' | calc.py --file spec.json | echo '...' | calc.py [--pretty] [--strict]
"""
import json, os, sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import common, lookup

_dishes = None

# 默认份量: 名称关键词优先, 其次成分表大类前缀, 兜底 100g (膳食指南食物份量图口径)
KEYWORD_DEFAULTS = [("粥", 300), ("大米", 75), ("生米", 75), ("挂面", 75), ("生面", 75)]
CATEGORY_DEFAULTS = {
    "谷类": 150, "薯类": 150, "干豆类": 100, "坚果种子类": 10,
    "畜肉类": 100, "禽肉类": 100, "鱼虾蟹贝类": 100, "蛋类": 60,
    "乳类": 250, "蔬菜类": 150, "菌藻类": 100, "水果类": 200,
    "饮料类": 250, "含酒精饮料": 100, "植物油": 10, "油脂": 10,
    "调味品": 10, "糖": 20, "速食食品": 150, "小吃": 100, "糕饼": 100,
}
PORTION_MULT = {"少": 0.7, "正常": 1.0, "多": 1.3}
# 自动计油的炒菜判定: query 含烹饪词 且 条目属于这些大类 (坚果炒货/花生酱等不加油)
STIRFRY_KEYS = ("炒", "红烧", "干煸", "干锅", "油焖", "爆", "熘")
AUTO_OIL_CATS = ("蔬菜类", "畜肉类", "禽肉类", "鱼虾蟹贝类", "蛋类", "菌藻类", "薯类", "干豆类")

def default_grams(name, category):
    n = name or ""
    for kw, g in KEYWORD_DEFAULTS:
        if kw in n:
            return g
    for prefix, g in CATEGORY_DEFAULTS.items():
        if (category or "").startswith(prefix):
            return g
    return 100

NUTRIENTS = [  # (字段, 单位, 显示名)
    ("energyKCal", "kcal", "能量"), ("protein", "g", "蛋白质"), ("fat", "g", "脂肪"),
    ("CHO", "g", "碳水化合物"), ("dietaryFiber", "g", "膳食纤维"),
    ("cholesterol", "mg", "胆固醇"), ("vitaminA", "μgRE", "维生素A"),
    ("vitaminC", "mg", "维生素C"), ("Ca", "mg", "钙"), ("Fe", "mg", "铁"),
    ("Na", "mg", "钠"), ("K", "mg", "钾"),
]

def to_float(v):
    if v in ("", "—", "-", "Tr", None): return 0.0
    try: return float(str(v).rstrip("*"))
    except ValueError: return 0.0

def load_dishes():
    global _dishes
    if _dishes is None:
        _dishes = {d["name"]: d for d in common.load_data("dishes.json")}
    return _dishes

def resolve(item):
    """item → (food row, err)。优先 code, 否则 query 检索"""
    if "code" in item:
        r = lookup.get(str(item["code"]))
        return (r, None) if r else (None, f"foodCode 不存在: {item['code']}")
    q = item.get("query")
    if not q: return None, "item 缺少 code/query"
    cands = lookup.lookup(q, topk=3)
    if not cands: return None, f"查无此食物: {q}"
    return lookup.get(cands[0]["foodCode"]), None if cands[0]["score"] >= 0.89 else \
        f"「{q}」未精确命中, 已取最近候选 {cands[0]['foodName']}, 备选: {[c['foodName'] for c in cands[1:]]}"

def nutrients_of(row, grams_edible):
    out, missing = {}, []
    for field, unit, _ in NUTRIENTS:
        raw = row.get(field, "")
        if raw in ("", "—"):
            missing.append(field)
            out[field] = 0.0
        else:
            out[field] = to_float(raw) * grams_edible / 100.0
    return out, missing

def calc_item(item, fast=True):
    """→ (line dict, warnings list)。line 含 g_lo/g_hi 用于区间
    fast: grams 缺失按默认份量估 (grams_source="default", ±30%); --strict 时报错"""
    warnings = []
    mult = PORTION_MULT.get(str(item.get("portion", "")).strip(), 1.0)
    if isinstance(item.get("portion"), (int, float)):
        mult = float(item["portion"])
    user_grams = item.get("grams") is not None
    grams = float(item["grams"]) * mult if user_grams else None
    u = 0.15 if user_grams else 0.30

    if "dish" in item:  # 复合菜: 配方聚合后按克重缩放
        dish = load_dishes().get(item["dish"])
        if not dish:
            near = [n for n in load_dishes() if item["dish"] in n or n in item["dish"]]
            return None, [f"配方库无「{item['dish']}」" + (f", 近似: {near}" if near else "")]
        if grams is None:
            if not fast:
                return None, [f"「{item['dish']}」缺少 grams (--strict 不估算)"]
            grams = (float(dish.get("yield_g") or 0) or
                     sum(i["grams"] for i in dish["ingredients"]) + (dish.get("oil_g") or 0)) * mult
        agg = {f: 0.0 for f, _, _ in NUTRIENTS}
        for ing in dish["ingredients"]:
            r = lookup.get(str(ing["code"]))
            if not r: return None, [f"配方「{dish['name']}」食材 code 无效: {ing['code']}"]
            # 配方克重默认可食部(edible); 带骨/带壳食材显式标 purchase 才乘食部系数
            ed = to_float(r.get("edible") or 100) or 100.0
            g_ed = float(ing["grams"]) if ing.get("basis", "edible") == "edible" \
                else float(ing["grams"]) * ed / 100.0
            n, _ = nutrients_of(r, g_ed)
            for f in agg: agg[f] += n[f]
        if dish.get("oil_g"):
            oil = lookup.get(dish["oil_code"]) if dish.get("oil_code") else _default_oil()
            n, _ = nutrients_of(oil, float(dish["oil_g"]))
            for f in agg: agg[f] += n[f]
        yield_g = float(dish.get("yield_g") or 0) or \
            sum(i["grams"] for i in dish["ingredients"]) + (dish.get("oil_g") or 0)
        factor = grams / yield_g
        lo, hi = item.get("range") or [grams * (1 - u), grams * (1 + u)]
        line = {"dish": dish["name"], "grams": round(grams, 1), "basis": "recipe",
                "grams_source": "user" if user_grams else "default",
                "recipe_yield_g": yield_g, "source": "recipe:" + dish.get("ref", "estimated"),
                "confidence": dish.get("confidence", "medium")}
        for f in agg: line[f] = round(agg[f] * factor, 1)
        line["g_lo"], line["g_hi"] = float(lo), float(hi)
        return line, warnings

    row, warn = resolve(item)
    if warn: warnings.append(warn)
    if not row: return None, warnings
    if grams is None:
        if not fast:
            return None, warnings + [f"「{item.get('query') or item.get('code')}」缺少 grams (--strict 不估算)"]
        grams = default_grams(item.get("query") or row["foodName"], row.get("category")) * mult
    edible = to_float(row.get("edible") or 100) or 100.0
    basis = item.get("basis", "purchase")
    grams_edible = grams if basis == "edible" else grams * edible / 100.0
    n, missing = nutrients_of(row, grams_edible)
    lo, hi = item.get("range") or [grams * (1 - u), grams * (1 + u)]
    line = {"foodCode": row["foodCode"], "foodName": row["foodName"],
            "category": row["category"], "grams": round(grams, 1), "basis": basis,
            "grams_source": "user" if user_grams else "default",
            "edible_pct": edible, "grams_edible": round(grams_edible, 1),
            "source": row["source"], "confidence": row["confidence"],
            "g_lo": round(float(lo), 1), "g_hi": round(float(hi), 1)}
    for f, _, _ in NUTRIENTS: line[f] = round(n[f], 1)
    # 自动用油标记: 按用户原始 query 判断 (查表后名称会丢失"炒"等烹饪词)
    q = str(item.get("query") or "")
    if any(k in q for k in STIRFRY_KEYS) and row["category"].startswith(AUTO_OIL_CATS):
        line["_auto_oil"] = True
    if missing:
        names = [cn for f, _, cn in NUTRIENTS if f in missing]
        line["missing_fields"] = names
        warnings.append(f"{row['foodName']} 缺数据: {','.join(names)} (按 0 计)")
    return line, warnings

def _default_oil():
    for q in ("植物油", "菜籽油", "花生油"):
        cands = lookup.lookup(q, topk=1)
        if cands: return lookup.get(cands[0]["foodCode"])
    raise RuntimeError("数据库中找不到植物油条目")

def calc(spec, fast=True):
    warnings, lines = [], []
    for item in spec.get("items", []):
        line, w = calc_item(item, fast=fast)
        warnings.extend(w)
        if line: lines.append(line)
    # lite: 炒菜类条目(按原始 query 判断)且未显式给油 → 自动计 10g 烹调用油 (配方菜已含油)
    auto_flags = [l.pop("_auto_oil", False) for l in lines]
    if fast and spec.get("oil") is None and not spec.get("oil_g") and any(auto_flags):
        spec = dict(spec, oil_g=10)
        warnings.append("含炒菜类条目且未给用油量, 已自动计入烹调用油 10g")
    oil_spec = spec.get("oil")
    if oil_spec is None and spec.get("oil_g"):
        oil_spec = {"grams": spec["oil_g"]}
    if oil_spec:
        oil = _default_oil() if "query" not in oil_spec and "code" not in oil_spec else None
        if oil:
            n, _ = nutrients_of(oil, float(oil_spec["grams"]))
            line = {"foodCode": oil["foodCode"], "foodName": oil["foodName"] + "(烹调用)",
                    "category": oil["category"], "grams": float(oil_spec["grams"]),
                    "basis": "edible", "edible_pct": 100, "grams_edible": float(oil_spec["grams"]),
                    "grams_source": "user",
                    "source": oil["source"], "confidence": oil["confidence"],
                    "g_lo": float(oil_spec["grams"]), "g_hi": float(oil_spec["grams"])}
            for f, _, _ in NUTRIENTS: line[f] = round(n[f], 1)
            lines.append(line)
        else:
            line, w = calc_item({**oil_spec, "basis": "edible"})
            warnings.extend(w)
            if line: lines.append(line)

    totals = {f: round(sum(l[f] for l in lines), 1) for f, _, _ in NUTRIENTS}
    totals["salt_g"] = round(totals["Na"] * 2.54 / 1000, 1)
    kcal_lo = round(sum(l["energyKCal"] * l["g_lo"] / l["grams"] for l in lines if l["grams"]))
    kcal_hi = round(sum(l["energyKCal"] * l["g_hi"] / l["grams"] for l in lines if l["grams"]))
    return {
        "label": spec.get("label", ""),
        "items": lines,
        "totals": totals,
        "kcal_range": [kcal_lo, kcal_hi] if kcal_lo != totals["energyKCal"] or kcal_hi != totals["energyKCal"] else None,
        "estimated_items": [l.get("dish") or l["foodName"] for l in lines if l.get("grams_source") == "default"],
        "warnings": warnings,
        "provenance": [f"{l.get('dish') or l['foodName']} · {l['source']} · {l.get('foodCode', '-')}" for l in lines],
    }

def pretty(res):
    out = [f"== {res['label'] or '计算结果'} =="]
    for l in res["items"]:
        name = l.get("dish") or l["foodName"]
        if l.get("grams_source") == "default": name = "≈" + name
        extra = f" (食部{l['edible_pct']}%)" if l.get("edible_pct", 100) < 100 and l.get("basis") != "edible" else ""
        out.append(f"  {name} {l['grams']:g}g{extra}: {l['energyKCal']:g} kcal | "
                   f"P{l['protein']:g} F{l['fat']:g} C{l['CHO']:g} Na{l['Na']:g}mg")
    t = res["totals"]
    out.append(f"  合计: {t['energyKCal']:g} kcal | 蛋白质 {t['protein']:g}g 脂肪 {t['fat']:g}g "
               f"碳水 {t['CHO']:g}g 纤维 {t['dietaryFiber']:g}g | 钠 {t['Na']:g}mg(≈盐{t['salt_g']:g}g)")
    if res["kcal_range"]:
        out.append(f"  能量区间: {res['kcal_range'][0]}~{res['kcal_range'][1]} kcal")
    if res.get("estimated_items"):
        out.append(f"  ≈ 为默认份估算(±30%): {'、'.join(res['estimated_items'])} · 提供克重可精确")
    for w in res["warnings"]: out.append(f"  ⚠ {w}")
    return "\n".join(out)

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--file" in sys.argv:
        spec = json.load(open(args[0], encoding="utf-8"))
    elif args:
        spec = json.loads(args[0])
    else:
        spec = json.load(sys.stdin)
    res = calc(spec, fast="--strict" not in sys.argv and spec.get("fast", True))
    if "--pretty" in sys.argv:
        print(pretty(res))
    else:
        print(json.dumps(res, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
