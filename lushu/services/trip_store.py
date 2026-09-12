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
from collections.abc import Sequence
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
from lushu.domain.transfer import TransferPlacementPlan
from lushu.store.connection import connect
from lushu.store.ids import (
    BUDGET,
    CITY_STAY,
    DAY,
    DAY_ITEM,
    TRANSFER,
    TRIP,
    WORKBENCH,
    new_id,
)


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
class CityRef:
    """城市的实体数据。坐标是 GCJ-02（ADR-0003）。"""

    adcode: str
    name: str
    lat_gcj02: float | None = None
    lng_gcj02: float | None = None


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
class StoredTransfer:
    """从表里读回的一次城际转移。"""

    id: str
    from_city_name: str
    to_city_name: str
    day: date
    day_index: int
    mode: str
    service_no: str | None = None
    from_station: str | None = None
    to_station: str | None = None
    dep_time: str | None = None
    arr_time: str | None = None
    duration_min: int | None = None
    price: float | None = None
    price_source: str = "estimate"
    is_reference_price: bool = True
    has_tickets: bool | None = None
    advice_reason: str | None = None
    note: str | None = None
    alternatives: tuple[dict, ...] = ()


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


# ─── 城际转移 ────────────────────────────────────────────────


def save_transfers(
    trip_id: str,
    transfers: Sequence[TransferPlacementPlan],
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """替换一份行程的城际转移，并同步城际交通的预算项。

    替换语义：重新查一次车次会覆盖旧的。城际交通的预算项是从转移派生的，
    所以一并重建——两处各存一份必然会对不上。

    入参用序号定位（城市停留序号 + 全行程天序号），数据库 id 在这里换算。
    """
    owned = conn is None
    active = conn or connect()
    try:
        with active:
            stay_ids = _stay_ids_by_seq(active, trip_id)
            day_ids = _day_ids_by_index(active, trip_id)
            if not stay_ids:
                raise LookupError(f"行程不存在或没有城市停留：{trip_id}")

            active.execute("DELETE FROM intercity_transfer WHERE trip_id = ?", (trip_id,))
            active.execute(
                "DELETE FROM budget_item WHERE trip_id = ? AND category = 'intercity'",
                (trip_id,),
            )

            written = 0
            for transfer in transfers:
                from_stay_id = stay_ids.get(transfer.from_stay_seq)
                to_stay_id = stay_ids.get(transfer.to_stay_seq)
                day_id = day_ids.get(transfer.day_index)
                if from_stay_id is None or to_stay_id is None or day_id is None:
                    raise LookupError(
                        f"转移落点不存在：城市 {transfer.from_stay_seq}→{transfer.to_stay_seq}，"
                        f"天序号 {transfer.day_index}"
                    )

                chosen = transfer.chosen
                active.execute(
                    "INSERT INTO intercity_transfer "
                    "(id, trip_id, from_city_stay_id, to_city_stay_id, day_id, mode, service_no, "
                    " from_station, to_station, dep_time, arr_time, duration_min, price, "
                    " price_source, is_reference_price, has_tickets, advice_reason, note, "
                    " alternatives_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id(TRANSFER),
                        trip_id,
                        from_stay_id,
                        to_stay_id,
                        day_id,
                        transfer.mode.value,
                        chosen.service_no if chosen else None,
                        chosen.from_station if chosen else None,
                        chosen.to_station if chosen else None,
                        chosen.dep_time if chosen else None,
                        chosen.arr_time if chosen else None,
                        chosen.duration_min if chosen else None,
                        transfer.intercity_fare,
                        chosen.price_source.value if chosen else "estimate",
                        1 if (chosen is None or chosen.is_reference_price) else 0,
                        None if chosen is None else (1 if chosen.has_tickets else 0),
                        transfer.advice_reason,
                        transfer.note,
                        json.dumps(
                            [_option_payload(option) for option in transfer.alternatives],
                            ensure_ascii=False,
                        ),
                    ),
                )
                written += 1

                fare = transfer.intercity_fare
                if fare is not None:
                    active.execute(
                        "INSERT INTO budget_item "
                        "(id, trip_id, category, label, amount, currency, is_reference_price, source, note) "
                        "VALUES (?, ?, 'intercity', ?, ?, 'CNY', ?, ?, ?)",
                        (
                            new_id(BUDGET),
                            trip_id,
                            _transfer_label(transfer),
                            fare,
                            1 if (chosen is None or chosen.is_reference_price) else 0,
                            chosen.price_source.value if chosen else "estimate",
                            chosen.note if chosen else None,
                        ),
                    )

            return written
    finally:
        if owned:
            active.close()


