#!/usr/bin/env python3
"""一日菜单生成: 交换份分配 (二元方程解) + dishes.json 组菜 + calc 验证

用法:
  plan.py --target 1400 [--protein 84] [--region 西南] [--avoid 内脏,海鲜] [--disease 高血压] [--seed 42]
  plan.py --profile            # 从 profile/active.json 读 target_kcal 与 macros
"""
import argparse, json, os, random, sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import calc, common, lookup

def load(name):
    return common.load_data(name)

def allocate(target_kcal, protein_g):
    """交换份分配 (每份≈90kcal)。规则式 + 上下限:
       蔬菜/水果/豆乳固定; 谷薯按碳水供能45%反推(每份≈18g碳水); 肉蛋按蛋白质缺口反推
       (每份≈9g); 油脂作热量平衡项 (每份≈10g油)。各类带 floor/cap 保证可执行。"""
    veg, fruit, dairy = 1.0, (1.0 if target_kcal >= 1400 else 0.5), 2.5
    clamp = lambda x, lo, hi: max(lo, min(hi, round(x * 2) / 2))
    grain = clamp((target_kcal * 0.45 / 4 - fruit * 20 - veg * 10 - dairy * 8) / 18, 4, 9)
    meat = clamp((protein_g - grain * 2.5 - veg * 3 - fruit * 1 - dairy * 5.5) / 9, 3, 8)
    oil = clamp((target_kcal - 90 * (grain + veg + fruit + meat + dairy)) / 90, 1.5, 3.5)
    return {"谷薯": grain, "蔬菜": veg, "水果": fruit,
            "肉蛋": meat, "豆乳": dairy, "油脂": oil}

def dish_nutrition(dish):
    """整份配方的营养 (缓存)"""
    if not hasattr(dish_nutrition, "_c"):
        dish_nutrition._c = {}
    if dish["name"] not in dish_nutrition._c:
        res = calc.calc({"items": [{"dish": dish["name"], "grams": dish["yield_g"]}]})
        t = res["totals"]
        dish_nutrition._c[dish["name"]] = {
            "kcal": t["energyKCal"], "protein": t["protein"], "fat": t["fat"],
            "Na": t["Na"], "per100_kcal": round(t["energyKCal"] / dish["yield_g"] * 100)}
    return dish_nutrition._c[dish["name"]]

def filter_dishes(dishes, avoid, disease, purine):
    avoid = avoid or []
    out = []
    purine_avoid = set()
    purine_mid = set()
    if disease == "高尿酸":
        purine_avoid = set(purine["高_避免"]["items"])
        purine_mid = set(purine["中_限量"]["items"])
    for d in dishes:
        text = d["name"] + " " + json.dumps(d["ingredients"], ensure_ascii=False)
        if any(a and a in text for a in avoid): continue
        if disease == "高血压" and dish_nutrition(d)["Na"] > 600: continue
        if disease == "高尿酸":
            names = " ".join((lookup.get(str(i["code"])) or {}).get("foodName", "")
                             for i in d["ingredients"])
            if any(p in names or p in d["name"] for p in purine_avoid): continue
            if sum(1 for p in purine_mid if p in names) >= 2: continue
        out.append(d)
    return out

def _cat(code):
    r = lookup.get(str(code))
    return r["category"] if r else ""

def kcal_of(spec_items):
    """小工具: 一组 items 的能量"""
    return calc.calc({"items": spec_items})["totals"]["energyKCal"]

