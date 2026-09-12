"""景点预约领域模型：预约规则、复核门禁与预约清单。

承载的规则：

1. **未经复核的规则不得展示**（Q10）。预约规则错一个字段，用户就会白跑一趟，
   这是本项目唯一会直接伤害用户的失败模式，所以草案状态是硬门禁。
2. **预约清单按距今剩余天数倒排**（Q11）。真正有用的不是「这个景点要预约」，
   而是「还有 3 天放票，现在就得盯着」。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

# 预约规则与门票开放时间类信息的复验周期
REVIEW_VALID_DAYS = 90

# 距放票日不足这么多天时标为紧急
URGENT_WITHIN_DAYS = 3


class RuleStatus(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"


class ChannelKind(StrEnum):
    WEB = "web"
    MINIAPP = "miniapp"
    OFFICIAL_ACCOUNT = "official_account"
    PHONE = "phone"


class Urgency(StrEnum):
    """预约清单的紧迫度。"""

    OVERDUE = "overdue"  # 放票日已过，得立刻去查还有没有票
    TODAY = "today"  # 今天放票
    SOON = "soon"  # 三天内放票
    LATER = "later"


@dataclass(frozen=True)
class Channel:
    """预约渠道。规则必须有渠道，否则用户知道要预约也无处可去。"""

    name: str
    kind: ChannelKind
    url: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("预约渠道必须有名称")


@dataclass(frozen=True)
class ClosedDays:
    """闭馆日。weekdays 用 0 表示周一。"""

    weekdays: tuple[int, ...] = ()
    ranges: tuple[tuple[date, date], ...] = ()

    def __post_init__(self) -> None:
        for wd in self.weekdays:
            if not 0 <= wd <= 6:
                raise ValueError(f"星期取值必须是 0 到 6，收到 {wd}")

    def covers(self, day: date) -> bool:
        if day.weekday() in self.weekdays:
            return True
        return any(start <= day <= end for start, end in self.ranges)


@dataclass(frozen=True)
class BookingRule:
    """一个景点关于提前预约的可执行约束。"""

    poi_id: str
    booking_required: bool
    status: RuleStatus = RuleStatus.DRAFT
    advance_days: int | None = None
    release_time: str | None = None  # 放票时点，如 "20:00"
    channels: tuple[Channel, ...] = ()
    requires_real_name: bool | None = None
    id_required_note: str | None = None
    closed_days: ClosedDays = ClosedDays()
    evidence_url: str | None = None
    reviewed_at: date | None = None
    reviewer_note: str | None = None

    def __post_init__(self) -> None:
        if self.booking_required and self.advance_days is None:
            raise ValueError("需要预约的景点必须给出提前天数，否则无法算放票日")
        if self.advance_days is not None and self.advance_days < 0:
            raise ValueError("提前天数不能为负")

    @property
    def verify_due_at(self) -> date | None:
        if self.reviewed_at is None:
            return None
        return self.reviewed_at + timedelta(days=REVIEW_VALID_DAYS)

    def is_due_for_review(self, today: date) -> bool:
        due = self.verify_due_at
        return due is not None and today >= due

    def release_date_for(self, visit_date: date) -> date | None:
        """针对某次游览日期，票是哪一天放出来的。"""
        if self.advance_days is None:
            return None
        return visit_date - timedelta(days=self.advance_days)


def is_visible(rule: BookingRule) -> bool:
    """规则是否可以展示给用户。未经复核一律不可见。"""
    return rule.status is RuleStatus.REVIEWED


@dataclass(frozen=True)
class PlannedVisit:
    """行程里的一次景点游览。"""

    poi_id: str
    poi_name: str
    visit_date: date
    city_name: str | None = None


@dataclass(frozen=True)
class BookingAlert:
    """预约清单上的一条。"""

    poi_id: str
    poi_name: str
    visit_date: date
    release_date: date | None
    days_until_release: int | None
    urgency: Urgency
    release_time: str | None
    channels: tuple[Channel, ...]
    requires_real_name: bool | None
    city_name: str | None = None

    @property
    def headline(self) -> str:
        """给用户看的一行结论。"""
        if self.release_date is None:
            return "需要预约，但放票规则待补齐"
        if self.urgency is Urgency.OVERDUE:
            return f"放票日已过 {abs(self.days_until_release or 0)} 天，立刻确认是否还有票"
        if self.urgency is Urgency.TODAY:
            when = f" {self.release_time}" if self.release_time else ""
            return f"今天{when} 放票"
        return f"{self.days_until_release} 天后放票"


def _urgency_for(days_until_release: int | None) -> Urgency:
    if days_until_release is None:
        return Urgency.LATER
    if days_until_release < 0:
        return Urgency.OVERDUE
    if days_until_release == 0:
        return Urgency.TODAY
    if days_until_release <= URGENT_WITHIN_DAYS:
        return Urgency.SOON
    return Urgency.LATER


def build_alert_list(
    rules: Iterable[BookingRule],
    visits: Sequence[PlannedVisit],
    today: date,
) -> list[BookingAlert]:
    """从规则与行程推出预约清单，按距今剩余天数倒排。

    只包含需要预约、且规则已复核的景点。草案状态的规则被静默跳过——
    宁可少提醒，也不能用未复核的规则让用户白跑一趟。
    """
    by_poi = {rule.poi_id: rule for rule in rules}
    alerts: list[BookingAlert] = []

    for visit in visits:
        rule = by_poi.get(visit.poi_id)
        if rule is None or not rule.booking_required or not is_visible(rule):
            continue

        release_date = rule.release_date_for(visit.visit_date)
        days_until = (release_date - today).days if release_date is not None else None
        alerts.append(
            BookingAlert(
                poi_id=visit.poi_id,
                poi_name=visit.poi_name,
                visit_date=visit.visit_date,
                release_date=release_date,
                days_until_release=days_until,
                urgency=_urgency_for(days_until),
                release_time=rule.release_time,
                channels=rule.channels,
                requires_real_name=rule.requires_real_name,
                city_name=visit.city_name,
            )
        )

    # 倒排：放票日越近越靠前；没有放票日的排最后
    alerts.sort(key=lambda a: (a.days_until_release is None, a.days_until_release or 0))
    return alerts
