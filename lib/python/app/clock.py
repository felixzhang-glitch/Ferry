from __future__ import annotations

from datetime import datetime

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_PERIODS = ((4, "凌晨"), (8, "早上"), (10, "上午"), (13, "中午"), (17, "下午"), (22, "晚上"))

_DIRECTIVE = (
    "时段词（现在/今天/今晚/明早/周末）一律以此为准；"
    "括号里的时段直接用，不要自己换算，也不要沿用会话历史里更早的时间。"
)


def period(hour: int) -> str:
    for end, label in _PERIODS:
        if hour <= end:
            return label
    return "深夜"


def time_context(now: datetime | None = None) -> str:
    """The one clock block every backend injects.

    时段 is pre-computed rather than left to the model: given a bare
    `11:45:24` it answered "今晚陪你到这儿" at midday.
    """
    now = now or datetime.now()
    stamp = f"{now:%Y-%m-%d %H:%M} {WEEKDAYS[now.weekday()]}（{period(now.hour)}）"
    return f"当前系统时间: {stamp}\n{_DIRECTIVE}"
