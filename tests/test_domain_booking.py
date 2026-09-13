"""预约领域规则测试。

重点是复核门禁：未经复核的规则绝不能出现在用户面前。
预约信息错一个字段就会让人白跑一趟，这是本项目唯一会直接伤害用户的失败模式。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from lushu.domain.booking import (
    REVIEW_VALID_DAYS,
    BookingRule,
    Channel,
    ChannelKind,
    ClosedDays,
    PlannedVisit,
    RuleStatus,
    Urgency,
    build_alert_list,
    is_visible,
)

TODAY = date(2026, 9, 12)
GUGONG = "B000A8UIN8"


def _rule(**overrides) -> BookingRule:
    base: dict = dict(
        poi_id=GUGONG,
        booking_required=True,
        status=RuleStatus.REVIEWED,
        advance_days=7,
        release_time="20:00",
        channels=(
            Channel(
                name="故宫博物院观众服务",
                kind=ChannelKind.MINIAPP,
                url="https://gugong.ktmtech.cn",
            ),
        ),
        requires_real_name=True,
        reviewed_at=date(2026, 9, 1),
    )
    base.update(overrides)
    return BookingRule(**base)


def _visit(days_ahead: int = 20, poi_id: str = GUGONG, name: str = "故宫博物院") -> PlannedVisit:
    return PlannedVisit(
        poi_id=poi_id,
        poi_name=name,
        visit_date=TODAY + timedelta(days=days_ahead),
        city_name="北京",
    )


# ─── 复核门禁 ────────────────────────────────────────────────


def test_draft_rules_are_not_visible() -> None:
    assert not is_visible(_rule(status=RuleStatus.DRAFT))


def test_reviewed_rules_are_visible() -> None:
    assert is_visible(_rule())


def test_draft_rules_never_reach_the_alert_list() -> None:
    """宁可少提醒，也不能用未复核的规则让用户白跑一趟。"""
    assert build_alert_list([_rule(status=RuleStatus.DRAFT)], [_visit()], TODAY) == []


def test_reviewed_rules_do_reach_the_alert_list() -> None:
    alerts = build_alert_list([_rule()], [_visit()], TODAY)
    assert len(alerts) == 1
    assert alerts[0].poi_name == "故宫博物院"


# ─── 放票日与紧迫度 ──────────────────────────────────────────


def test_release_date_is_visit_date_minus_advance_days() -> None:
    rule = _rule(advance_days=7)
    assert rule.release_date_for(date(2026, 10, 1)) == date(2026, 9, 24)


def test_days_until_release_is_counted_from_today() -> None:
    # 游览日 10 月 1 日，提前 7 天放票 → 放票日是 9 月 24 日，距 9 月 12 日还有 12 天
    alerts = build_alert_list([_rule(advance_days=7)], [_visit(19)], TODAY)
    assert alerts[0].visit_date == date(2026, 10, 1)
    assert alerts[0].release_date == date(2026, 9, 24)
    assert alerts[0].days_until_release == 12
    assert alerts[0].urgency is Urgency.LATER


def test_release_today_is_marked_today() -> None:
    # 提前 7 天、今天放票 → 游览日必须是 7 天后
    alerts = build_alert_list([_rule(advance_days=7)], [_visit(7)], TODAY)
    assert alerts[0].days_until_release == 0
    assert alerts[0].urgency is Urgency.TODAY
    assert "今天" in alerts[0].headline


def test_release_time_appears_in_the_headline() -> None:
    alerts = build_alert_list([_rule(advance_days=7, release_time="20:00")], [_visit(7)], TODAY)
    assert "20:00" in alerts[0].headline


@pytest.mark.parametrize(
    ("days_ahead", "expected_days", "expected_urgency"),
    [
        (9, 2, Urgency.SOON),
        (10, 3, Urgency.SOON),
        (11, 4, Urgency.LATER),
    ],
)
def test_urgency_thresholds(days_ahead: int, expected_days: int, expected_urgency: Urgency) -> None:
    alerts = build_alert_list([_rule(advance_days=7)], [_visit(days_ahead)], TODAY)
    assert alerts[0].days_until_release == expected_days
    assert alerts[0].urgency is expected_urgency


def test_passed_release_date_is_overdue() -> None:
    alerts = build_alert_list([_rule(advance_days=7)], [_visit(5)], TODAY)
    assert alerts[0].days_until_release == -2
    assert alerts[0].urgency is Urgency.OVERDUE
    assert "已过" in alerts[0].headline


def test_visible_alerts_always_carry_a_release_date() -> None:
    """不变量：能出现在清单上的规则都已复核，而已复核的必预约景点必须有提前天数。

    因此 `release_date` 在实践中不会为 None；`headline` 里的兜底分支是防御性的。
    """
    alerts = build_alert_list([_rule(), _rule(poi_id="p2")], [_visit(), _visit(poi_id="p2")], TODAY)
    assert alerts
    assert all(a.release_date is not None for a in alerts)
    assert all(a.days_until_release is not None for a in alerts)


# ─── 排序与过滤 ──────────────────────────────────────────────


def test_alerts_are_sorted_by_nearest_release_first() -> None:
    far = _rule(poi_id="p-far", advance_days=30)
    near = _rule(poi_id="p-near", advance_days=3)
    visits = [
        PlannedVisit(poi_id="p-far", poi_name="远", visit_date=date(2026, 12, 1)),
        PlannedVisit(poi_id="p-near", poi_name="近", visit_date=date(2026, 9, 20)),
    ]
    alerts = build_alert_list([far, near], visits, TODAY)
    assert [a.poi_id for a in alerts] == ["p-near", "p-far"]


def test_places_without_a_booking_requirement_are_skipped() -> None:
    rule = _rule(booking_required=False, advance_days=None)
    assert build_alert_list([rule], [_visit()], TODAY) == []


def test_places_without_a_rule_are_skipped() -> None:
    assert build_alert_list([], [_visit()], TODAY) == []


def test_visits_not_in_the_rules_are_skipped() -> None:
    assert build_alert_list([_rule()], [_visit(poi_id="别的景点")], TODAY) == []


# ─── 构造校验 ────────────────────────────────────────────────


def test_booking_required_without_advance_days_is_allowed() -> None:
    """「需要预约」与「算得出放票日」是两件事。

    实测 17 个必须预约的知名景点里有 12 个的官方页面只说「须提前线上预约」
    而从不公布放票天数与时刻（兵马俑流传的四种说法互相矛盾且都无官方出处）。
    这种规则仍然有用：它告诉用户「这里必须预约、去哪儿约」，
    而那正是最怕白跑的一件事。
    """
    rule = BookingRule(poi_id="p1", booking_required=True, advance_days=None)
    assert rule.has_release_window is False
    assert rule.release_date_for(date(2026, 10, 1)) is None


def test_incomplete_rule_still_says_what_to_do() -> None:
    from lushu.domain.booking import Channel, ChannelKind, PlannedVisit, build_alert_list

    rule = BookingRule(
        poi_id="p1",
        booking_required=True,
        status=RuleStatus.REVIEWED,
        advance_days=None,
        channels=(Channel("官方公众号", ChannelKind.OFFICIAL_ACCOUNT),),
        reviewed_at=TODAY,
    )
    alerts = build_alert_list(
        [rule], [PlannedVisit(poi_id="p1", poi_name="某博物馆", visit_date=date(2026, 10, 1))], TODAY
    )

    assert len(alerts) == 1
    # 不写「待补齐」——那不是我们忘了填，是官方没公布
    assert "官方未公布" in alerts[0].headline
    assert alerts[0].channels


def test_negative_advance_days_is_rejected() -> None:
    with pytest.raises(ValueError, match="不能为负"):
        BookingRule(poi_id="p1", booking_required=True, advance_days=-1)


def test_rule_without_booking_requirement_needs_no_advance_days() -> None:
    assert BookingRule(poi_id="p1", booking_required=False).advance_days is None


def test_channel_requires_a_name() -> None:
    with pytest.raises(ValueError, match="名称"):
        Channel(name="  ", kind=ChannelKind.WEB)


# ─── 复核有效期 ──────────────────────────────────────────────


def test_review_expiry_is_ninety_days_after_review() -> None:
    rule = _rule(reviewed_at=date(2026, 1, 1))
    assert rule.verify_due_at == date(2026, 1, 1) + timedelta(days=REVIEW_VALID_DAYS)


def test_review_expiry_has_a_boundary() -> None:
    rule = _rule(reviewed_at=date(2026, 1, 1))
    assert not rule.is_due_for_review(date(2026, 3, 31))
    assert rule.is_due_for_review(date(2026, 4, 1))


def test_unreviewed_rule_has_no_expiry() -> None:
    assert _rule(status=RuleStatus.DRAFT, reviewed_at=None).verify_due_at is None


# ─── 闭馆日 ──────────────────────────────────────────────────


def test_closed_days_match_weekday() -> None:
    # 2026 年 9 月 14 日是星期一
    closed = ClosedDays(weekdays=(0,))
    assert closed.covers(date(2026, 9, 14))
    assert not closed.covers(date(2026, 9, 15))


def test_closed_days_match_date_range() -> None:
    closed = ClosedDays(ranges=((date(2026, 9, 1), date(2026, 9, 10)),))
    assert closed.covers(date(2026, 9, 5))
    assert not closed.covers(date(2026, 9, 11))


def test_invalid_weekday_is_rejected() -> None:
    with pytest.raises(ValueError, match="星期"):
        ClosedDays(weekdays=(7,))