def assemble(target, alloc, dishes, region, seed, protein_g):
    """先定菜(预算大头), 主食填缺口, 最后 calc 全量验证"""
    rnd = random.Random(seed)
    meat_dishes = [d for d in dishes if any(
        _cat(i["code"]).startswith(("畜肉", "禽肉", "鱼虾")) for i in d["ingredients"])]
    veg_dishes = [d for d in dishes if d not in meat_dishes]
    # 蛋白质导向: 荤菜优先蛋白密度 ≥8g/100g 的; 蛋白目标高时两道荤菜都从高密池取
    def prot_per100(d):
        n = dish_nutrition(d)
        return n["protein"] / d["yield_g"] * 100 if d["yield_g"] else 0
    hp = [d for d in meat_dishes if prot_per100(d) >= 8]
    lp = [d for d in meat_dishes if prot_per100(d) < 8]
    rnd.shuffle(hp); rnd.shuffle(lp); rnd.shuffle(veg_dishes)
    high_target = protein_g >= target * 0.22 / 4  # 蛋白供能比 ≥22% 算高目标
    meat_pool = (hp + hp + lp) if high_target else (hp + lp)

    def pick(pool, used):
        for d in pool:
            if d["name"] not in used: return d
        return pool[0]

    used = set()
    lm = pick(meat_pool, used); used.add(lm["name"])
    dm = pick(meat_pool, used); used.add(dm["name"])
    if "汤" in lm["name"] and "汤" in dm["name"]:
        dm = pick([d for d in meat_pool if "汤" not in d["name"]] or meat_pool, used)
    lv = pick(veg_dishes, used); used.add(lv["name"])
    dv = pick(veg_dishes, used)
    picked = [lm, lv, dm, dv]

    # 菜品预算: 早 28% + 水果 ~5%, 菜品目标占 ~62%; 超出则整盘等比缩到 0.5~1.0 份
    dishes_kcal_full = sum(dish_nutrition(d)["kcal"] for d in picked)
    scale = max(0.5, min(1.0, target * 0.62 / dishes_kcal_full))
    picked = [dict(d, yield_g=round(d["yield_g"] * scale)) for d in picked]

    # 早餐: 蛋 + 奶 + 黄瓜 + 馒头(凑到 ~28% 目标)
    bf_fixed = [{"code": "111101x", "grams": 60}, {"code": "101101x", "grams": 200},
                {"code": "043208", "grams": 100}]
    bf_kcal = kcal_of(bf_fixed)
    mantou_g = max(0, (target * 0.28 - bf_kcal)) / 2.23  # 馒头 223kcal/100g
    # 加餐水果
    fruit_kcal = kcal_of([{"code": "061101x", "grams": alloc["水果"] * 170}])
    # 菜品能量 (缩放后)
    dishes_kcal = sum(dish_nutrition(d)["kcal"] * scale for d in picked)
    # 米饭填剩余缺口 (116kcal/100g), 午 55% 晚 45%
    rice_kcal = target - bf_kcal - mantou_g * 2.23 - fruit_kcal - dishes_kcal
    rice_g = max(0, min(rice_kcal / 1.16, 450))
    if 0 < rice_g < 80:
        rice_g = 0  # 总量不足半碗(80g)就不单列, 碳水由菜品内主食覆盖
    leftover = rice_kcal - rice_g * 1.16

    lm, lv, dm, dv = picked  # 缩放后的菜品
    # 菜品本身是主食(炒饭/面)时, 该餐不再单列米饭
    lunch_staple = lm.get("cuisine") == "主食" or lv.get("cuisine") == "主食"
    dinner_staple = dm.get("cuisine") == "主食" or dv.get("cuisine") == "主食"
    lunch_rice = [] if (lunch_staple or rice_g == 0) else [f"米饭 {round(rice_g*0.55)}g"]
    dinner_rice = [] if (dinner_staple or rice_g == 0) else [f"米饭 {round(rice_g*0.45)}g 或 等量面条"]
    plan = {
        "早餐": [f"馒头 {round(mantou_g)}g 或 等量全麦面包", "鸡蛋 1 个(60g)",
                 "牛奶 200ml", "拌黄瓜 100g"],
        "午餐": lunch_rice + [f"{lm['name']} {lm['yield_g']}g", f"{lv['name']} {lv['yield_g']}g"],
        "加餐": [f"水果 {round(alloc['水果']*170)}g (如苹果/梨/橙)"],
        "晚餐": dinner_rice + [f"{dm['name']} {dm['yield_g']}g", f"{dv['name']} {dv['yield_g']}g"],
    }
    if leftover > 100:
        plan["加餐"].append(f"主食缺口 {leftover:.0f} kcal, 可加燕麦/玉米/牛奶")
    note = None
    if rice_kcal < 0:
        note = f"菜品能量已超目标 {-rice_kcal:.0f} kcal, 建议菜品减半或换清淡菜"
    elif scale < 1.0:
        note = f"菜品按 {scale:.0%} 份量计以满足能量目标"
    if rice_g == 0 and rice_kcal > 0:
        note = (note + "; " if note else "") + "菜品中的主食(炒饭/土豆/粉丝等)已覆盖碳水, 不再单列米饭"
    verify_items = [{"dish": d["name"], "grams": d["yield_g"]} for d in picked] + bf_fixed + [
        {"code": "011404x", "grams": mantou_g}, {"code": "012401x", "grams": rice_g},
        {"code": "061101x", "grams": alloc["水果"] * 170}]
    return plan, verify_items, note

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--target", type=float)
    p.add_argument("--protein", type=float)
    p.add_argument("--region", default="西南")
    p.add_argument("--avoid", default="")
    p.add_argument("--disease", default="")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--profile", action="store_true")
    a = p.parse_args()

    if a.profile:
        prof = common.load_profile()
        a.target = a.target or prof["能量"]["target_kcal"]
        a.protein = a.protein or prof["能量"]["macros_g"]["protein"]
    if not a.target:
        raise SystemExit("缺 --target 或 --profile")
    a.protein = a.protein or a.target * 0.20 / 4

    guidance, purine = load("guidance.json"), load("purine.json")
    dishes = load("dishes.json")
    alloc = allocate(a.target, a.protein)
    avoid = [x for x in a.avoid.split(",") if x]
    pool = filter_dishes(dishes, avoid, a.disease, purine)
    if len(pool) < 4:
        raise SystemExit(f"约束后可用菜品不足 ({len(pool)} 道), 放宽 --avoid/--disease")
    # 多 seed 重试: 能量±10% 与 盐≤6g 为硬条件, 蛋白质越高越好, 最多试 15 次
    best = None
    for offset in range(15):
        menu, verify_items, note = assemble(a.target, alloc, pool, a.region, a.seed + offset, a.protein)
        t = calc.calc({"items": verify_items})["totals"]
        dev = (t["energyKCal"] - a.target) / a.target
        ok_kcal, ok_salt = abs(dev) <= 0.10, t["salt_g"] <= 6
        score = (ok_kcal, ok_salt, t["protein"])
        if best is None or score > best[0]:
            best = (score, menu, note, t, dev, offset)
        if ok_kcal and ok_salt and t["protein"] >= a.protein * 0.8:
            break
    _, menu, note, t, dev, used_offset = best
    ok_kcal, ok_salt = abs(dev) <= 0.10, t["salt_g"] <= 6
    ok_protein = t["protein"] >= a.protein * 0.8

    print(f"== 一日菜单 · 目标 {a.target:.0f} kcal · 蛋白质 {a.protein:.0f}g ==")
    print(f"交换份: 谷薯{alloc['谷薯']} 蔬菜{alloc['蔬菜']} 水果{alloc['水果']} "
          f"肉蛋{alloc['肉蛋']} 豆乳{alloc['豆乳']} 油脂{alloc['油脂']}")
    for meal, items in menu.items():
        if meal.startswith("_"): continue
        print(f"\n【{meal}】" + "、".join(items))
    if note:
        print(f"\n※ {note}")
    print(f"\n== 验证 ==")
    print(f"总能量 {t['energyKCal']:.0f} kcal (目标 {a.target:.0f}, 偏差 {dev:+.0%})")
    print(f"蛋白质 {t['protein']:.0f}g (目标 {a.protein:.0f}g) | 脂肪 {t['fat']:.0f}g | "
          f"钠 {t['Na']:.0f}mg (≈盐 {t['salt_g']:.1f}g, 上限 5g)")
    print("达标" if ok_kcal and ok_protein and ok_salt
          else f"需调整: {'能量偏差超±10% ' if not ok_kcal else ''}"
               f"{'蛋白质不足(可加鸡胸/鱼虾/蛋白) ' if not ok_protein else ''}"
               f"{'盐超标' if not ok_salt else ''}")
    if a.disease:
        rules = guidance["疾病约束"].get(a.disease, {}).get("规则", [])
        if rules:
            print(f"\n{a.disease}注意: " + "; ".join(rules))

if __name__ == "__main__":
    main()
