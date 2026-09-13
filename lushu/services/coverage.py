"""这份行程里，有多少地方是网友真的推荐过的。

设计第 5.1 节的验收是「打开城市看到的是网友推荐而非一片 POI」；
这一层把同一个问题问到**行程**上：排进去的这些地方，哪些有人写过、
哪些只是地图上恰好有。

为什么值得单独做一层。设计里有一条封闭世界约束——「排程不得引入候选池
之外的景点，由复审环节硬性检查」。而候选池**还没有接进排程的景点搜索**
（那要改 vendored 的 `attraction_search_node`），所以现在排出来的行程，
景点仍然来自高德搜索。这时硬性拒绝整份行程是没用的——它会把每一份行程
都毙掉。有用的是**如实标出来**：让用户看见「这 8 个地方有人推荐过，
那 3 个只是地图上有」。

这条信息本身就是产品价值的一部分：一个只是地图上有的地方，
没有任何人说过它值得去，也不知道要预约、什么时候去、注意什么。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from lushu.services import candidate_pool
from lushu.store.connection import connect


@dataclass(frozen=True)
class ItemCoverage:
    """一个排进行程的地方，以及有没有人推荐过它。"""

    poi_id: str
    title: str
    city_adcode: str | None
    city_name: str | None
    recommended: bool
    claim_count: int
    booking_required: bool | None

    @property
    def bare(self) -> bool:
        """只是地图上有——没有任何人写过它。"""
        return not self.recommended


@dataclass
class TripCoverage:
    """一份行程的覆盖情况。"""

    trip_id: str
    items: list[ItemCoverage] = field(default_factory=list)
    # 没有对上实体的天项。它们连「有没有人推荐过」都判不了，
    # 得让用户看见——那正是 M3 的对齐要解决的问题。
    unresolved: int = 0

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def recommended(self) -> int:
        return sum(1 for item in self.items if item.recommended)

    @property
    def bare_items(self) -> list[ItemCoverage]:
        return [item for item in self.items if item.bare]

    @property
    def ratio(self) -> float | None:
        """网友推荐过的比例。一条都对不上实体时算不出来，不是 0。"""
        return self.recommended / self.total if self.total else None


def _city_of(conn: sqlite3.Connection, trip_id: str, poi_id: str) -> tuple[str | None, str | None]:
    """这个天项属于哪座城市——按它落在哪一天、那一天属于哪个城市停留推。"""
    row = conn.execute(
        "SELECT cs.city_adcode AS adcode, cs.city_name AS name "
        "FROM day_item di JOIN day d ON d.id = di.day_id "
        "JOIN city_stay cs ON cs.id = d.city_stay_id "
        "WHERE d.trip_id = ? AND di.poi_id = ? LIMIT 1",
        (trip_id, poi_id),
    ).fetchone()
    return (row["adcode"], row["name"]) if row else (None, None)


def coverage_for_trip(
    trip_id: str, *, conn: sqlite3.Connection | None = None
) -> TripCoverage:
    """把行程里的每个景点拿候选池对一遍。

    候选池每座城市只算一次：一份五天的行程里同城的天项很多，
    每条都重算一遍候选池是白干活。
    """
    owned = conn is None
    active = conn or connect()
    result = TripCoverage(trip_id=trip_id)
    pools: dict[str, dict[str, candidate_pool.Candidate]] = {}
    try:
        rows = active.execute(
            "SELECT di.poi_id AS poi_id, COALESCE(di.title, p.name) AS title, "
            "  MIN(d.date) AS first_date "
            "FROM day_item di JOIN day d ON d.id = di.day_id "
            "LEFT JOIN poi p ON p.amap_poi_id = di.poi_id "
            "WHERE d.trip_id = ? AND di.kind = 'poi' AND di.poi_id IS NOT NULL "
            "GROUP BY di.poi_id, COALESCE(di.title, p.name) "
            "ORDER BY first_date",
            (trip_id,),
        ).fetchall()

        unresolved = active.execute(
            "SELECT COUNT(*) AS n FROM day_item di JOIN day d ON d.id = di.day_id "
            "WHERE d.trip_id = ? AND di.kind = 'poi' AND di.poi_id IS NULL",
            (trip_id,),
        ).fetchone()["n"]
        result.unresolved = unresolved

        for row in rows:
            poi_id = row["poi_id"]
            adcode, city_name = _city_of(active, trip_id, poi_id)
            if adcode and adcode not in pools:
                pools[adcode] = {
                    item.poi_id: item
                    for item in candidate_pool.city_candidates(adcode, conn=active)
                }
            candidate = pools.get(adcode or "", {}).get(poi_id)
            result.items.append(
                ItemCoverage(
                    poi_id=poi_id,
                    title=row["title"] or poi_id,
                    city_adcode=adcode,
                    city_name=city_name,
                    recommended=candidate is not None,
                    claim_count=candidate.claim_count if candidate else 0,
                    booking_required=candidate.booking_required if candidate else None,
                )
            )
    finally:
        if owned:
            active.close()
    return result
