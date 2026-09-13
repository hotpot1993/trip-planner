"""核对一份行程的读取路径（`load_trip`）能拿到每个天项的坐标与地址。

路书直接查库，所以餐饮的坐标在路书上早就对了；但**界面走的是 `load_trip`**，
它原先只在有实体 id 时才建 `facts`，于是餐饮的坐标被读出来又丢掉——
路书上看不出，界面上却拿不到「这家店在哪」。

用法：python scripts/probe_trip_coords.py [trip_id]
"""

from __future__ import annotations

import sys

from _bootstrap import setup

setup()

from lushu.services import trip_store  # noqa: E402


def main(argv: list[str]) -> int:
    trip_id = argv[0] if argv else "trip_b389a97aa4b8"
    loaded = trip_store.load_trip(trip_id)
    if loaded is None:
        print(f"没有这份行程：{trip_id}")
        return 1

    total = without = 0
    for stay in loaded.plan.stays:
        print(f"== {stay.city_name} {stay.stay_days} 天")
        for day in stay.days:
            for item in day.items:
                total += 1
                facts = item.facts
                if facts and facts.has_coordinates:
                    spot = f"{facts.lat_gcj02:.4f},{facts.lng_gcj02:.4f}"
                else:
                    spot = "（无坐标）"
                    without += 1
                address = (facts.address if facts else None) or ""
                print(f"  [{item.kind.value}] {item.title:<28}{spot}  {address}")

    print()
    print(f"{total} 个天项，{total - without} 个有坐标")
    return 0 if without == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
