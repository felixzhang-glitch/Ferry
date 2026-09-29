#!/usr/bin/env python3
"""饮食日记 + 体重记录 + 反馈校准

用法:
  log.py eat --meal 午餐 --items '{"items":[{"query":"米饭","grams":200}],"oil_g":10}'
  log.py weight 74.8 [--date 2026-01-02]
  log.py report [--days 7]
  log.py calibrate          # ≥10 天记录 + ≥2 次体重后, 用体重趋势反推实际 TDEE
"""
import argparse, json, os, sys
from datetime import date, datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import calc, common

PROFILE_DIR = common.PROFILE_DIR
LOG = common.profile_file("log.jsonl")
WEIGHT = common.profile_file("weight.jsonl")
PROFILE = common.profile_file("active.json")

def append(path, obj):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def read_all(path):
    if not os.path.exists(path): return []
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]

def load_profile():
    return json.load(open(PROFILE, encoding="utf-8")) if os.path.exists(PROFILE) else {}

def cmd_eat(a):
    spec = json.loads(a.items)
    res = calc.calc(spec)
    t = res["totals"]
    rec = {"date": a.date or str(date.today()), "meal": a.meal,
           "label": spec.get("label", ""), "kcal": t["energyKCal"],
           "protein": t["protein"], "fat": t["fat"], "CHO": t["CHO"], "Na_mg": t["Na"]}
    append(LOG, rec)
    print(calc.pretty(res))
    print(f"\n已记录 {rec['date']} {a.meal}")

def cmd_weight(a):
    rec = {"date": a.date or str(date.today()), "weight_kg": a.kg}
    append(WEIGHT, rec)
    print(f"已记录 {rec['date']} 体重 {a.kg} kg")

def cmd_report(a):
    prof = load_profile()
    target = prof.get("能量", {}).get("target_kcal")
    logs = read_all(LOG)[-a.days * 6:]
    by_day = {}
    for r in logs:
        d = by_day.setdefault(r["date"], {"kcal": 0, "protein": 0, "Na": 0, "meals": 0})
        d["kcal"] += r["kcal"]; d["protein"] += r["protein"]
        d["Na"] += r["Na_mg"]; d["meals"] += 1
    print(f"== 近 {a.days} 天摄入 ==")
    for day in sorted(by_day)[-a.days:]:
        d = by_day[day]
        line = f"{day}: {d['kcal']:.0f} kcal | P {d['protein']:.0f}g | 盐 {d['Na']*2.54/1000:.1f}g"
        if target: line += f" | 目标 {target} ({(d['kcal']-target)/target:+.0%})"
        print(line)
    ws = read_all(WEIGHT)
    if len(ws) >= 2:
        first, last = ws[0], ws[-1]
        print(f"\n体重: {first['date']} {first['weight_kg']}kg → {last['date']} {last['weight_kg']}kg "
              f"({last['weight_kg']-first['weight_kg']:+.1f}kg)")

def cmd_calibrate(a):
    """用 ≥10 天平均摄入 + 体重变化反推实际 TDEE (7700 kcal ≈ 1kg 体脂)"""
    logs, ws = read_all(LOG), read_all(WEIGHT)
    if len(ws) < 2: raise SystemExit("体重记录不足 2 次, 无法校准")
    by_day = {}
    for r in logs:
        by_day.setdefault(r["date"], 0)
        by_day[r["date"]] += r["kcal"]
    if len(by_day) < 10: raise SystemExit(f"完整记录仅 {len(by_day)} 天, 需 ≥10 天")
    days = sorted(by_day)
    w_in = [w for w in ws if days[0] <= w["date"] <= days[-1]]
    if len(w_in) < 2: raise SystemExit("记录期内体重数据不足 2 次")
    span = (datetime.fromisoformat(days[-1]) - datetime.fromisoformat(days[0])).days or 1
    avg_intake = sum(by_day.values()) / len(by_day)
    delta_w = w_in[-1]["weight_kg"] - w_in[0]["weight_kg"]
    daily_energy_gap = delta_w * 7700 / span          # 正=实际盈余
    est_tdee = avg_intake + daily_energy_gap
    prof = load_profile()
    est_formula = prof.get("能量", {}).get("tdee_kcal")
    print(f"记录期 {days[0]} ~ {days[-1]} ({span} 天)")
    print(f"平均摄入 {avg_intake:.0f} kcal/日, 体重变化 {delta_w:+.2f} kg")
    print(f"反推实际 TDEE ≈ {est_tdee:.0f} kcal/日")
    if est_formula:
        diff = (est_tdee - est_formula) / est_formula
        print(f"公式估算 {est_formula} kcal, 偏差 {diff:+.0%}")
        if abs(diff) > 0.10:
            print(f"建议: 目标能量按实测 TDEE 重设 → {est_tdee:.0f} - 缺口400 ≈ {est_tdee-400:.0f} kcal")
        else:
            print("公式估算与实测吻合, 无需调整")

def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("eat"); pe.add_argument("--meal", required=True)
    pe.add_argument("--items", required=True); pe.add_argument("--date")
    pw = sub.add_parser("weight"); pw.add_argument("kg", type=float); pw.add_argument("--date")
    pr = sub.add_parser("report"); pr.add_argument("--days", type=int, default=7)
    sub.add_parser("calibrate")
    a = p.parse_args()
    {"eat": cmd_eat, "weight": cmd_weight, "report": cmd_report,
     "calibrate": cmd_calibrate}[a.cmd](a)

if __name__ == "__main__":
    main()
