"""把路段查成真实距离，写进天项。

路书里的路段说明原先全是直线估算。真实步行距离通常是直线的 1.3 倍，
**写着「步行 1200 米」而实际要走 1800 米**，正是设计里说的「现场会很意外」。

为什么不导出时现查：路书是确认后导出的只读交付物，导出这一步现在是纯粹的
读库（ADR-0006 的整套取舍都建立在「不必联网」上）。让导出去联网会把它变成
慢、不确定、网断了就导不出的操作。所以照 `ls trip coords` 与 `ls verify scan`
的先例：**先跑一条命令查好写进库，导出照旧只读库**。

`leg_to_item_id` 是自失效的键：它记下这条路段通向哪一项。行程一改
（插入、删除、重排），键就对不上，路书读的时候自然退回估算——不需要任何
「行程变了要清缓存」的额外记账，那种记账迟早会漏。同一天里同一对端点
重复出现（去而复返）时，键指向的仍是同一个 id，也就仍然有效，这是对的。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from lushu.adapters.route import RouteError, RoutePlan
from lushu.domain.geo import distance_m
from lushu.domain.roadbook import leg_fingerprint
from lushu.services.roadbook_service import WALK_LIMIT_M, item_sort_key
from lushu.store.connection import connect

# 高德 QPS 限制实测会撞，每次请求之间歇一下。与 `meal_coords` 同一个数。
PAUSE_SECONDS = 0.4


@dataclass(frozen=True)
class LegSlot:
    """一段待查的天项之间的路。"""

    trip_id: str
    day_id: str
    day_date: str
    from_item_id: str
    from_title: str
    from_lat: float
    from_lng: float
    to_item_id: str
    to_title: str
    to_lat: float
    to_lng: float
    # 直线距离。它同时决定查哪一个接口——**判据与估算器共用同一个阈值**，
    # 否则会出现「估算器说该走、路径规划说该打车」这种自相矛盾。
    straight_m: float = 0.0

    @property
    def walk(self) -> bool:
        return self.straight_m <= WALK_LIMIT_M


@dataclass(frozen=True)
class LegFix:
    """一段路的处置结果。"""

    slot: LegSlot
    plan: RoutePlan | None = None
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.plan is not None


@dataclass
class LegReport:
    fixes: list[LegFix] = field(default_factory=list)

    @property
    def resolved(self) -> list[LegFix]:
        return [fix for fix in self.fixes if fix.resolved]

    @property
    def unresolved(self) -> list[LegFix]:
        return [fix for fix in self.fixes if not fix.resolved]


def pending_legs(
    *, conn: sqlite3.Connection | None = None, trip_id: str | None = None
) -> list[LegSlot]:
    """还没有真实距离的路段。

    两端都要有坐标才算得出来（那是路书契约里的老规矩）。坐标按「先实体、
    后天项」取——历史行程的景点坐标在 `poi` 表里。

    已经有**有效**路段的跳过：`leg_to_item_id` 指向的正是下一项时才作数，
    行程改过就自动退回待办（键对不上了）。
    """
    owned = conn is None
    active = conn or connect()
    try:
        rows = active.execute(
            "SELECT i.id, i.day_id, i.seq, i.start_time, i.end_time, i.title, i.kind, "
            "       i.leg_mode, i.leg_distance_m, i.leg_duration_min, i.leg_key, "
            "       d.date, d.trip_id, "
            "       COALESCE(p.lat_gcj02, i.lat_gcj02) AS lat, "
            "       COALESCE(p.lng_gcj02, i.lng_gcj02) AS lng "
            "FROM day_item i "
            "JOIN day d ON d.id = i.day_id "
            "LEFT JOIN poi p ON p.amap_poi_id = i.poi_id "
            "WHERE (? IS NULL OR d.trip_id = ?) "
            "ORDER BY d.date, i.seq",
            (trip_id, trip_id),
        ).fetchall()
    finally:
        if owned:
            active.close()

    by_day: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_day.setdefault(row["day_id"], []).append(row)

    found: list[LegSlot] = []
    for day_rows in by_day.values():
        ordered = sorted(day_rows, key=item_sort_key)
        for current, following in zip(ordered, ordered[1:], strict=False):
            if current["lat"] is None or current["lng"] is None:
                continue
            if following["lat"] is None or following["lng"] is None:
                continue
            # 已经有**有效**路段就跳过。指纹覆盖两端坐标，所以坐标换过
            # （对齐把天项挪到别的实体上）也会自动退回待办。
            if current["leg_mode"] and current["leg_key"] == leg_fingerprint(
                from_lat=current["lat"],
                from_lng=current["lng"],
                to_lat=following["lat"],
                to_lng=following["lng"],
                to_ref=following["id"],
            ):
                continue
            found.append(
                LegSlot(
                    trip_id=current["trip_id"],
                    day_id=current["day_id"],
                    day_date=current["date"],
                    from_item_id=current["id"],
                    from_title=current["title"] or "（未命名）",
                    from_lat=current["lat"],
                    from_lng=current["lng"],
                    to_item_id=following["id"],
                    to_title=following["title"] or "（未命名）",
                    to_lat=following["lat"],
                    to_lng=following["lng"],
                    straight_m=distance_m(
                        current["lat"], current["lng"], following["lat"], following["lng"]
                    ),
                )
            )
    return found


def plan_legs(
    slots: Sequence[LegSlot],
    *,
    walk=None,
    drive=None,
    pause: float = PAUSE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> LegReport:
    """把每段路查一遍。只算不改。"""
    if walk is None or drive is None:
        from lushu.adapters.route import plan_drive, plan_walk

        walk = walk or (
            lambda slot: plan_walk(
                from_lat=slot.from_lat,
                from_lng=slot.from_lng,
                to_lat=slot.to_lat,
                to_lng=slot.to_lng,
            )
        )
        drive = drive or (
            lambda slot: plan_drive(
                from_lat=slot.from_lat,
                from_lng=slot.from_lng,
                to_lat=slot.to_lat,
                to_lng=slot.to_lng,
            )
        )

    fixes: list[LegFix] = []
    for index, slot in enumerate(slots):
        if index and pause:
            sleep(pause)
        planner = walk if slot.walk else drive
        try:
            fixes.append(LegFix(slot=slot, plan=planner(slot)))
        except RouteError as exc:
            # 只接「路径规划失败」。别的异常是我们的 bug，该炸就炸。
            fixes.append(LegFix(slot=slot, reason=str(exc)))
    return LegReport(fixes=fixes)


def apply_fixes(fixes: Sequence[LegFix], *, conn: sqlite3.Connection | None = None) -> list[str]:
    """把查到的路段写进**起点那一项**，返回改动的天项 id。

    写起点而不是终点：路段是「从这一项出发怎么去下一项」，挂在下一天项上
    就分不清「到达」与「出发」了。键里同时记下它通向哪一项（自失效）。
    """
    owned = conn is None
    active = conn or connect()
    changed: list[str] = []
    try:
        for fix in fixes:
            if not fix.resolved or fix.plan is None:
                continue
            active.execute(
                "UPDATE day_item SET leg_mode = ?, leg_distance_m = ?, leg_duration_min = ?, "
                "leg_key = ? WHERE id = ?",
                (
                    fix.plan.mode,
                    fix.plan.distance_m,
                    fix.plan.duration_min,
                    leg_fingerprint(
                        from_lat=fix.slot.from_lat,
                        from_lng=fix.slot.from_lng,
                        to_lat=fix.slot.to_lat,
                        to_lng=fix.slot.to_lng,
                        to_ref=fix.slot.to_item_id,
                    ),
                    fix.slot.from_item_id,
                ),
            )
            changed.append(fix.slot.from_item_id)
        active.commit()
        return changed
    finally:
        if owned:
            active.close()
