"""用真实网络验证铁路适配器。

不是测试套件的一部分——测试套件用 httpx.MockTransport 打桩，不发真实请求。
这个脚本用来确认适配器对着真的 12306 也能正常工作。
"""

from __future__ import annotations

from datetime import date, timedelta

from lushu.adapters import rail

PAIRS = (
    ("南京", "西安", (32.060255, 118.796877), (34.341574, 108.939770)),
    ("北京", "上海", (39.904179, 116.407387), (31.230416, 121.473701)),
)


def main() -> None:
    today = date.today()
    print(f"今天 {today}   预售期至 {rail.sale_window_end(today)}")
    print()

    print("=== 车站名表 ===")
    stations = rail.load_stations()
    print(f"  载入 {len(stations)} 个车站")
    for city in ("南京", "西安", "北京", "上海"):
        candidates = rail.station_candidates(city, stations)[:3]
        print(f"  {city}: {[(s.name, s.telecode) for s in candidates]}")
    print()

    for from_city, to_city, from_point, to_point in PAIRS:
        distance = rail.rail_distance_km(from_point, to_point)
        print(f"=== {from_city} → {to_city}（估程 {distance:.0f} 公里）===")

        for offset in (5, 20):
            travel_date = today + timedelta(days=offset)
            result = rail.query_rail(from_city, to_city, travel_date, distance_km=distance)
            print(f"  ── {travel_date}（T+{offset}）预售期内={result.within_sale_window} ──")
            if result.note:
                print(f"     提示：{result.note}")
            print(f"     车次 {len(result.options)} 个")
            for option in result.options[:3]:
                seats = "、".join(f"{k} {v}" for k, v in option.seats.items()) or "无席别信息"
                print(
                    f"       {option.service_no:<6} {option.dep_time}→{option.arr_time} "
                    f"{option.duration_min // 60}h{option.duration_min % 60:02d}m  "
                    f"{option.train_class.label}  "
                    f"¥{option.price}{'（参考价）' if option.is_reference_price else ''}  "
                    f"有票={option.has_tickets}  {seats}"
                )
            print()

        # 顺便验证建议规则吃的是真数据
        result = rail.query_rail(from_city, to_city, today + timedelta(days=5), distance_km=distance)
        from lushu.domain.transfer import advise_mode

        advice = advise_mode(
            best_rail_minutes=result.best_minutes,
            rail_available=bool(result.options),
            distance_km=distance,
        )
        print(f"  建议：{advice.mode_label} —— {advice.reason}")
        print()


if __name__ == "__main__":
    main()
