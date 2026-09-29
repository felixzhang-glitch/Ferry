#!/usr/bin/env python3
"""能量基线与目标: Mifflin-St Jeor BMR × 活动系数 → TDEE → 目标能量档

用法:
  tdee.py --sex 男 --age 32 --height 172 --weight 75 --activity 久坐 --goal 减重
  tdee.py ... --save            # 写入 profile/active.json
"""
import argparse, json, os, sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import common

GUIDANCE = None
PROFILE = common.profile_file("active.json")

def guidance():
    global GUIDANCE
    if GUIDANCE is None:
        common.require_data("guidance.json")
        GUIDANCE = json.load(open(common.data_file("guidance.json"), encoding="utf-8"))
    return GUIDANCE

def bmr_mifflin(sex, weight, height, age):
    base = 10 * weight + 6.25 * height - 5 * age
    return base + 5 if sex in ("男", "male", "m") else base - 161

def nearest_tier(kcal):
    tiers = guidance()["目标能量档_kcal"]
    return min(tiers, key=lambda t: abs(t - kcal))

def compute(sex, age, height, weight, activity, goal, elderly=False):
    G = guidance()
    bmr = bmr_mifflin(sex, weight, height, age)
    factors = G["活动系数"]
    factor = factors.get(activity)
    if factor is None:
        raise SystemExit(f"activity 必须是: {[k for k in factors if not k.startswith('_')]}")
    tdee = bmr * factor
    if goal == "减重":
        deficit = 250 if elderly else 400  # 指南缺口 300~500, 老年 200~300
    elif goal == "增重":
        deficit = -300
    else:
        deficit = 0
    target = max(tdee - deficit, 1200)
    # 宏量营养素
    p_lo, p_hi = G["减重"]["老年蛋白质_g每kg" if elderly else "蛋白质_g每kg"]
    protein_g = weight * ((p_lo + p_hi) / 2 if goal == "减重" else 1.0)
    fat_g = target * 0.225 / 9
    cho_g = (target - protein_g * 4 - fat_g * 9) / 4
    split = G["餐次能量比"]
    return {
        "input": {"sex": sex, "age": age, "height_cm": height, "weight_kg": weight,
                  "activity": activity, "goal": goal, "elderly": elderly},
        "bmr_kcal": round(bmr),
        "tdee_kcal": round(tdee),
        "deficit_kcal": deficit,
        "target_kcal": round(target),
        "nearest_tier_kcal": nearest_tier(target),
        "macros_g": {"protein": round(protein_g), "fat": round(fat_g), "CHO": round(cho_g)},
        "meal_split_kcal": {k: round(target * v) for k, v in split.items()},
        "daily_limits": {"盐_g_max": 5, "烹调油_g": G["成人每日推荐"]["烹调油_g"]["减重" if goal == "减重" else "normal"],
                        "添加糖_g_max": 25, "膳食纤维_g": G["成人每日推荐"]["膳食纤维_g"]},
    }

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sex", required=True, choices=["男", "女", "male", "female", "m", "f"])
    p.add_argument("--age", type=int, required=True)
    p.add_argument("--height", type=float, required=True)
    p.add_argument("--weight", type=float, required=True)
    p.add_argument("--activity", default="久坐")
    p.add_argument("--goal", default="减重", choices=["减重", "维持", "增重"])
    p.add_argument("--elderly", action="store_true")
    p.add_argument("--save", action="store_true")
    a = p.parse_args()
    res = compute(a.sex, a.age, a.height, a.weight, a.activity, a.goal, a.elderly)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if a.save:
        os.makedirs(os.path.dirname(PROFILE), exist_ok=True)
        prof = json.load(open(PROFILE, encoding="utf-8")) if os.path.exists(PROFILE) else {}
        prof.update({"身体": res["input"], "能量": {k: v for k, v in res.items() if k != "input"}})
        json.dump(prof, open(PROFILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"已写入 {PROFILE}", file=sys.stderr)

if __name__ == "__main__":
    main()
