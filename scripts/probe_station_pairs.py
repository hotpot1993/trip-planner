"""确认站对筛选有没有漏掉高铁主站。

station_candidates 的排序是「站名越短越前」，所以西安的候选是
[西安, 西安东, 西安北]，取前 2 就永远查不到西安北——而它才是西安的高铁主站。
如果确实漏了，最快车次就可能没被发现。
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from lushu.adapters import rail

FROM = "南京"
TO = "西安"
DISTANCE = 1189.0


def main() -> None:
    travel_date = date.today() + timedelta(days=5)
    stations = rail.load_stations()

    print("候选站：")
    for city in (FROM, TO):
        candidates = rail.station_candidates(city, stations)
        print(f"  {city}: {[s.name for s in candidates]}")
    print()

    result = rail.query_rail(FROM, TO, travel_date, distance_km=DISTANCE)
    pairs = Counter(f"{o.from_station} → {o.to_station}" for o in result.options)
    print(f"当前实现查到的 {len(result.options)} 个车次，站对分布：")
    for pair, count in pairs.most_common():
        print(f"  {pair}: {count} 个")
    if result.best:
        print(f"  最快：{result.best.service_no} {result.best.duration_min} 分钟 "
              f"({result.best.from_station} → {result.best.to_station})")
    print()

    # 直接问主站对，看有没有被漏掉
    print("直接查各站对（每组单独问一次）：")
    found: dict[str, tuple[int, int | None, str | None]] = {}
    for from_station in rail.station_candidates(FROM, stations)[:2]:
        for to_station in rail.station_candidates(TO, stations)[:3]:
            query = rail.query_rail(
                FROM, TO, travel_date, distance_km=DISTANCE,
                stations=[from_station, to_station],
            )
            # 用单一候选列表时，query_rail 只会查这一组
            fastest = query.best
            label = f"{from_station.name} → {to_station.name}"
            found[label] = (
                len(query.options),
                fastest.duration_min if fastest else None,
                fastest.service_no if fastest else None,
            )

    for label, (count, minutes, service_no) in found.items():
        fastest_text = f"最快 {service_no} {minutes} 分钟" if minutes else "无车次"
        print(f"  {label:<22} {count:>3} 个车次   {fastest_text}")

    print()
    overall = [(m, s, label) for label, (_, m, s) in found.items() if m is not None]
    if overall:
        fastest = min(overall)
        print(f"全部站对里最快的：{fastest[1]} {fastest[0]} 分钟（{fastest[2]}）")
        current = result.best.duration_min if result.best else None
        if current is not None and fastest[0] < current:
            print(f"  ⚠ 当前实现漏掉了更快的车次（快了 {current - fastest[0]} 分钟）")
        else:
            print("  当前实现没有漏掉更快的车次")


if __name__ == "__main__":
    main()
