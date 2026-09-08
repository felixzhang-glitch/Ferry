"""时段是算出来的，不是让模型推的。

2026-09-06 11:45（周日中午）注入的时间戳完全正确，模型却回了"今晚陪你到这儿"
"今天早点休息"。日期没错，错在从 HH:MM:SS 推时段这一步，所以时段改由代码给出。
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

    assert "不要自己换算" in text
    # pi 的老 transcript 里还躺着改动前累积的几百个旧时间戳
    assert "不要沿用会话历史里更早的时间" in text


def test_defaults_to_now() -> None:
    assert time_context().startswith(f"当前系统时间: {datetime.now():%Y-%m-%d}")