def load_transfers(
    trip_id: str, *, conn: sqlite3.Connection | None = None
) -> list[StoredTransfer]:
    """读回一份行程的城际转移，按日期排序。"""
    owned = conn is None
    active = conn or connect()
    try:
        rows = active.execute(
            "SELECT t.id, t.mode, t.service_no, t.from_station, t.to_station, "
            "       t.dep_time, t.arr_time, t.duration_min, t.price, t.price_source, "
            "       t.is_reference_price, t.has_tickets, t.advice_reason, t.note, "
            "       t.alternatives_json, "
            "       d.date, d.id AS day_id, fs.city_name AS from_city, ts.city_name AS to_city, "
            "       fs.seq AS from_seq "
            "FROM intercity_transfer t "
            "JOIN day d ON d.id = t.day_id "
            "JOIN city_stay fs ON fs.id = t.from_city_stay_id "
            "JOIN city_stay ts ON ts.id = t.to_city_stay_id "
            "WHERE t.trip_id = ? ORDER BY d.date",
            (trip_id,),
        ).fetchall()

        # 全行程的天序号用于把转移挂回它所在的那一天
        order = [
            row["id"]
            for row in active.execute(
                "SELECT id FROM day WHERE trip_id = ? ORDER BY date", (trip_id,)
            ).fetchall()
        ]
    finally:
        if owned:
            active.close()

    index_of = {day_id: index for index, day_id in enumerate(order)}

    return [
        StoredTransfer(
            id=row["id"],
            from_city_name=row["from_city"],
            to_city_name=row["to_city"],
            day=date.fromisoformat(row["date"]),
            day_index=index_of.get(row["day_id"], 0),
            mode=row["mode"],
            service_no=row["service_no"],
            from_station=row["from_station"],
            to_station=row["to_station"],
            dep_time=row["dep_time"],
            arr_time=row["arr_time"],
            duration_min=row["duration_min"],
            price=row["price"],
            price_source=row["price_source"],
            is_reference_price=bool(row["is_reference_price"]),
            has_tickets=None if row["has_tickets"] is None else bool(row["has_tickets"]),
            advice_reason=row["advice_reason"],
            note=row["note"],
            alternatives=tuple(json.loads(row["alternatives_json"] or "[]")),
        )
        for row in rows
    ]


def _stay_ids_by_seq(conn: sqlite3.Connection, trip_id: str) -> dict[int, str]:
    rows = conn.execute(
        "SELECT id, seq FROM city_stay WHERE trip_id = ?", (trip_id,)
    ).fetchall()
    return {int(row["seq"]): row["id"] for row in rows}


def _day_ids_by_index(conn: sqlite3.Connection, trip_id: str) -> dict[int, str]:
    """全行程的天序号 → 天 id。序号按日期排，与读模型一致。"""
    rows = conn.execute(
        "SELECT id FROM day WHERE trip_id = ? ORDER BY date", (trip_id,)
    ).fetchall()
    return {index: row["id"] for index, row in enumerate(rows)}


def _option_payload(option) -> dict:
    """备选方案存成 JSON。只留展示要用的字段。"""
    return {
        "service_no": option.service_no,
        "from_station": option.from_station,
        "to_station": option.to_station,
        "dep_time": option.dep_time,
        "arr_time": option.arr_time,
        "duration_min": option.duration_min,
        "price": option.price,
        "is_reference_price": option.is_reference_price,
        "has_tickets": option.has_tickets,
    }


def _transfer_label(transfer: TransferPlacementPlan) -> str:
    chosen = transfer.chosen
    if chosen is None:
        return f"城际交通（{transfer.from_stay_seq}→{transfer.to_stay_seq}，暂无方案）"
    return f"{chosen.from_station} → {chosen.to_station} {chosen.service_no}"


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


def _upsert_city(
    conn: sqlite3.Connection,
    name: str,
    adcode: str,
    *,
    lat_gcj02: float | None = None,
    lng_gcj02: float | None = None,
) -> None:
    """城市是实体真源的一部分（ADR-0002）。

    坐标为 None 时**不覆盖**已有的值：一条行程里若没解析到坐标，不该把之前
    解析好的坐标抹掉。天气面板与里程估价都依赖这两列。
    """
    conn.execute(
        "INSERT INTO city (adcode, name, lat_gcj02, lng_gcj02, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(adcode) DO UPDATE SET "
        "  name = excluded.name, "
        "  lat_gcj02 = COALESCE(excluded.lat_gcj02, city.lat_gcj02), "
        "  lng_gcj02 = COALESCE(excluded.lng_gcj02, city.lng_gcj02), "
        "  updated_at = excluded.updated_at",
        (adcode, name, lat_gcj02, lng_gcj02, _now()),
    )


def upsert_cities(cities: Sequence[CityRef], *, conn: sqlite3.Connection | None = None) -> None:
    """把解析到的城市写进实体表。

    城市名是人给的，代码与坐标必须以高德为准（ADR-0002）。解析是唯一能拿到
    坐标的时机，所以顺手记下来——天气与里程估价不该为了坐标再问一次高德。
    """
    if not cities:
        return

    owned = conn is None
    active = conn or connect()
    try:
        with active:
            for city in cities:
                _upsert_city(
                    active,
                    city.name,
                    city.adcode,
                    lat_gcj02=city.lat_gcj02,
                    lng_gcj02=city.lng_gcj02,
                )
    finally:
        if owned:
            active.close()


def load_cities(
    adcodes: Sequence[str], *, conn: sqlite3.Connection | None = None
) -> dict[str, CityRef]:
    """按行政区划代码取城市。返回 {adcode: CityRef}。"""
    codes = [code for code in dict.fromkeys(adcodes) if code]
    if not codes:
        return {}

    owned = conn is None
    active = conn or connect()
    try:
        placeholders = ",".join("?" for _ in codes)
        rows = active.execute(
            f"SELECT adcode, name, lat_gcj02, lng_gcj02 FROM city WHERE adcode IN ({placeholders})",
            codes,
        ).fetchall()
    finally:
        if owned:
            active.close()

    return {
        row["adcode"]: CityRef(
            adcode=row["adcode"],
            name=row["name"],
            lat_gcj02=row["lat_gcj02"],
            lng_gcj02=row["lng_gcj02"],
        )
        for row in rows
    }


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
