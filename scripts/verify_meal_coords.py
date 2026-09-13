"""跑一次真实规划，核对餐饮项的坐标有没有落库。

迁移 10 让天项自己记住坐标与地址，为的是路书能算出「中午从博物馆走多久
到那家店」。单元测试能证明转换层不再丢坐标，但**真实路径必须跑一次**——
引擎给的餐饮结构长什么样，只有真跑才知道。

用法：python scripts/verify_meal_coords.py [城市] [天数]
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta

from _bootstrap import setup

setup()

from lushu.services import trip_service  # noqa: E402
from lushu.store import connect  # noqa: E402


async def main(argv: list[str]) -> int:
    city = argv[0] if argv else "南京"
    days = int(argv[1]) if len(argv) > 1 else 1
    query = f"{city}{days}天，看看博物馆和老建筑"
    # 出发日期要留出足够的提前量：太近的话天气预报与 12306 都拿不到
    start = date.today() + timedelta(days=40)

    print(f"跑一次真实规划：{query}（{start} 出发）")
    result = await trip_service.plan_and_save(
        trip_service.PlanRequest(query=query, start_date=start, days=days, name="餐饮坐标核对")
    )
    print(f"行程 {result.trip_id} 生成完毕")

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT di.kind, di.title, di.address, di.lat_gcj02, di.lng_gcj02, di.poi_id "
            "FROM day_item di JOIN day d ON d.id = di.day_id "
            "WHERE d.trip_id = ? ORDER BY d.date, di.seq",
            (result.trip_id,),
        ).fetchall()
    finally:
        conn.close()

    print()
    with_coords = 0
    for row in rows:
        has = "有坐标" if row["lat_gcj02"] is not None else "**没坐标**"
        has_addr = "有地址" if row["address"] else "没地址"
        if row["lat_gcj02"] is not None:
            with_coords += 1
        print(f"  [{row['kind']}] {row['title']:<24}{has}  {has_addr}")

    print()
    print(f"{len(rows)} 个天项里 {with_coords} 个有坐标")
    meals = [row for row in rows if row["kind"] == "meal"]
    meals_with = [row for row in meals if row["lat_gcj02"] is not None]
    print(f"餐饮 {len(meals)} 项，其中 {len(meals_with)} 项有坐标")
    if meals and not meals_with:
        print("餐饮项仍然没有坐标——迁移 10 没生效，或者引擎这次没给餐厅")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
