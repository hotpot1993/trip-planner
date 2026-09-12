"""对运行中的服务验证 M2 的三个新面：转移、预算栏、多城市天气。

不属于测试套件。天气那一段会真的问 Open-Meteo。
"""

from __future__ import annotations

import httpx

BASE = "http://127.0.0.1:8756"


def main() -> None:
    trips = httpx.get(f"{BASE}/api/trips", timeout=10).json()
    multi = next((t for t in trips if len(t["city_names"]) > 1), None)
    if multi is None:
        print("  没有多城市行程")
        return

    detail = httpx.get(f"{BASE}/api/trips/{multi['id']}", timeout=10).json()
    print(f"行程：{detail['name']}")
    print(f"  {detail['start_date']} ~ {detail['end_date']}   {detail['total_days']} 天")
    print()

    print(f"── 城际转移 {len(detail['transfers'])} 段 ──")
    for transfer in detail["transfers"]:
        print(f"  {transfer['from_city_name']} → {transfer['to_city_name']}"
              f"   第 {transfer['day_index'] + 1} 天（{transfer['day']}）")
        print(f"    方式 {transfer['mode']}")
        if transfer["service_no"]:
            minutes = transfer["duration_min"] or 0
            reference = "参考价" if transfer["is_reference_price"] else "实价"
            print(f"    {transfer['service_no']}  {transfer['from_station']} → {transfer['to_station']}"
                  f"  {transfer['dep_time']}→{transfer['arr_time']}"
                  f"  {minutes // 60}h{minutes % 60:02d}m  ¥{transfer['price']}（{reference}）")
        if transfer["advice_reason"]:
            print(f"    依据：{transfer['advice_reason']}")
        if transfer["note"]:
            print(f"    说明：{transfer['note']}")
        for option in transfer["alternatives"]:
            minutes = option.get("duration_min") or 0
            print(f"    备选 {option['service_no']:<7} {option['dep_time']}→{option['arr_time']} "
                  f"{minutes // 60}h{minutes % 60:02d}m")
    print()

    budget = detail["budget"]
    print("── 预算面板 ──")
    print(f"  城际交通 {len(budget['intercity'])} 条  合计 ¥{budget['intercity_total']}")
    for item in budget["intercity"]:
        mark = "（参考价）" if item["is_reference_price"] else ""
        print(f"    {item['label']}  ¥{item['amount']}{mark}")
    print(f"  其他 {len(budget['others'])} 条  合计 ¥{budget['other_total']}")
    print(f"  总计 ¥{budget['total']}   含参考价：{budget['has_reference_prices']}")
    print()

    print("── 多城市天气 ──")
    response = httpx.get(f"{BASE}/api/trips/{multi['id']}/weather", timeout=60)
    print(f"  HTTP {response.status_code}")
    weather = response.json()
    for city in weather["cities"]:
        print(f"  {city['city_name']}（{city['source_label']}）{len(city['days'])} 天")
        if city["note"]:
            print(f"    说明：{city['note']}")
        for day in city["days"][:5]:
            mark = "  ⚠ 不宜户外" if day["is_bad_outdoor"] else ""
            rain = (
                f"  降水 {day['precipitation_probability']:.0f}%"
                if day["precipitation_probability"] is not None
                else ""
            )
            print(f"    {day['date']}  {day['text']:<8} {day['temperature_text']}{rain}{mark}")


if __name__ == "__main__":
    main()
