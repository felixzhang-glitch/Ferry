"""时段是算出来的，不是让模型推的。

2026-09-06 11:45（周日中午）注入的时间戳完全正确，模型却回了"今晚陪你到这儿"
"今天早点休息"。日期没错，错在从 HH:MM:SS 推时段这一步，所以时段改由代码给出。

2026-09-15 12:24 的注入完全正确，模型却把"明天生日"复读成了历史里的"下周三"
（9/11 周五时正确）。所以昨天/明天/后天、本周/下周范围也由代码预计算。
"""

from datetime import datetime

import pytest

from app.clock import period, time_context


@pytest.mark.parametrize(
    "hour,label",
    [
        (0, "凌晨"),
        (4, "凌晨"),
        (5, "早上"),
        (8, "早上"),
        (9, "上午"),
        (10, "上午"),
        (11, "中午"),
        (13, "中午"),
        (14, "下午"),
        (17, "下午"),
        (18, "晚上"),
        (22, "晚上"),
        (23, "深夜"),
    ],
)
def test_period_covers_every_hour(hour: int, label: str) -> None:
    assert period(hour) == label


def test_stamp_carries_weekday_and_a_precomputed_period() -> None:
    line = time_context(datetime(2026, 9, 6, 11, 45, 30)).splitlines()[0]

    assert line == "当前系统时间: 2026-09-06 11:45 周日（中午）"


def test_directive_points_at_this_turn_only() -> None:
    text = time_context(datetime(2026, 9, 6, 11, 45))

    assert "不要自己换算或心算" in text
    # 9/15 生日事件：历史里的旧时间戳与"下周三"这类相对说法必须作废
    assert "都已过期，不得复读" in text


def test_relative_dates_are_precomputed() -> None:
    lines = time_context(datetime(2026, 9, 15, 12, 24)).splitlines()

    assert lines[1] == "相对日期: 昨天 9/14 周一 | 明天 9/16 周三 | 后天 9/17 周四"
    assert lines[2] == "本周: 9/14 周一 ~ 9/20 周日 | 下周: 9/21 周一 ~ 9/27 周日"


def test_relative_dates_cross_month_and_week_boundaries() -> None:
    # 2026-08-30 是周日：明天跨月，下周整段跨月
    lines = time_context(datetime(2026, 8, 30, 10, 0)).splitlines()

    assert lines[1] == "相对日期: 昨天 8/29 周六 | 明天 8/31 周一 | 后天 9/1 周二"
    assert lines[2] == "本周: 8/24 周一 ~ 8/30 周日 | 下周: 8/31 周一 ~ 9/6 周日"


def test_defaults_to_now() -> None:
    assert time_context().startswith(f"当前系统时间: {datetime.now():%Y-%m-%d}")
