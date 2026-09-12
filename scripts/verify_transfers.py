"""用真实数据验证城际转移的两条路径。

不属于测试套件，会真的问 12306。

  路径一：行程在预售期之外（多城市行程的常态）→ 按距离建议，不编车次
  路径二：行程在预售期内 → 真车次、真时刻、按里程估的参考票价、城际交通预算栏
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from lushu.domain.planned import PlannedTrip, StaySpec, lay_out
from lushu.engine import resolve_city
from lushu.services.transfer_service import refresh_transfers
from lushu.services.trip_store import (
    CityRef,
    delete_trip,
    list_trips,
    load_trip,
    save_planned_trip,
    upsert_cities,
)
from lushu.store import connect


async def _ensure_city(name: str, adcode: str) -> CityRef | None:
    match = resolve_city(name)
    if match is None:
        print(f"  ⚠ 高德查不到「{name}」")
        return None
    ref = CityRef(
        adcode=adcode,
        name=name,
        lat_gcj02=match.lat_gcj02,
        lng_gcj02=match.lng_gcj02,
    )
    upsert_cities([ref])
    return ref


def _budget_rows(trip_id: str) -> list:
    conn = connect()
    try:
        return conn.execute(
            "SELECT category, label, amount, is_reference_price, source FROM budget_item "
            "WHERE trip_id = ? ORDER BY category, label",
            (trip_id,),
        ).fetchall()
    finally:
        conn.close()


async def _show(trip_id: str, title: str) -> None:
    stored = load_trip(trip_id)
    if stored is None:
        print("  行程不见了")
        return

    print(f"── {title} ──")
    print(f"   {stored.name}   {stored.plan.start_date} ~ {stored.plan.end_date}   {stored.plan.total_days} 天")
    print(f"   城市：{'、'.join(f'{s.city_name} {s.stay_days} 天' for s in stored.plan.stays)}")

    transfers = await refresh_transfers(trip_id, today=date.today())
    for transfer in transfers:
        print()
        print(f"   {transfer.from_city_name} → {transfer.to_city_name}"
              f"   落在第 {transfer.day_index + 1} 天（{transfer.day}）")
        print(f"   方式：{transfer.mode}")
        if transfer.advice_reason:
            print(f"   依据：{transfer.advice_reason}")
        if transfer.service_no:
            price = f"¥{transfer.price}" if transfer.price is not None else "无票价"
            reference = "参考价" if transfer.is_reference_price else "实价"
            tickets = {True: "有票", False: "无票", None: "未知"}[transfer.has_tickets]
            duration = transfer.duration_min or 0
            print(
                f"   车次：{transfer.service_no}  {transfer.from_station} → {transfer.to_station}"
            )
            print(
                f"         {transfer.dep_time}→{transfer.arr_time}  "
                f"{duration // 60} 小时 {duration % 60} 分   {price}（{reference}）   {tickets}"
            )
            for option in transfer.alternatives:
                minutes = option.get("duration_min") or 0
                print(
                    f"   备选：{option['service_no']:<7} {option['dep_time']}→{option['arr_time']}  "
                    f"{minutes // 60}h{minutes % 60:02d}m"
                )
        if transfer.note:
            print(f"   说明：{transfer.note}")

    rows = _budget_rows(trip_id)
    print()
    print(f"   预算项 {len(rows)} 条：")
    for row in rows:
        mark = "（参考价）" if row["is_reference_price"] else ""
        print(f"     [{row['category']}] {row['label']}  ¥{row['amount']}{mark}")
    if rows:
        print(f"     城际交通合计：¥{sum(r['amount'] for r in rows)}")
    print()


async def main() -> None:
    today = date.today()
    print(f"今天 {today}    12306 预售期至 {today + timedelta(days=14)}")
    print()

    print("准备城市坐标……")
    nanjing = await _ensure_city("南京", "320100")
    xian = await _ensure_city("西安", "610100")
    if not nanjing or not xian:
        return
    print(f"  南京 {nanjing.lat_gcj02},{nanjing.lng_gcj02}")
    print(f"  西安 {xian.lat_gcj02},{xian.lng_gcj02}")
    print()

    # 路径一：预售期之外
    far_start = today + timedelta(days=40)
    far = PlannedTrip(
        name=f"预售期外 南京西安（{far_start}）",
        start_date=far_start,
        stays=lay_out(far_start, [StaySpec("南京", 2, "320100"), StaySpec("西安", 3, "610100")]),
    )
    far_id = save_planned_trip(far)
    await _show(far_id, "路径一：行程在预售期之外")
    delete_trip(far_id)

    # 路径二：预售期内
    near_start = today + timedelta(days=5)
    near = PlannedTrip(
        name=f"预售期内 南京西安（{near_start}）",
        start_date=near_start,
        stays=lay_out(near_start, [StaySpec("南京", 2, "320100"), StaySpec("西安", 3, "610100")]),
    )
    near_id = save_planned_trip(near)
    await _show(near_id, "路径二：行程在预售期内")
    print(f"这份行程留在库里供界面查看：{near_id}")

    print()
    print("全部行程：")
    for summary in list_trips():
        print(f"  {summary.name:<34} {summary.total_days} 天  {summary.id}")


if __name__ == "__main__":
    asyncio.run(main())
