"""行程领域规则测试。

重点是 ADR-0004 的两条不变量：
  总天数 = 各城市停留天数之和
  天数列表的长度也等于它（转移不额外占天）
"""

from __future__ import annotations

from datetime import date

import pytest

from lushu.domain.trip import (
    LONG_TRANSFER_MINUTES,
    CityStay,
    TransferMode,
    default_transfer_day,
    plan_days,
    total_days,
)

START = date(2026, 10, 1)


def _bj_xa() -> list[CityStay]:
    """北京 3 天 + 西安 3 天，贯穿整个测试文件的基准场景。"""
    return [
        CityStay(id="cs-bj", city_adcode="110100", city_name="北京", seq=0, stay_days=3),
        CityStay(id="cs-xa", city_adcode="610100", city_name="西安", seq=1, stay_days=3),
    ]


def test_total_days_is_the_sum_of_stays() -> None:
    assert total_days(_bj_xa()) == 6


def test_day_count_equals_total_days() -> None:
    """核心不变量：转移不额外占天，所以天数与总天数必须严格相等。"""
    stays = _bj_xa()
    assert len(plan_days(stays, START)) == total_days(stays)


def test_days_are_consecutive_and_correctly_attributed() -> None:
    days = plan_days(_bj_xa(), START)

    assert [d.day_date for d in days] == [date(2026, 10, 1 + i) for i in range(6)]
    assert [d.city_stay_id for d in days] == ["cs-bj"] * 3 + ["cs-xa"] * 3
    assert [d.seq_in_stay for d in days] == [0, 1, 2, 0, 1, 2]
    assert [d.index for d in days] == list(range(6))


def test_stays_are_ordered_by_seq_not_input_order() -> None:
    shuffled = list(reversed(_bj_xa()))
    days = plan_days(shuffled, START)
    assert [d.city_stay_id for d in days] == ["cs-bj"] * 3 + ["cs-xa"] * 3


def test_empty_stays_produce_no_days() -> None:
    assert plan_days([], START) == []
    assert total_days([]) == 0


def test_single_city_trip_has_no_transfer_question() -> None:
    stays = [CityStay(id="cs-bj", city_adcode="110100", city_name="北京", seq=0, stay_days=4)]
    days = plan_days(stays, START)
    assert len(days) == 4
    assert all(d.city_stay_id == "cs-bj" for d in days)


# ─── 城际转移的落点（Q45）─────────────────────────────────────


def test_short_transfer_defaults_to_last_day_of_departure_city() -> None:
    stays = _bj_xa()
    days = plan_days(stays, START)
    # 200 分钟短于 240 分钟的阈值，属于「傍晚出发」这一类
    placement = default_transfer_day(days, stays[0], stays[1], duration_min=200, mode=TransferMode.RAIL)

    # 第 3 天是北京的最后一天，即索引 2
    assert placement.day_index == 2
    assert days[placement.day_index].city_stay_id == "cs-bj"


def test_transfer_at_exactly_the_threshold_still_stays_in_departure_city() -> None:
    """阈值取的是「超过」，正好等于阈值不算长车程。"""
    stays = _bj_xa()
    days = plan_days(stays, START)
    placement = default_transfer_day(
        days, stays[0], stays[1], duration_min=LONG_TRANSFER_MINUTES, mode=TransferMode.RAIL
    )
    assert placement.day_index == 2


def test_long_transfer_moves_to_first_day_of_arrival_city() -> None:
    stays = _bj_xa()
    days = plan_days(stays, START)
    placement = default_transfer_day(
        days, stays[0], stays[1], duration_min=LONG_TRANSFER_MINUTES + 60, mode=TransferMode.RAIL
    )

    assert placement.day_index == 3
    assert days[placement.day_index].city_stay_id == "cs-xa"


def test_transfer_by_air_always_moves_to_arrival_city() -> None:
    """飞机即使是短程也要算上往返机场的时间，一律建议落在到达城市第一天。"""
    stays = _bj_xa()
    days = plan_days(stays, START)
    placement = default_transfer_day(days, stays[0], stays[1], duration_min=120, mode=TransferMode.AIR)

    assert placement.day_index == 3
    assert days[placement.day_index].city_stay_id == "cs-xa"


def test_transfer_placement_explains_itself() -> None:
    stays = _bj_xa()
    days = plan_days(stays, START)
    normal = default_transfer_day(days, stays[0], stays[1], duration_min=270)
    long = default_transfer_day(days, stays[0], stays[1], duration_min=600)

    assert normal.reason and long.reason
    assert normal.reason != long.reason
    assert normal.is_default and long.is_default


def test_duration_unknown_falls_back_to_departure_city() -> None:
    stays = _bj_xa()
    days = plan_days(stays, START)
    placement = default_transfer_day(days, stays[0], stays[1], duration_min=None)
    assert placement.day_index == 2


def test_transfer_requires_both_ends_in_the_trip() -> None:
    stays = _bj_xa()
    days = plan_days(stays, START)
    outsider = CityStay(id="cs-cd", city_adcode="510100", city_name="成都", seq=2, stay_days=2)
    with pytest.raises(ValueError):
        default_transfer_day(days, stays[0], outsider)


# ─── 构造校验 ────────────────────────────────────────────────


@pytest.mark.parametrize("bad_days", [0, -1])
def test_stay_days_must_be_at_least_one(bad_days: int) -> None:
    with pytest.raises(ValueError):
        CityStay(id="x", city_adcode="110100", city_name="北京", seq=0, stay_days=bad_days)


def test_seq_cannot_be_negative() -> None:
    with pytest.raises(ValueError):
        CityStay(id="x", city_adcode="110100", city_name="北京", seq=-1, stay_days=1)
