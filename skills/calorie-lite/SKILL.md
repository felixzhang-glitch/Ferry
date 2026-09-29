---
name: calorie-lite
description: 卡路里/热量单发估算与饮食建议（calorie-coach 瘦身版）。当用户问这顿多少卡、吃了什么、算热量、减脂怎么吃、一天该吃多少、TDEE/基础代谢、要一天菜单、记录体重时触发。数值一律由脚本查《中国食物成分表》计算，禁止心算；不追问份量，缺克重按默认份直接估（±30%）。
---

# calorie-lite

calorie-coach 的单发版：默认份估算、不多轮确认、接受 ±30% 误差；用户主动给克重则按 ±15% 精确算。
**唯一硬约束：热量数字只能出自 calc.py 查表，禁止凭模型记忆报数。**

## 数据与隐私

- 仓库只有代码。成分表数据（版权属原出版社，仅限个人本地使用，禁止入库/分发）与个人档案（身体数据、饮食日记、体重）都在 `~/.env/calorie-lite/`（`CALORIE_HOME` 可覆盖），目录 700 权限
- 每台运行机初始化一次：`python3 <skill-dir>/scripts/setup.py`；部署机没有源数据时按 setup.py 输出提示手动放置 data 文件（不走 git）
- 任何个人信息（体重、档案、饮食记录）不得写入仓库或提交

## 单发流程

1. 用户说吃了什么 → 直接组 items（只有食物名，无克重也行）→ 一次 calc.py → 报结果
2. 回复一行式：`午餐 ≈ 700 kcal（560~880）· 蛋白 32g · 占日目标 33%`（有档案时算占比），附一句「默认份估算，报克重可精确」
3. **不反问克重、不反问用油**。仅当用户明确说要精确核算且没给克重时才问一次
4. 用户说「大份/小份/吃得多」→ items 里加 `"portion": "多|少"`（×1.3/×0.7）
5. warnings 里「未精确命中」自动取最近候选即可，不打扰用户；照片识餐同理：识别菜名 → 直接单发，识别不出就说

## 命令

`<skill-dir>` = 仓库内 `skills/calorie-lite/`。python3 纯标准库，无依赖

```bash
# 算一餐（唯一出数字的入口）: query 食物名 / dish 配方菜名(51道) / code foodCode
python3 <skill-dir>/scripts/calc.py '{"label":"午餐","items":[{"query":"米饭"},{"dish":"宫保鸡丁"}]}' --pretty
#   grams 可缺省(自动默认份); basis:"edible"=净肉净菜重量; oil_g 额外用油; --strict=缺克重报错

# 能量基线/目标 (Mifflin-St Jeor); --save 写档案, 之后 log/plan 自动读
python3 <skill-dir>/scripts/tdee.py --sex 男 --age 32 --height 172 --weight 75 --activity 久坐 --goal 减重 --save
#   activity: 久坐/轻体力/中体力/重体力

# 饮食日记/体重/周报/校准(≥10天记录+2次体重后反推真实TDEE)
python3 <skill-dir>/scripts/log.py eat --meal 午餐 --items '{"items":[{"query":"米饭"},{"dish":"宫保鸡丁"}]}'
python3 <skill-dir>/scripts/log.py weight 74.8
python3 <skill-dir>/scripts/log.py report --days 7
python3 <skill-dir>/scripts/log.py calibrate

# 一日菜单（读档案目标; 疾病约束: --disease 高血压|高尿酸|糖尿病, --avoid 内脏,香菜）
python3 <skill-dir>/scripts/plan.py --profile
```

## 默认份规则（calc.py 内置）

一碗饭/面 150g · 一盘配方菜 = 配方整份 · 肉/鱼一掌心 100g · 蛋 60g · 蔬菜一捧 150g · 水果 200g · 奶 250g · 粥/汤 300g · 油/坚果/调味 10g；炒菜类条目自动计入 10g 烹调用油（配方菜已含油不重复）

## 边界

- 疾病相关建议附「以上为营养估算，具体遵医嘱」；嘌呤数据低置信
- 误差预期：默认份 ±30%、配方为家常估算——减重看周趋势，不追求单日假精确
- 阈值参考（盐≤5g、油20-25g、添加糖≤25g）在 data/guidance.json，源自膳食指南 2022
