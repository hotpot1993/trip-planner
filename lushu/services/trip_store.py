"""行程的落库与读取。

ADR-0007：**本项目的表是行程的唯一真相。** 引擎的产出经 `plan_converter`
转换后由本模块写入；引擎自己的 `itineraries` 表只作为生成过程的记录，
不参与读取。

写入是幂等的替换语义：重新规划会删掉原有的城市停留与天，再按新产出重建
（外键级联会把天项一并带走）。用户手工做过的调整因此会被覆盖，所以调用方
必须先弹「旧 → 新」差异让用户确认（Q48）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from lushu.domain.planned import (
    ItemKind,
    PlannedDay,
    PlannedItem,
    PlannedStay,
    PlannedTrip,
    PoiFacts,
)
from lushu.store.connection import connect
from lushu.store.ids import CITY_STAY, DAY, DAY_ITEM, TRIP, WORKBENCH, new_id


class UnresolvedCityError(RuntimeError):
    """城市没能解析到行政区划代码，无法落库。

    `city_stay.city_adcode` 有非空外键约束。城市名是人给的，行政区划代码
    必须以高德为准（ADR-0002），所以解析不到时宁可明确失败，也不要塞一个
    编造的代码进去——那会让天气、预算的城市归属全部错位。
    """

    def __init__(self, city_names: tuple[str, ...]) -> None:
        self.city_names = city_names
        super().__init__("以下城市没能解析到行政区划代码：" + "、".join(city_names))


@dataclass(frozen=True)
class TripSummary:
    """行程列表里的一行。"""

    id: str
    name: str
    start_date: date
    total_days: int
    status: str
    city_names: tuple[str, ...]
    updated_at: str


@dataclass(frozen=True)
class StoredTrip:
    """从表里读回来的完整行程。"""

    id: str
    name: str
    status: str
    created_at: str
    updated_at: str
    plan: PlannedTrip


# ─── 写入 ────────────────────────────────────────────────────


def save_planned_trip(draft: PlannedTrip, *, conn: sqlite3.Connection | None = None) -> str:
    """把规划产出存成一份新行程，返回行程标识。"""
    owned = conn is None
    active = conn or connect()
    try:
        with active:
            trip_id = _insert_trip(active, draft)
            _insert_plan(active, trip_id, draft)
            _queue_alignment_tasks(active, draft)
        return trip_id
    finally:
        if owned:
            active.close()


def replace_plan(
    trip_id: str, draft: PlannedTrip, *, conn: sqlite3.Connection | None = None
) -> None:
    """用新的规划产出替换行程内容，保留行程标识与创建时间。

    删除城市停留会连级联删掉它的天、天项与城际转移；`budget_item` 挂在
    行程上而不是城市停留上，因此预算不会被这一步抹掉。
    """
    _require_resolvable_cities(draft)

    owned = conn is None
    active = conn or connect()
    try:
        with active:
            row = active.execute("SELECT id FROM trip WHERE id = ?", (trip_id,)).fetchone()
            if row is None:
                raise LookupError(f"行程不存在：{trip_id}")

            active.execute("DELETE FROM city_stay WHERE trip_id = ?", (trip_id,))
            active.execute(
                "UPDATE trip SET name = ?, start_date = ?, query = ?, updated_at = ? WHERE id = ?",
                (draft.name, draft.start_date.isoformat(), draft.query or None, _now(), trip_id),
            )
            _insert_plan(active, trip_id, draft)
            _queue_alignment_tasks(active, draft)
    finally:
        if owned:
            active.close()


def delete_trip(trip_id: str, *, conn: sqlite3.Connection | None = None) -> bool:
    """删除行程。级联会带走它的城市停留、天与天项。"""
    owned = conn is None
    active = conn or connect()
    try:
        with active:
            cursor = active.execute("DELETE FROM trip WHERE id = ?", (trip_id,))
            return cursor.rowcount > 0
    finally:
        if owned:
            active.close()


def set_status(trip_id: str, status: str, *, conn: sqlite3.Connection | None = None) -> None:
    """改行程状态。确认后的行程才允许导出路书。"""
    if status not in {"draft", "confirmed"}:
        raise ValueError(f"未知的行程状态：{status}")

    owned = conn is None
    active = conn or connect()
    try:
        with active:
            active.execute(
                "UPDATE trip SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), trip_id),
            )
    finally:
        if owned:
            active.close()


# ─── 读取 ────────────────────────────────────────────────────


def load_trip(trip_id: str, *, conn: sqlite3.Connection | None = None) -> StoredTrip | None:
    """读回一份完整行程。存进去什么形状，读出来就是什么形状。"""
    owned = conn is None
    active = conn or connect()
    try:
        trip_row = active.execute(
            "SELECT id, name, start_date, status, query, created_at, updated_at "
            "FROM trip WHERE id = ?",
            (trip_id,),
        ).fetchone()
        if trip_row is None:
            return None

        stays = _load_stays(active, trip_row)
        return StoredTrip(
            id=trip_row["id"],
            name=trip_row["name"],
            status=trip_row["status"],
            created_at=trip_row["created_at"],
            updated_at=trip_row["updated_at"],
            plan=PlannedTrip(
                name=trip_row["name"],
                start_date=date.fromisoformat(trip_row["start_date"]),
                stays=stays,
                query=trip_row["query"] or "",
            ),
        )
    finally:
        if owned:
            active.close()


def list_trips(*, conn: sqlite3.Connection | None = None, limit: int = 50) -> list[TripSummary]:
    """列出全部行程，最近更新的在前。"""
    owned = conn is None
    active = conn or connect()
    try:
        rows = active.execute(
            "SELECT t.id, t.name, t.start_date, t.status, t.updated_at, "
            "       (SELECT COUNT(*) FROM day d WHERE d.trip_id = t.id) AS total_days, "
            "       (SELECT GROUP_CONCAT(cs.city_name, '、') FROM city_stay cs "
            "        WHERE cs.trip_id = t.id ORDER BY cs.seq) AS city_names "
            "FROM trip t ORDER BY t.updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            TripSummary(
                id=row["id"],
                name=row["name"],
                start_date=date.fromisoformat(row["start_date"]),
                total_days=int(row["total_days"] or 0),
                status=row["status"],
                city_names=tuple((row["city_names"] or "").split("、")) if row["city_names"] else (),
                updated_at=row["updated_at"],
            )
            for row in rows
        ]
    finally:
        if owned:
            active.close()


# ─── 内部：写入 ──────────────────────────────────────────────


def _insert_trip(conn: sqlite3.Connection, draft: PlannedTrip) -> str:
    _require_resolvable_cities(draft)
    now = _now()
    trip_id = new_id(TRIP)
    conn.execute(
        "INSERT INTO trip (id, name, start_date, status, query, created_at, updated_at) "
        "VALUES (?, ?, ?, 'draft', ?, ?, ?)",
        (trip_id, draft.name, draft.start_date.isoformat(), draft.query or None, now, now),
    )
    return trip_id


def _require_resolvable_cities(draft: PlannedTrip) -> None:
    missing = tuple(s.city_name for s in draft.stays if not s.city_adcode)
    if missing:
        raise UnresolvedCityError(missing)


def _insert_plan(conn: sqlite3.Connection, trip_id: str, draft: PlannedTrip) -> None:
    """插入城市停留、天与天项。城市与 POI 的硬事实顺带 upsert。"""
    for stay in draft.stays:
        stay_id = new_id(CITY_STAY)
        _upsert_city(conn, stay.city_name, stay.city_adcode or "")

        conn.execute(
            "INSERT INTO city_stay (id, trip_id, city_adcode, city_name, seq, stay_days) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (stay_id, trip_id, stay.city_adcode, stay.city_name, stay.seq, stay.stay_days),
        )

        for day in stay.days:
            day_id = new_id(DAY)
            conn.execute(
                "INSERT INTO day (id, trip_id, city_stay_id, date, seq_in_stay, theme) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (day_id, trip_id, stay_id, day.day.isoformat(), day.seq_in_stay, day.theme),
            )

            for seq, item in enumerate(day.items):
                if item.poi_id and item.facts:
                    _upsert_poi(conn, item.poi_id, item.title, stay.city_adcode or "", item.facts)
                conn.execute(
                    "INSERT INTO day_item "
                    "(id, day_id, seq, kind, poi_id, title, start_time, end_time, note, origin) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ai')",
                    (
                        new_id(DAY_ITEM),
                        day_id,
                        seq,
                        item.kind.value,
                        item.poi_id,
                        item.title,
                        item.start_time,
                        item.end_time,
                        item.note,
                    ),
                )


def _upsert_city(conn: sqlite3.Connection, name: str, adcode: str) -> None:
    """城市是实体真源的一部分（ADR-0002）。已存在时不覆盖坐标，只更新时间戳。"""
    conn.execute(
        "INSERT INTO city (adcode, name, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(adcode) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at",
        (adcode, name, _now()),
    )


def _upsert_poi(
    conn: sqlite3.Connection,
    poi_id: str,
    name: str,
    city_adcode: str,
    facts: PoiFacts,
) -> None:
    """写入或刷新一个 POI 的硬事实。

    坐标是 GCJ-02（ADR-0003），字段名带后缀。`raw_json` 留空——
    这里存的是已经结构化的硬事实，不需要再留一份原始响应。
    """
    conn.execute(
        "INSERT INTO poi (amap_poi_id, name, city_adcode, address, lat_gcj02, lng_gcj02, "
        "                 tel, rating, open_time, photo_url, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(amap_poi_id) DO UPDATE SET "
        "  name = excluded.name, city_adcode = excluded.city_adcode, address = excluded.address, "
        "  lat_gcj02 = excluded.lat_gcj02, lng_gcj02 = excluded.lng_gcj02, tel = excluded.tel, "
        "  rating = excluded.rating, open_time = excluded.open_time, "
        "  photo_url = excluded.photo_url, fetched_at = excluded.fetched_at",
        (
            poi_id,
            name,
            city_adcode or None,
            facts.address,
            facts.lat_gcj02,
            facts.lng_gcj02,
            facts.tel,
            facts.rating,
            facts.open_time,
            facts.photo,
            _now(),
        ),
    )


def _queue_alignment_tasks(conn: sqlite3.Connection, draft: PlannedTrip) -> int:
    """把没能对上实体真源的景点送进待对齐队列（Q19）。

    同一个「提及名 + 城市」只入队一次，重新规划不会堆出重复任务。
    """
    queued = 0
    for stay in draft.stays:
        for day in stay.days:
            for item in day.items:
                if not item.unresolved_name:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM alignment_task "
                    "WHERE mention_name = ? AND IFNULL(city_adcode, '') = ? AND status = 'pending' "
                    "LIMIT 1",
                    (item.unresolved_name, stay.city_adcode or ""),
                ).fetchone()
                if exists:
                    continue

                conn.execute(
                    "INSERT INTO alignment_task "
                    "(id, mention_name, city_adcode, context_snippet, candidate_pois_json, "
                    " status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (
                        new_id(WORKBENCH),
                        item.unresolved_name,
                        stay.city_adcode,
                        _alignment_context(day, stay),
                        json.dumps(_facts_payload(item), ensure_ascii=False),
                        _now(),
                    ),
                )
                queued += 1
    return queued


def _alignment_context(day: PlannedDay, stay: PlannedStay) -> str:
    return f"{stay.city_name} 第 {day.seq_in_stay + 1} 天（{day.day.isoformat()}）· {day.theme or '无主题'}"


def _facts_payload(item: PlannedItem) -> dict:
    facts = item.facts
    return {
        "name": item.title,
        "start_time": item.start_time,
        "end_time": item.end_time,
        "lat_gcj02": facts.lat_gcj02 if facts else None,
        "lng_gcj02": facts.lng_gcj02 if facts else None,
        "address": facts.address if facts else None,
    }


# ─── 内部：读取 ──────────────────────────────────────────────


def _load_stays(conn: sqlite3.Connection, trip_row: sqlite3.Row) -> tuple[PlannedStay, ...]:
    stay_rows = conn.execute(
        "SELECT id, city_adcode, city_name, seq FROM city_stay WHERE trip_id = ? ORDER BY seq",
        (trip_row["id"],),
    ).fetchall()

    stays: list[PlannedStay] = []
    for stay_row in stay_rows:
        day_rows = conn.execute(
            "SELECT id, date, seq_in_stay, theme FROM day WHERE city_stay_id = ? ORDER BY seq_in_stay",
            (stay_row["id"],),
        ).fetchall()

        days = tuple(_load_day(conn, r) for r in day_rows)
        stays.append(
            PlannedStay(
                city_name=stay_row["city_name"],
                city_adcode=stay_row["city_adcode"],
                seq=int(stay_row["seq"]),
                days=days,
            )
        )
    return tuple(stays)


def _load_day(conn: sqlite3.Connection, day_row: sqlite3.Row) -> PlannedDay:
    item_rows = conn.execute(
        "SELECT i.kind, i.poi_id, i.title, i.start_time, i.end_time, i.note, "
        "       p.lat_gcj02, p.lng_gcj02, p.address, p.tel, p.rating, p.open_time, p.photo_url "
        "FROM day_item i LEFT JOIN poi p ON p.amap_poi_id = i.poi_id "
        "WHERE i.day_id = ? ORDER BY i.seq",
        (day_row["id"],),
    ).fetchall()

    return PlannedDay(
        day=date.fromisoformat(day_row["date"]),
        seq_in_stay=int(day_row["seq_in_stay"]),
        theme=day_row["theme"],
        items=tuple(_item_from_row(row) for row in item_rows),
    )


def _item_from_row(row: sqlite3.Row) -> PlannedItem:
    """把一行天项还原成领域对象。

    一个景点如果没有实体主键，它就是「待对齐」的，而它的提及名就是它的标题——
    待对齐队列（`alignment_task`）里存着正式的记录，这里只需要让状态可辨认。
    不这样还原的话，未对齐的景点既没有主键也没有待对齐记录，会被
    `PlannedItem` 自己的不变量拒掉，整份行程就读不回来了。
    """
    kind = ItemKind(row["kind"])
    poi_id = row["poi_id"]
    title = row["title"] or ""
    awaiting_alignment = kind is ItemKind.POI and not poi_id

    return PlannedItem(
        kind=kind,
        title=title,
        poi_id=poi_id,
        start_time=row["start_time"],
        end_time=row["end_time"],
        note=row["note"],
        facts=PoiFacts(
            lat_gcj02=row["lat_gcj02"],
            lng_gcj02=row["lng_gcj02"],
            address=row["address"],
            tel=row["tel"],
            rating=row["rating"],
            open_time=row["open_time"],
            photo=row["photo_url"],
        )
        if poi_id
        else None,
        unresolved_name=title if awaiting_alignment else None,
    )


def _now() -> str:
    """带时区偏移的本地时间戳。本地工具里它比 UTC 更好读，且不产生歧义。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")
