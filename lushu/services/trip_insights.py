"""把攻略结论挂到行程的天项上。

设计第 5.4 节把这件事说得很清楚，它是字段级分层采信（ADR-0001）的第二次生效：

- **硬约束注入 prompt**——开放时间、闭馆日、放票日、是否必须预约、坐标。
  模型需要这些才能排出可执行的行程。
- **软经验生成后挂载**——完整的避坑指南与打卡建议原文。
  **不注入**，这样 LLM 无法把它们当成自己写的内容混进行程正文。

所以这一层是**只读的挂载**：行程排完之后，把知识库里挂在这个景点上的结论
取出来贴上去。模型没见过这些文字，也就编不出「我在故宫拍到了没人的太和殿」。

一条取舍：**结论不进时间轴的正文**。时间轴的价值是可扫读——几点、去哪儿、
怎么走。把整段攻略原文铺进去，扫读就没了。所以界面上是「几个标记 + 展开」，
数量看得见，原文点开才读。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from lushu.domain.knowledge import Confidence
from lushu.services import knowledge_store as ks
from lushu.store.connection import connect


@dataclass(frozen=True)
class Insight:
    """一条挂在某个天项上的结论。"""

    claim_id: str
    text: str
    facet: str
    confidence: str
    independent_source_count: int
    evidence_count: int
    verify_due_at: str | None

    @property
    def single_source(self) -> bool:
        """单源的说法照样显示，但要让用户看得出它是单源。

        设计里说的是「标为待验证个例而不是丢弃」——单源经验仍有参考价值，
        只是必须让人知道自己看的是一个孤例。
        """
        return self.confidence != Confidence.HIGH.value


@dataclass(frozen=True)
class ItemInsights:
    """一个天项上的软经验。"""

    poi_id: str
    poi_name: str
    highlights: tuple[Insight, ...] = ()
    avoids: tuple[Insight, ...] = ()

    @property
    def total(self) -> int:
        return len(self.highlights) + len(self.avoids)


@dataclass
class TripInsights:
    """一份行程上的全部软经验。"""

    trip_id: str
    by_poi: dict[str, ItemInsights] = field(default_factory=dict)

    @property
    def covered_items(self) -> int:
        return len(self.by_poi)

    @property
    def total_claims(self) -> int:
        return sum(item.total for item in self.by_poi.values())

    def for_poi(self, poi_id: str | None) -> ItemInsights | None:
        return self.by_poi.get(poi_id) if poi_id else None


def _insight(claim: ks.ClaimRow) -> Insight:
    return Insight(
        claim_id=claim.claim_id,
        text=claim.text,
        facet=claim.facet,
        confidence=claim.confidence.value,
        independent_source_count=claim.independent_source_count,
        evidence_count=claim.evidence_count,
        verify_due_at=claim.verify_due_at,
    )


def trip_poi_ids(trip_id: str, *, conn: sqlite3.Connection) -> list[str]:
    """这份行程里对上了实体的天项，按出现顺序去重。

    没对上实体（`poi_id` 为空）的挂不上任何知识——那正是 M3 的对齐要解决的问题，
    界面上会用「待对齐」标出来，而不是在这里假装有内容。
    """
    rows = conn.execute(
        "SELECT DISTINCT di.poi_id AS poi_id "
        "FROM day_item di JOIN day d ON d.id = di.day_id "
        "WHERE d.trip_id = ? AND di.poi_id IS NOT NULL AND di.kind = 'poi'",
        (trip_id,),
    ).fetchall()
    return [row["poi_id"] for row in rows]


def insights_for_trip(
    trip_id: str, *, conn: sqlite3.Connection | None = None, max_per_kind: int = 8
) -> TripInsights:
    """这份行程里每个景点上挂着的结论。

    `max_per_kind` 是**显示**的上限，不是查询的上限——一条结论都不丢，
    只是界面上不一次铺完。超出的部分由界面自己说「另有 N 条」。
    """
    owned = conn is None
    active = conn or connect()
    result = TripInsights(trip_id=trip_id)
    try:
        poi_ids = trip_poi_ids(trip_id, conn=active)
        for poi_id in poi_ids:
            claims = ks.claims_for_poi(poi_id, conn=active)
            if not claims:
                continue
            row = active.execute(
                "SELECT name FROM poi WHERE amap_poi_id = ?", (poi_id,)
            ).fetchone()
            # 高置信的排前面：那是「多个人都这么说」的那些
            picks = sorted(
                claims,
                key=lambda item: (
                    item.confidence is not Confidence.HIGH,
                    -item.independent_source_count,
                ),
            )
            result.by_poi[poi_id] = ItemInsights(
                poi_id=poi_id,
                poi_name=(row["name"] if row else None) or poi_id,
                highlights=tuple(
                    _insight(item) for item in picks if item.polarity == "highlight"
                )[:max_per_kind],
                avoids=tuple(_insight(item) for item in picks if item.polarity == "avoid")[
                    :max_per_kind
                ],
            )
    finally:
        if owned:
            active.close()
    return result
