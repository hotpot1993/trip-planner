"""改城市与天数的测试。

这条用例是设计里「用户可动态添加城市并设置各城市停留天数，系统自动计算
总行程天数」的落点，也是 M2 多城市编辑的基础。重点验证两件事：
天数口径随改动正确重算，以及仍然存在的天不丢内容。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from lushu.domain.planned import ItemKind, PlannedItem, PoiFacts, StaySpec
from lushu.services.trip_service import update_stays
from lushu.store import connect, initialize

START = date(2026, 10, 1)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """把默认库指向临时文件，并给出一个假的建行程入口。"""
    from lushu import config

    path = tmp_path / "lushu.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    initialize(path)
    conn = connect(path)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def stub_city_resolution(monkeypatch):
    """城市解析走外部服务，这里替换成一张固定的表。"""
    from lushu.engine.amap import CityMatch
    from lushu.services import trip_service

    table = {
        "南京": CityMatch(name="南京", adcode="320100", level="city"),
        "北京": CityMatch(name="北京", adcode="110100", level="city"),
        "西安": CityMatch(name="西安", adcode="610100", level="city"),
    }
    monkeypatch.setattr(trip_service, "resolve_city", lambda name, **kw: table.get(name))
    return table


def _item(title: str, poi_id: str) -> PlannedItem:
    return PlannedItem(
        kind=ItemKind.POI,
        title=title,
        poi_id=poi_id,
        facts=PoiFacts(lat_gcj02=32.0, lng_gcj02=118.8),
    )


async def _make_trip(specs, monkeypatch):
    """用真实落库路径建一份带内容的行程。"""
    from lushu.domain.planned import PlannedDay, PlannedStay, PlannedTrip
    from lushu.services.trip_store import save_planned_trip

    cursor = START
    stays = []
    for seq, spec in enumerate(specs):
        days = []
        for offset in range(spec.stay_days):
            days.append(
                PlannedDay(
                    day=cursor,
                    seq_in_stay=offset,
                    theme=f"{spec.city_name}第{offset + 1}天",
                    items=(_item(f"{spec.city_name}景点{offset + 1}", f"P{seq}{offset}"),),
                )
            )
            cursor += timedelta(days=1)
        stays.append(
            PlannedStay(
                city_name=spec.city_name,
                city_adcode=spec.city_adcode,
                seq=seq,
                days=tuple(days),
            )
        )
    draft = PlannedTrip(
        name="、".join(s.city_name for s in specs) + f" {sum(s.stay_days for s in specs)} 天",
        start_date=START,
        stays=tuple(stays),
    )
    return save_planned_trip(draft)


@pytest.mark.asyncio
async def test_increasing_days_recomputes_the_total(db) -> None:
    from lushu.services.trip_store import load_trip

    trip_id = await _make_trip([StaySpec("南京", 3, "320100")], None)
    await update_stays(trip_id, specs=[StaySpec("南京", 5)])

    loaded = load_trip(trip_id)
    assert loaded is not None
    assert loaded.plan.total_days == 5
    assert loaded.plan.stays[0].stay_days == 5
    assert loaded.plan.end_date == date(2026, 10, 5)


@pytest.mark.asyncio
async def test_decreasing_days_recomputes_the_total(db) -> None:
    from lushu.services.trip_store import load_trip

    trip_id = await _make_trip([StaySpec("南京", 4, "320100")], None)
    await update_stays(trip_id, specs=[StaySpec("南京", 2)])

    loaded = load_trip(trip_id)
    assert loaded is not None
    assert loaded.plan.total_days == 2
    assert [d.day for d in loaded.plan.stays[0].days] == [START, date(2026, 10, 2)]


@pytest.mark.asyncio
async def test_existing_days_keep_their_content(db) -> None:
    """改天数不该把没动过的那些天的内容一起抹掉。"""
    from lushu.services.trip_store import load_trip

    trip_id = await _make_trip([StaySpec("南京", 3, "320100")], None)
    await update_stays(trip_id, specs=[StaySpec("南京", 4)])

    loaded = load_trip(trip_id)
    assert loaded is not None
    days = loaded.plan.stays[0].days

    # 前三天原样保留
    assert [d.items[0].title for d in days[:3]] == ["南京景点1", "南京景点2", "南京景点3"]
    assert days[0].theme == "南京第1天"
    # 新增的第四天是空的
    assert days[3].items == ()


@pytest.mark.asyncio
async def test_removed_days_lose_their_content(db) -> None:
    from lushu.services.trip_store import load_trip

    trip_id = await _make_trip([StaySpec("南京", 3, "320100")], None)
    await update_stays(trip_id, specs=[StaySpec("南京", 1)])

    loaded = load_trip(trip_id)
    assert loaded is not None
    assert loaded.plan.total_days == 1
    assert loaded.plan.stays[0].days[0].items[0].title == "南京景点1"


# ─── 多城市 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adding_a_city_totals_both(db) -> None:
    """这就是「动态添加城市」：天数自动相加，日期自动接续。"""
    from lushu.services.trip_store import load_trip

    trip_id = await _make_trip([StaySpec("南京", 2, "320100")], None)
    await update_stays(
        trip_id, specs=[StaySpec("南京", 2), StaySpec("西安", 3)]
    )

    loaded = load_trip(trip_id)
    assert loaded is not None
    assert loaded.plan.city_names == ("南京", "西安")
    assert loaded.plan.total_days == 5
    assert [s.stay_days for s in loaded.plan.stays] == [2, 3]
    # 西安的第一天紧接南京的最后一天
    assert loaded.plan.stays[1].days[0].day == date(2026, 10, 3)


@pytest.mark.asyncio
async def test_removing_a_city_drops_its_days(db) -> None:
    from lushu.services.trip_store import load_trip

    trip_id = await _make_trip(
        [StaySpec("南京", 2, "320100"), StaySpec("西安", 2, "610100")], None
    )
    await update_stays(trip_id, specs=[StaySpec("南京", 2)])

    loaded = load_trip(trip_id)
    assert loaded is not None
    assert loaded.plan.city_names == ("南京",)
    assert loaded.plan.total_days == 2
    assert db.execute("SELECT COUNT(*) AS n FROM city_stay").fetchone()["n"] == 1


@pytest.mark.asyncio
async def test_changing_a_city_drops_that_city_s_old_content(db) -> None:
    """改了城市名等于换了一座城市，原来那天的事项不该跟过来。"""
    from lushu.services.trip_store import load_trip

    trip_id = await _make_trip([StaySpec("南京", 2, "320100")], None)
    await update_stays(trip_id, specs=[StaySpec("北京", 2)])

    loaded = load_trip(trip_id)
    assert loaded is not None
    assert loaded.plan.city_names == ("北京",)
    assert all(d.items == () for d in loaded.plan.stays[0].days)


@pytest.mark.asyncio
async def test_start_date_can_be_moved(db) -> None:
    from lushu.services.trip_store import load_trip

    trip_id = await _make_trip([StaySpec("南京", 2, "320100")], None)
    await update_stays(
        trip_id, specs=[StaySpec("南京", 2)], start_date=date(2026, 12, 24)
    )

    loaded = load_trip(trip_id)
    assert loaded is not None
    assert loaded.plan.start_date == date(2026, 12, 24)
    assert loaded.plan.stays[0].days[0].day == date(2026, 12, 24)


# ─── 名字 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_generated_name_follows_the_change(db) -> None:
    """自动生成的名字必须跟着变，否则会名不副实。"""
    from lushu.services.trip_store import load_trip

    trip_id = await _make_trip([StaySpec("南京", 3, "320100")], None)
    await update_stays(trip_id, specs=[StaySpec("南京", 2), StaySpec("西安", 3)])

    loaded = load_trip(trip_id)
    assert loaded is not None
    assert loaded.name == "南京、西安 5 天"


@pytest.mark.asyncio
async def test_custom_name_is_left_alone(db) -> None:
    from lushu.domain.planned import PlannedTrip, lay_out
    from lushu.services.trip_store import load_trip, save_planned_trip

    draft = PlannedTrip(
        name="国庆小长假",
        start_date=START,
        stays=lay_out(START, [StaySpec("南京", 2, "320100")]),
    )
    trip_id = save_planned_trip(draft)

    await update_stays(trip_id, specs=[StaySpec("南京", 4)])
    loaded = load_trip(trip_id)
    assert loaded is not None
    assert loaded.name == "国庆小长假"


# ─── 失败路径 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_trip_raises(db) -> None:
    with pytest.raises(LookupError):
        await update_stays("trip_不存在", specs=[StaySpec("南京", 2)])


@pytest.mark.asyncio
async def test_unknown_city_raises_and_changes_nothing(db) -> None:
    from lushu.services.trip_service import CityNotFoundError
    from lushu.services.trip_store import load_trip

    trip_id = await _make_trip([StaySpec("南京", 3, "320100")], None)

    with pytest.raises(CityNotFoundError):
        await update_stays(trip_id, specs=[StaySpec("南京", 1), StaySpec("查不到", 2)])

    loaded = load_trip(trip_id)
    assert loaded is not None
    assert loaded.plan.total_days == 3  # 原样未动
