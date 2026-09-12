"""城际转移的编排：定落点、查车次、给建议。

三步，每一步都能指出依据：

1. **落点**由 `default_transfer_day` 决定：默认落在出发城市的最后一天，
   长车程或需转机时改落在到达城市的第一天（Q45）。
2. **交通方式**由 `advise_mode` 按距离与耗时决定，不交给 LLM（Q33）。
3. **车次与票价**来自 12306。票价是估价并如实标注——它的票价接口当前不可用，
   而余票响应里根本没有价格字段（Q14）。

**先后依赖**：落点取决于车程，车程要按日期去查，而日期又取决于落点。这里的
解法是先用默认落点的日期查一次；若最终落点落在另一天，再用那一天重查一次并
以新数据为准，到此为止不再迭代——两个候选日期只差一天，时刻表几乎一样。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date

from lushu.adapters import rail
from lushu.domain.planned import PlannedTrip
from lushu.domain.transfer import (
    TransferMode,
    TransferPlacementPlan,
    advise_mode,
)
from lushu.domain.trip import CalendarDay, CityStay, default_transfer_day, plan_days
from lushu.services.trip_store import (
    CityRef,
    StoredTransfer,
    load_cities,
    load_transfers,
    load_trip,
    save_transfers,
)

__all__ = ["build_transfers", "refresh_transfers"]

# 备选方案最多留几个。这不是查时刻表，三个够挑了。
MAX_ALTERNATIVES = 3


async def build_transfers(
    plan: PlannedTrip,
    cities: dict[str, CityRef],
    *,
    today: date | None = None,
) -> list[TransferPlacementPlan]:
    """算出相邻城市之间的转移。单城市行程返回空列表。"""
    if len(plan.stays) < 2:
        return []

    today = today or date.today()

    # 复用已验证的排期逻辑：它负责回答「哪一天属于哪座城市」
    calendar_stays = [
        CityStay(
            id=str(stay.seq),
            city_adcode=stay.city_adcode or "",
            city_name=stay.city_name,
            seq=stay.seq,
            stay_days=stay.stay_days,
        )
        for stay in plan.stays
    ]
    calendar_days = plan_days(calendar_stays, plan.start_date)
    by_seq = {stay.seq: stay for stay in calendar_stays}

    transfers: list[TransferPlacementPlan] = []
    for index in range(len(plan.stays) - 1):
        transfers.append(
            await _build_one(
                from_stay=by_seq[index],
                to_stay=by_seq[index + 1],
                calendar_days=calendar_days,
                cities=cities,
                today=today,
            )
        )
    return transfers


async def refresh_transfers(trip_id: str, *, today: date | None = None) -> list[StoredTransfer]:
    """重新查一遍某份行程的城际转移并落库，返回落库后的结果。"""
    stored = load_trip(trip_id)
    if stored is None:
        raise LookupError(f"行程不存在：{trip_id}")

    adcodes = [stay.city_adcode for stay in stored.plan.stays if stay.city_adcode]
    cities = load_cities(adcodes)
    plans = await build_transfers(stored.plan, cities, today=today)
    save_transfers(trip_id, plans)
    return load_transfers(trip_id)


# ─── 内部 ────────────────────────────────────────────────────


async def _build_one(
    *,
    from_stay: CityStay,
    to_stay: CityStay,
    calendar_days: Sequence[CalendarDay],
    cities: dict[str, CityRef],
    today: date,
) -> TransferPlacementPlan:
    from_city = cities.get(from_stay.city_adcode)
    to_city = cities.get(to_stay.city_adcode)
    distance = _distance(from_city, to_city)

    from_days = [day for day in calendar_days if day.city_stay_id == from_stay.id]
    to_days = [day for day in calendar_days if day.city_stay_id == to_stay.id]
    if not from_days or not to_days:
        raise LookupError(f"找不到「{from_stay.city_name}」或「{to_stay.city_name}」的天")

    # 第一次：按默认落点（出发城市最后一天）的日期查
    first_date = from_days[-1].day_date
    query = await _query(from_stay, to_stay, first_date, distance, today)

    advice, placement = _advise(from_stay, to_stay, calendar_days, query, distance)
    target_date = _day_date(calendar_days, placement.day_index)

    # 落点最终不在这天时，用那天的真实时刻表重查一次，以新数据为准
    if target_date != first_date:
        query = await _query(from_stay, to_stay, target_date, distance, today)
        advice, placement = _advise(from_stay, to_stay, calendar_days, query, distance)

    chosen = query.best if advice.mode is TransferMode.RAIL else None
    alternatives = (
        tuple(query.options[1 : 1 + MAX_ALTERNATIVES]) if chosen is not None else ()
    )

    notes = [note for note in (query.note, chosen.note if chosen else None) if note]

    return TransferPlacementPlan(
        from_stay_seq=from_stay.seq,
        to_stay_seq=to_stay.seq,
        day_index=placement.day_index,
        mode=advice.mode,
        advice_reason=advice.reason,
        chosen=chosen,
        alternatives=alternatives,
        note="；".join(notes) if notes else None,
    )


def _advise(
    from_stay: CityStay,
    to_stay: CityStay,
    calendar_days: Sequence[CalendarDay],
    query: rail.RailQuery,
    distance: float | None,
):
    """给出交通方式建议与落点。"""
    advice = advise_mode(
        best_rail_minutes=query.best_minutes,
        rail_available=bool(query.options),
        distance_km=distance,
        # 超出预售期不等于没有铁路。混为一谈会因为一个与铁路无关的原因
        # 建议用户去坐飞机。
        rail_unknown=not query.within_sale_window,
    )
    placement = default_transfer_day(
        calendar_days,
        from_stay,
        to_stay,
        duration_min=query.best_minutes,
        mode=advice.mode,
    )
    return advice, placement


async def _query(
    from_stay: CityStay,
    to_stay: CityStay,
    travel_date: date,
    distance: float | None,
    today: date,
) -> rail.RailQuery:
    return await asyncio.to_thread(
        rail.query_rail,
        from_stay.city_name,
        to_stay.city_name,
        travel_date,
        distance_km=distance,
        today=today,
    )


def _day_date(calendar_days: Sequence[CalendarDay], index: int) -> date:
    for day in calendar_days:
        if day.index == index:
            return day.day_date
    raise LookupError(f"行程里没有第 {index} 天")


def _distance(from_city: CityRef | None, to_city: CityRef | None) -> float | None:
    """两座城市之间的估算铁路里程。缺坐标就返回 None——不编一个距离出来。"""
    if from_city is None or to_city is None:
        return None
    if None in (from_city.lat_gcj02, from_city.lng_gcj02, to_city.lat_gcj02, to_city.lng_gcj02):
        return None
    return rail.rail_distance_km(
        (from_city.lat_gcj02, from_city.lng_gcj02),  # type: ignore[arg-type]
        (to_city.lat_gcj02, to_city.lng_gcj02),  # type: ignore[arg-type]
    )
