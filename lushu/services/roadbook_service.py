"""从行程装配出一份路书，并渲染成单个 HTML 文件。

设计第八节：产物是**单个 HTML 文件，手机优先、离线可读**，旅行途中在手机上
查阅。契约在 `lushu/domain/roadbook.py`，这里做两件事——装配与渲染。

**路段是这一层最要紧的东西。** 地图被砍掉之后（ADR-0006），
「上午故宫、步行二十分钟到景山」这类文字就是空间关系的全部载体，
设计原话是「这块内容要做足」。而 `leg_option` 表从 M1 起就是空的，
所以这里按坐标算直线距离与预计耗时，**并如实标注这是估算**——
在一个没有地图的页面上说「步行 20 分钟」，用户会当真；
说「直线约 1.2 公里，实际路程更长」，他才知道要留余量。

渲染的三条硬约束（契约里会再查一遍）：

1. 内联样式，**零外部请求**——旅行途中信号不好是常态
2. 手机优先：窄屏单列，触控目标够大
3. 导航深链保留：点开就跳到地图 App，这是地图被砍掉之后留下的那部分
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from html import escape

from lushu.domain.geo import distance_m
from lushu.domain.roadbook import (
    NEEDS_LEG_METRES,
    Roadbook,
    RoadbookBooking,
    RoadbookClaim,
    RoadbookDay,
    RoadbookItem,
    RoadbookLeg,
    leg_fingerprint,
)
from lushu.services import knowledge_store as ks
from lushu.store.connection import connect

# 步行与市内交通的粗略速度（公里/小时）。它们是**估算**，
# 界面上会如实标出来，不假装是路径规划的结果。
SPEEDS = {"walk": 4.5, "bike": 12.0, "metro": 25.0, "bus": 18.0, "taxi": 22.0, "drive": 28.0}

# 超过这个距离就不建议走了，改推地铁或打车。
#
# **它不是一个独立的数**：就是 `domain/roadbook.py` 的 `NEEDS_LEG_METRES`
# ——那一条是契约里的「写着步行但这么远，现场会很意外」。原先这里抄了一个
# 1500.0 并在注释里要求「必须与那边一致」，现在直接指向它：一处判断只能有
# 一个数字，而「要求两处保持一致」的注释迟早会被忽略。
WALK_LIMIT_M = NEEDS_LEG_METRES

# 高德导航深链。设计说从 travel-plan-viz 的 map.js 借构造函数，
# 但那个仓库没进本项目（见 docs/M6-STATUS.md），这里直接按高德 URI API 拼。
NAV_MARKER = "https://uri.amap.com/marker?position={lng},{lat}&name={name}"
NAV_ROUTE = "https://uri.amap.com/navigation?from={flng},{flat},{fname}&to={tlng},{tlat},{tname}&mode={mode}"


@dataclass(frozen=True)
class _Point:
    name: str
    lat: float | None
    lng: float | None

    @property
    def located(self) -> bool:
        return self.lat is not None and self.lng is not None


def haversine_m(left: _Point, right: _Point) -> float | None:
    """两点直线距离（米）。算不出来就返回 None，不猜。

    算式在 `domain/geo.py`——餐饮候选排序用的是同一个（那边要按距离排），
    两处各写一遍只会在某次改动后悄悄不一致。
    """
    if not (left.located and right.located):
        return None
    return distance_m(left.lat, left.lng, right.lat, right.lng)  # type: ignore[arg-type]

def estimate_leg(left: _Point, right: _Point) -> RoadbookLeg | None:
    """把两点之间怎么走估出来。

    没有坐标就给 None——契约里那条「这两处之间没有路段说明」的提醒会报出来，
    比编一段看起来合理的文字诚实。

    距离取直线，耗时按交通方式的粗略速度算，**并写明是估算**。
    """
    distance = haversine_m(left, right)
    if distance is None:
        return None

    if distance <= WALK_LIMIT_M:
        mode = "walk"
    elif distance <= 6000:
        mode = "metro"
    else:
        mode = "taxi"

    minutes = max(1, round(distance / 1000 / SPEEDS[mode] * 60))
    nav = NAV_ROUTE.format(
        flng=left.lng,
        flat=left.lat,
        fname=escape(left.name),
        tlng=right.lng,
        tlat=right.lat,
        tname=escape(right.name),
        mode={"walk": "walk", "metro": "bus", "taxi": "car"}[mode],
    )
    return RoadbookLeg(
        mode=mode,
        distance_m=round(distance),
        duration_min=minutes,
        from_name=left.name,
        to_name=right.name,
        nav_url=nav,
        note="直线距离估算，实际路程更长",
    )


def _stored_leg(
    current: sqlite3.Row, following: sqlite3.Row, left: _Point, right: _Point
) -> RoadbookLeg | None:
    """天项上存着的**真实**路段；没有或已经失效就返回 None（退回估算）。

    有效性由**指纹**判定：两端坐标加上通向哪一项。距离只由两端坐标决定，
    键覆盖全部依赖，就没有悄悄过期的余地——行程重排会变，对齐把天项挪到
    另一个实体上（坐标变了而 id 没变）也会变。原先只记「通向哪一项」，
    挡得住前者挡不住后者，而后者留下的是**一条错的距离**，在路书上看起来
    和一个对的一模一样。

    坐标用传进来的 `_Point` 而不是天项那两列：历史行程的景点坐标在 `poi`
    表里（迁移 10 才让天项自己记），直接用列会拼出一条坐标是 None 的导航链接。
    **读的时候与写的时候取的必须是同一份坐标**，否则指纹每次都对不上，
    路段会永远退回待办（那样倒也不会出错，只是白查）。

    真实路段**不带「估算」那句提醒**：那句话是给估算用的，挂在真数据上
    会让用户以为连这个也不准。
    """
    if not current["leg_mode"] or not (left.located and right.located):
        return None
    if current["leg_key"] != leg_fingerprint(
        from_lat=left.lat,
        from_lng=left.lng,
        to_lat=right.lat,
        to_lng=right.lng,
        to_ref=following["id"],
    ):
        return None
    mode = current["leg_mode"]
    return RoadbookLeg(
        mode=mode,
        distance_m=current["leg_distance_m"],
        duration_min=current["leg_duration_min"],
        from_name=left.name,
        to_name=right.name,
        nav_url=NAV_ROUTE.format(
            flng=left.lng,
            flat=left.lat,
            fname=escape(left.name),
            tlng=right.lng,
            tlat=right.lat,
            tname=escape(right.name),
            mode={"walk": "walk", "metro": "bus", "taxi": "car"}.get(mode, "walk"),
        ),
        note=None,
    )


def _seconds(value: str | None) -> int | None:
    if not value or ":" not in value:
        return None
    head, _, tail = value.partition(":")
    if not (head.isdigit() and tail.isdigit()):
        return None
    return int(head) * 3600 + int(tail) * 60


def item_sort_key(row: sqlite3.Row) -> tuple[int, int, int]:
    """天项在一天里的先后。**路书与路段规划必须用同一个。**

    按时刻排，没有时刻的排在当天最后——它们本来就是「有空再说」的那些。
    同刻按 `seq`：那是行程编辑器里的顺序，也是唯一的稳定依据。
    原先只按时刻，同刻的顺序取决于 `SELECT *` 不带 ORDER BY 时行的物理顺序，
    那是实现细节——**把排序建在存储布局上**，换个 SQLite 版本就可能变，
    而路段会因此挂到错误的一对端点之间，页面上完全看不出来。
    """
    seconds = _seconds(row["start_time"])
    return (1 if seconds is None else 0, seconds or 0, row["seq"] or 0)


def _poi_map(conn: sqlite3.Connection, poi_ids: list[str]) -> dict[str, sqlite3.Row]:
    if not poi_ids:
        return {}
    placeholders = ",".join("?" for _ in poi_ids)
    rows = conn.execute(
        f"SELECT * FROM poi WHERE amap_poi_id IN ({placeholders})", poi_ids
    ).fetchall()
    return {row["amap_poi_id"]: row for row in rows}


def _insight_map(conn: sqlite3.Connection, poi_ids: list[str]) -> dict[str, list[ks.ClaimRow]]:
    grouped: dict[str, list[ks.ClaimRow]] = {}
    for poi_id in poi_ids:
        claims = ks.claims_for_poi(poi_id, conn=conn)
        if claims:
            grouped[poi_id] = claims
    return grouped


def _booking_text(conn: sqlite3.Connection, poi_id: str) -> str | None:
    """这个景点要不要预约。**只取已复核的规则**（Q10）。"""
    row = conn.execute(
        "SELECT booking_required, advance_days, release_time FROM booking_rule "
        "WHERE poi_id = ? AND status = 'reviewed'",
        (poi_id,),
    ).fetchone()
    if row is None or not row["booking_required"]:
        return None
    if row["advance_days"] is None:
        return "必须提前线上预约，放票口径以官方渠道为准"
    when = f" {row['release_time']}" if row["release_time"] else ""
    return f"需预约，提前 {row['advance_days']} 天{when}放票"


def assemble(
    trip_id: str, *, conn: sqlite3.Connection | None = None, generated_at: str | None = None
) -> tuple[Roadbook, list[str]]:
    """把库里的一份行程装配成路书。返回（路书，警告）。

    警告是「装不出来但不算错」的东西：天项没有坐标、没有地址。
    它们会在契约校验里以提醒的形式再出现一次，这里先收集起来供调用方展示。
    """
    owned = conn is None
    active = conn or connect()
    warnings: list[str] = []
    try:
        trip = active.execute(
            "SELECT * FROM trip WHERE id = ?", (trip_id,)
        ).fetchone()
        if trip is None:
            raise ValueError(f"没有这份行程：{trip_id}")

        stays = active.execute(
            "SELECT * FROM city_stay WHERE trip_id = ? ORDER BY seq", (trip_id,)
        ).fetchall()
        stay_by_id = {row["id"]: row for row in stays}

        days = active.execute(
            "SELECT * FROM day WHERE trip_id = ? ORDER BY date", (trip_id,)
        ).fetchall()

        bookings = _bookings(active, trip_id)
        budget = tuple(
            (row["label"], f"¥{row['amount']:.0f}" + ("（参考价）" if row["is_reference_price"] else ""))
            for row in active.execute(
                "SELECT * FROM budget_item WHERE trip_id = ? ORDER BY category, label",
                (trip_id,),
            )
        )

        roadbook_days: list[RoadbookDay] = []
        for day in days:
            items = active.execute(
                "SELECT * FROM day_item WHERE day_id = ?", (day["id"],)
            ).fetchall()
            items = sorted(items, key=item_sort_key)
            poi_ids = [row["poi_id"] for row in items if row["poi_id"]]
            pois = _poi_map(active, poi_ids)
            insights = _insight_map(active, poi_ids)

            roadbook_items: list[RoadbookItem] = []
            points: list[_Point] = []
            for row in items:
                poi = pois.get(row["poi_id"]) if row["poi_id"] else None
                claims = insights.get(row["poi_id"] or "", [])
                title = row["title"] or (poi["name"] if poi else "（未命名）")
                if row["kind"] == "poi" and poi is None:
                    warnings.append(f"{day['date']} 「{title}」还没对上实体，挂不上攻略")
                # 坐标优先取实体上的（ADR-0002：高德是硬事实的来源）；
                # 餐饮没有实体，取天项自己记下的那一份（迁移 10）
                lat = (poi["lat_gcj02"] if poi else None) or row["lat_gcj02"]
                lng = (poi["lng_gcj02"] if poi else None) or row["lng_gcj02"]
                roadbook_items.append(
                    RoadbookItem(
                        title=title,
                        kind=row["kind"],
                        start_time=row["start_time"],
                        end_time=row["end_time"],
                        address=(poi["address"] if poi else None) or row["address"],
                        note=row["note"],
                        highlights=tuple(
                            RoadbookClaim(
                                text=c.text, independent_source_count=c.independent_source_count
                            )
                            for c in claims
                            if c.polarity == "highlight"
                        ),
                        avoids=tuple(
                            RoadbookClaim(
                                text=c.text, independent_source_count=c.independent_source_count
                            )
                            for c in claims
                            if c.polarity == "avoid"
                        ),
                        booking=_booking_text(active, row["poi_id"]) if row["poi_id"] else None,
                        lat_gcj02=lat,
                        lng_gcj02=lng,
                    )
                )
                points.append(_Point(name=title, lat=lat, lng=lng))

            legs = tuple(
                _stored_leg(
                    items[index], items[index + 1], points[index], points[index + 1]
                )
                or estimate_leg(points[index], points[index + 1])
                for index in range(max(len(points) - 1, 0))
            )
            stay = stay_by_id.get(day["city_stay_id"])
            roadbook_days.append(
                RoadbookDay(
                    date=date.fromisoformat(day["date"]),
                    city_name=(stay["city_name"] if stay else "未知"),
                    seq_in_stay=day["seq_in_stay"],
                    theme=day["theme"],
                    items=tuple(roadbook_items),
                    legs=legs,
                    transfer=_transfer_text(active, day["id"]),
                )
            )

        roadbook = Roadbook(
            name=trip["name"],
            start_date=date.fromisoformat(trip["start_date"]),
            end_date=(
                date.fromisoformat(roadbook_days[-1].date.isoformat())
                if roadbook_days
                else date.fromisoformat(trip["start_date"])
            ),
            days=tuple(roadbook_days),
            bookings=bookings,
            budget=budget,
            generated_at=generated_at or datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
        return roadbook, warnings
    finally:
        if owned:
            active.close()


def _transfer_text(conn: sqlite3.Connection, day_id: str) -> str | None:
    row = conn.execute(
        "SELECT * FROM intercity_transfer WHERE day_id = ? LIMIT 1", (day_id,)
    ).fetchone()
    if row is None:
        return None
    mode = {"rail": "高铁", "air": "飞机", "coach": "大巴", "drive": "自驾"}.get(
        row["mode"], row["mode"]
    )

    # 起终站可能没查出来（预售期外就是这样），那就别写「高铁：高铁」——
    # 站点信息缺了，这句话仍然要有内容，而不是把交通方式说两遍
    stations = f"{row['from_station'] or ''} → {row['to_station'] or ''}".strip(" →")
    parts: list[str] = [stations] if stations else []
    if row["service_no"]:
        parts.append(row["service_no"])
    if row["dep_time"] and row["arr_time"]:
        parts.append(f"{row['dep_time']}–{row['arr_time']}")
    if row["duration_min"]:
        parts.append(f"{row['duration_min'] // 60} 小时 {row['duration_min'] % 60} 分")
    if row["price"]:
        suffix = "（参考价）" if row["is_reference_price"] else ""
        parts.append(f"¥{row['price']:.0f}{suffix}")
    if row["note"]:
        parts.append(row["note"])

    if not parts:
        return f"{mode}（车次与时刻还没查到）"
    return f"{mode}：{' · '.join(parts)}"


def _bookings(conn: sqlite3.Connection, trip_id: str) -> tuple[RoadbookBooking, ...]:
    """预约清单。**只含已复核的规则**（Q10）——未复核的规则不得展示。"""
    from lushu.services import booking_store

    alerts = booking_store.alerts_for_trip(trip_id, conn=conn)
    return tuple(
        RoadbookBooking(
            poi_name=alert.poi_name,
            visit_date=alert.visit_date,
            headline=alert.headline,
            channels=tuple(
                f"{channel.name}（{channel.url}）" if channel.url else channel.name
                for channel in alert.channels
            ),
            release_date=alert.release_date,
        )
        for alert in alerts
    )
