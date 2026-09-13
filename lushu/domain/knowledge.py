"""知识领域模型：攻略结论、证据与置信度。

承载的规则：

1. **三层结论共用同一形状**，区别只在主体类型（Q26）
2. **置信度按独立来源数决定**，不按素材篇数决定（Q34）——
   一篇爆款被十个站转载仍然只是一个来源
3. **复验周期按结论类型分级**，到期标为待复验而不是隐藏（Q49）
4. **没有溯源的结论不得入库**，因此证据是结论的必填伴侣
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

# 达到这个独立来源数才算多源交叉验证通过。
# 低于它的结论标为「待验证个例」而不是被丢弃——单源经验仍有参考价值，
# 只是必须让用户知道自己正在看一条未经验证的信息。
MIN_INDEPENDENT_SOURCES = 3


def count_independent_sources(sources: Iterable[tuple[str, str | None]]) -> int:
    """几个独立来源 = 几个**不同的来源组**；没有组的素材按自身算一个。

    **这是 Q34 的唯一判据。** 界面、路书、命令行里那个「N 个独立来源」都由
    它算出来，所以只能有这一份实现——两份实现会各按各的口径算（一份按素材
    篇数、一份按来源组），而两边都自称「独立来源数」。

    入参是（素材 id，来源组 id）的序列，**组号必须由调用方现查**。归组按设计
    可以反复重跑（ADR-0008），所以「提纯那一刻把组号抄进证据行」得到的是一份
    会过期的快照：重跑归组之后那些快照全是旧的，按它算出来的置信度不会跟着
    变——而「按文字复制口径算出的高置信，第二层跑完之后要被降级」正是 ADR
    要求的动作。真库里 39 条证据行的组号全是 NULL，就是这么来的。
    """
    return len({group or document for document, group in sources})


class SubjectType(StrEnum):
    """结论的主体类型。"""

    POI = "poi"  # 景点级：故宫只有午门能进
    ROUTE = "route"  # 路线级：颐和园到圆明园别打车
    CITY = "city"  # 城市级：周一是西安各大博物馆集中闭馆日


class Polarity(StrEnum):
    """极性。避坑与打卡分开建模、分开呈现。"""

    AVOID = "avoid"
    HIGHLIGHT = "highlight"


class Facet(StrEnum):
    """结论针对的方面。"""

    QUEUE = "queue"  # 排队
    ENTRANCE = "entrance"  # 入口
    HOURS = "hours"  # 时段
    CROWD = "crowd"  # 人流
    PHOTO = "photo"  # 拍照
    TRANSIT = "transit"  # 交通
    PRICE_DIFF = "price_diff"  # 票价差异
    CLOSURE = "closure"  # 闭馆
    OTHER = "other"


class Confidence(StrEnum):
    HIGH = "high"
    SINGLE_SOURCE = "single_source"


# 复验周期（天）。None 表示这类结论不设期限。
# 分级依据：门票与开放时间随时会变；排队与人流是慢变量；
# 拍照机位这类信息几年都不会失效。
_REFRESH_DAYS: dict[Facet, int | None] = {
    Facet.PRICE_DIFF: 90,
    Facet.CLOSURE: 90,
    Facet.HOURS: 90,
    Facet.QUEUE: 180,
    Facet.CROWD: 180,
    Facet.TRANSIT: 180,
    Facet.ENTRANCE: 180,
    Facet.OTHER: 180,
    Facet.PHOTO: None,
}


def refresh_days_for(facet: Facet) -> int | None:
    """该类结论的复验周期，None 表示不设期限。"""
    return _REFRESH_DAYS.get(facet, 180)


def confidence_for(independent_source_count: int) -> Confidence:
    """按独立来源数定级。"""
    if independent_source_count >= MIN_INDEPENDENT_SOURCES:
        return Confidence.HIGH
    return Confidence.SINGLE_SOURCE


@dataclass(frozen=True)
class ClaimSubject:
    """结论的主体。按类型三选一，构造时即校验，避免出现半个主体。"""

    subject_type: SubjectType
    poi_id: str | None = None
    poi_a_id: str | None = None
    poi_b_id: str | None = None
    city_adcode: str | None = None

    def __post_init__(self) -> None:
        if self.subject_type is SubjectType.POI:
            if not self.poi_id:
                raise ValueError("景点级结论必须给出 poi_id")
            self._reject_extra(("poi_a_id", "poi_b_id", "city_adcode"))
        elif self.subject_type is SubjectType.ROUTE:
            if not (self.poi_a_id and self.poi_b_id):
                raise ValueError("路线级结论必须给出两个端点 poi_a_id 与 poi_b_id")
            if self.poi_a_id == self.poi_b_id:
                raise ValueError("路线级结论的两个端点不能是同一个景点")
            self._reject_extra(("poi_id", "city_adcode"))
        elif self.subject_type is SubjectType.CITY:
            if not self.city_adcode:
                raise ValueError("城市级结论必须给出 city_adcode")
            self._reject_extra(("poi_id", "poi_a_id", "poi_b_id"))

    def _reject_extra(self, names: tuple[str, ...]) -> None:
        present = [n for n in names if getattr(self, n)]
        if present:
            raise ValueError(f"{self.subject_type.value} 级结论不应带有 {'、'.join(present)}")


@dataclass(frozen=True)
class ClaimEvidence:
    """结论的证据。原文片段是进入路书的东西，全文只留在本地底档。"""

    source_document_id: str
    quote: str
    # **这是提纯那一刻抄下来的快照，不是计数的依据。** 归组按设计可以反复
    # 重跑（ADR-0008），所以这个值随时可能过时；算「几个独立来源」要现查
    # 素材表，见 `count_independent_sources`。留着它是为了溯源时看得出
    # 「这条引文当时被认为属于哪个来源」。
    source_group_id: str | None = None
    char_start: int | None = None
    char_end: int | None = None

    def __post_init__(self) -> None:
        if not self.quote.strip():
            raise ValueError("证据必须带原文片段，空引用等同于没有溯源")


@dataclass(frozen=True)
class Claim:
    """攻略知识库的最小单位。"""

    id: str
    subject: ClaimSubject
    polarity: Polarity
    facet: Facet
    text: str
    independent_source_count: int
    first_seen_at: date
    evidence: tuple[ClaimEvidence, ...] = field(default_factory=tuple)
    last_verified_at: date | None = None
    status: str = "active"

    def __post_init__(self) -> None:
        if not self.evidence:
            raise ValueError("结论必须至少有一条证据，无溯源的结论不得入库")
        if self.independent_source_count < 1:
            raise ValueError("独立来源数至少为 1")
        if not self.text.strip():
            raise ValueError("结论正文不能为空")

    @property
    def confidence(self) -> Confidence:
        return confidence_for(self.independent_source_count)

    @property
    def verify_due_at(self) -> date | None:
        """复验到期日。不设期限的类别返回 None。"""
        days = refresh_days_for(self.facet)
        if days is None:
            return None
        anchor = self.last_verified_at or self.first_seen_at
        return anchor + timedelta(days=days)

    def is_due_for_review(self, today: date) -> bool:
        due = self.verify_due_at
        return due is not None and today >= due

    @property
    def distinct_source_groups(self) -> int:
        """证据覆盖的独立来源数。

        注意它读的是证据上记的组号，也就是**提纯那一刻的快照**；要算「现在
        算几个来源」必须现查素材表，走 `count_independent_sources`。
        """
        return count_independent_sources(
            (e.source_document_id, e.source_group_id) for e in self.evidence
        )
