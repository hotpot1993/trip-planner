"""领域层：实体与规则。

本层不依赖任何框架、不依赖存储、不做 IO。它回答的是
「什么是一个合法的行程」「总天数怎么算」「转移落在哪天」
「一条结论凭什么算可信」这类与框架无关的问题。
"""

from .booking import BookingAlert, BookingRule, build_alert_list, is_visible
from .knowledge import (
    MIN_INDEPENDENT_SOURCES,
    Claim,
    ClaimEvidence,
    ClaimSubject,
    Confidence,
    Facet,
    Polarity,
    SubjectType,
    confidence_for,
    refresh_days_for,
)
from .planned import (
    ItemKind,
    PlannedDay,
    PlannedItem,
    PlannedStay,
    PlannedTrip,
    PoiFacts,
    StaySpec,
    lay_out,
)
from .trip import (
    LONG_TRANSFER_MINUTES,
    CalendarDay,
    CityStay,
    TransferMode,
    TransferPlacement,
    default_transfer_day,
    plan_days,
    total_days,
)

__all__ = [
    "LONG_TRANSFER_MINUTES",
    "MIN_INDEPENDENT_SOURCES",
    "BookingAlert",
    "BookingRule",
    "CalendarDay",
    "CityStay",
    "Claim",
    "ClaimEvidence",
    "ClaimSubject",
    "Confidence",
    "Facet",
    "ItemKind",
    "PlannedDay",
    "PlannedItem",
    "PlannedStay",
    "PlannedTrip",
    "PoiFacts",
    "Polarity",
    "StaySpec",
    "SubjectType",
    "TransferMode",
    "TransferPlacement",
    "build_alert_list",
    "confidence_for",
    "default_transfer_day",
    "is_visible",
    "lay_out",
    "plan_days",
    "refresh_days_for",
    "total_days",
]
