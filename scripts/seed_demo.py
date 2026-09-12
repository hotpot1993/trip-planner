"""灌一份演示数据，用于人工查看界面。

它不是测试，也不进版本库的产物——只是让界面里有真实形状的内容可看。
城市解析走外部服务，这里直接给定代码，因此不需要 API Key。
"""

from __future__ import annotations

from datetime import date, timedelta

from lushu.domain.planned import (
    ItemKind,
    PlannedDay,
    PlannedItem,
    PlannedStay,
    PlannedTrip,
    PoiFacts,
)
from lushu.services.trip_store import delete_trip, list_trips, save_planned_trip


def facts(lat: float, lng: float, address: str, rating: float, hours: str) -> PoiFacts:
    return PoiFacts(
        lat_gcj02=lat,
        lng_gcj02=lng,
        address=address,
        rating=rating,
        open_time=hours,
    )


def poi(
    title: str,
    poi_id: str | None,
    *,
    start: str,
    end: str,
    lat: float = 32.06,
    lng: float = 118.79,
    address: str = "南京市玄武区",
    rating: float = 4.6,
    hours: str = "08:30-17:00",
    tip: str | None = None,
) -> PlannedItem:
    if poi_id:
        return PlannedItem(
            kind=ItemKind.POI,
            title=title,
            poi_id=poi_id,
            start_time=start,
            end_time=end,
            note=tip,
            facts=facts(lat, lng, address, rating, hours),
        )
    return PlannedItem(
        kind=ItemKind.POI,
        title=title,
        unresolved_name=title,
        start_time=start,
        end_time=end,
        note=tip,
    )


def meal(title: str, note: str | None = None) -> PlannedItem:
    return PlannedItem(kind=ItemKind.MEAL, title=title, note=note)


def nanjing_days(start: date) -> tuple[PlannedDay, ...]:
    return (
        PlannedDay(
            day=start,
            seq_in_stay=0,
            theme="钟山风景区",
            items=(
                poi("中山陵", "B000A8UIN0", start="09:00", end="11:30",
                    tip="台阶多，穿舒适的鞋；周一闭馆别安排在这天。"),
                meal("南京大牌档（中山陵店）", "盐水鸭与美龄粥是招牌，午市排队约 20 分钟。"),
                poi("明孝陵", "B000A8UIN1", start="13:30", end="16:00",
                    lat=32.058, lng=118.833, address="南京市玄武区石象路7号", rating=4.7),
                meal("回味鸭血粉丝汤"),
            ),
        ),
        PlannedDay(
            day=start + timedelta(days=1),
            seq_in_stay=1,
            theme="秦淮河畔",
            items=(
                poi("夫子庙", "B000A8V001", start="09:30", end="11:30",
                    lat=32.023, lng=118.788, address="南京市秦淮区秦淮河北岸", rating=4.5,
                    hours="全天开放", tip="晚上灯会人多，白天来更从容。"),
                meal("蒋有记锅贴", "牛肉锅贴配鸭血汤是本地吃法。"),
                poi("老门东", "B000A8V002", start="14:00", end="17:00",
                    lat=32.015, lng=118.782, address="南京市秦淮区剪子巷", rating=4.6,
                    hours="全天开放"),
                meal("绿柳居"),
            ),
        ),
        PlannedDay(
            day=start + timedelta(days=2),
            seq_in_stay=2,
            theme=None,
            items=(
                poi("南京博物院", "B000A8V003", start="09:00", end="12:00",
                    lat=32.043, lng=118.828, address="南京市玄武区中山东路321号", rating=4.8,
                    hours="09:00-17:00，周一闭馆",
                    tip="免费但需提前预约，周末名额很紧张。"),
                meal("狮王府"),
                # 这个景点没有实体主键，界面上应该出现「待对齐」标记
                poi("某个网友推荐但高德搜不到的小院子", None, start="14:30", end="16:00"),
                meal("晚饭待定"),
            ),
        ),
    )


def xian_days(start: date) -> tuple[PlannedDay, ...]:
    return (
        PlannedDay(
            day=start,
            seq_in_stay=0,
            theme="城墙与钟楼",
            items=(
                poi("西安城墙", "X000A8UIN0", start="09:00", end="12:00",
                    lat=34.258, lng=108.942, address="西安市碑林区南大街", rating=4.7,
                    hours="08:00-22:00"),
                meal("老孙家泡馍"),
            ),
        ),
        PlannedDay(
            day=start + timedelta(days=1),
            seq_in_stay=1,
            theme="兵马俑",
            items=(
                poi("秦始皇兵马俑博物馆", "X000A8UIN1", start="08:30", end="12:30",
                    lat=34.385, lng=109.273, address="西安市临潼区秦陵北路", rating=4.8,
                    hours="08:30-18:00",
                    tip="需提前实名预约；建议请讲解，否则看不出门道。"),
                meal("临潼石榴汁与biangbiang面"),
            ),
        ),
        PlannedDay(
            day=start + timedelta(days=2),
            seq_in_stay=2,
            theme="博物馆",
            items=(
                # 陕历博是需要提前预约的典型，M4 的预约子系统会重点覆盖它
                poi("陕西历史博物馆", "X000A8UIN2", start="09:00", end="12:00",
                    lat=34.237, lng=108.954, address="西安市雁塔区小寨东路91号", rating=4.8,
                    hours="09:00-17:30，周一闭馆",
                    tip="免费但必须提前预约，放票当天很快就没有了。"),
                meal("子午路张记肉夹馍"),
            ),
        ),
    )


def main() -> None:
    for summary in list_trips():
        delete_trip(summary.id)

    single = PlannedTrip(
        name="南京 3 天",
        start_date=date(2026, 10, 1),
        query="想去南京看博物馆和老建筑，节奏别太赶",
        stays=(
            PlannedStay(
                city_name="南京",
                city_adcode="320100",
                seq=0,
                days=nanjing_days(date(2026, 10, 1)),
            ),
        ),
    )
    print("已创建：", save_planned_trip(single))

    multi = PlannedTrip(
        name="南京、西安 5 天",
        start_date=date(2026, 11, 12),
        query="南京两天看博物馆，再去西安看兵马俑",
        stays=(
            PlannedStay(
                city_name="南京",
                city_adcode="320100",
                seq=0,
                days=nanjing_days(date(2026, 11, 12))[:2],
            ),
            PlannedStay(
                city_name="西安",
                city_adcode="610100",
                seq=1,
                days=xian_days(date(2026, 11, 14)),
            ),
        ),
    )
    print("已创建：", save_planned_trip(multi))

    empty = PlannedTrip(
        name="待填的行程",
        start_date=date(2026, 12, 24),
        stays=(
            PlannedStay(
                city_name="成都",
                city_adcode="510100",
                seq=0,
                days=tuple(
                    PlannedDay(day=date(2026, 12, 24) + timedelta(days=i), seq_in_stay=i)
                    for i in range(4)
                ),
            ),
        ),
    )
    print("已创建：", save_planned_trip(empty))

    print()
    for summary in list_trips():
        print(f"  {summary.name:<16} {summary.total_days} 天  {summary.status}")


if __name__ == "__main__":
    main()
