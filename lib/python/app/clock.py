from __future__ import annotations

from datetime import datetime, timedelta

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_PERIODS = ((4, "凌晨"), (8, "早上"), (10, "上午"), (13, "中午"), (17, "下午"), (22, "晚上"))

_DIRECTIVE = (
    "时段词（现在/今天/今晚/明早/周末）与相对日期（昨天/明天/后天/周几/下周X）"
    "一律以上面为准，直接用，不要自己换算或心算；"
    "会话历史里的时间戳与相对说法（含“下周三”“明天”）都已过期，不得复读或据此推算。"
)


def period(hour: int) -> str:
    for end, label in _PERIODS:
        if hour <= end:
            return label
    return "深夜"


def _day_stamp(day: datetime) -> str:
    return f"{day.month}/{day.day} {WEEKDAYS[day.weekday()]}"


def time_context(now: datetime | None = None) -> str:
    """The one clock block every backend injects.

    时段 is pre-computed rather than left to the model: given a bare
    `11:45:24` it answered "今晚陪你到这儿" at midday. 相对日期同理：
    2026-09-15 12:24 的注入完全正确，模型却把“明天生日”复读成了历史里的
    “下周三”（9/11 时正确）——所以昨天/明天/后天、本周/下周范围也由代码给出。
    """
    now = now or datetime.now()
    monday = now - timedelta(days=now.weekday())
    lines = [
        f"当前系统时间: {now:%Y-%m-%d %H:%M} {WEEKDAYS[now.weekday()]}（{period(now.hour)}）",
        (
            "相对日期: "
            f"昨天 {_day_stamp(now - timedelta(days=1))} | "
            f"明天 {_day_stamp(now + timedelta(days=1))} | "
            f"后天 {_day_stamp(now + timedelta(days=2))}"
        ),
        (
            f"本周: {_day_stamp(monday)} ~ {_day_stamp(monday + timedelta(days=6))} | "
            f"下周: {_day_stamp(monday + timedelta(days=7))} ~ {_day_stamp(monday + timedelta(days=13))}"
        ),
        _DIRECTIVE,
    ]
    return "\n".join(lines)
