"""行程落库与读取的测试。

这里的核心断言是**往返一致**：存进去什么形状，读出来就是什么形状。
它同时守护 ADR-0007（自有表是唯一真相）与 ADR-0004（天数口径）。
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from lushu.domain.planned import (
    ItemKind,
    PlannedDay,
    PlannedItem,
    PlannedStay,
    PlannedTrip,
    PoiFacts,
)
from lushu.services.trip_store import (
    UnresolvedCityError,
    delete_trip,
    list_trips,
    load_trip,
    replace_plan,
    save_planned_trip,
    set_status,
)
from lushu.store import connect, initialize

START = date(2026, 10, 1)
NANJING = "320100"
NANJING_LNG = 118.796877
NANJING_LAT = 32.060255


@pytest.fixture
def db(tmp_path) -> sqlite3.Connection:
    """一个建好表结构的临时库，测试结束时关闭。"""
    path = tmp_path / "lushu.db"
    initialize(path)
    conn = connect(path)
    yield conn
    conn.close()


def _facts(**overrides) -> PoiFacts:
    base = dict(
        lat_gcj02=NANJING_LAT,
        lng_gcj02=NANJING_LNG,
        address="南京市玄武区",
        tel="025-12345678",
        rating=4.7,
        open_time="08:30-17:00",
        photo="https://example.invalid/a.jpg",
    )
    base.update(overrides)
    return PoiFacts(**base)


def _item(title: str, poi_id: str | None = "B000A8UIN8", **kw) -> PlannedItem:
    if poi_id:
        return PlannedItem(
            kind=ItemKind.POI,
            title=title,
            poi_id=poi_id,
            start_time="09:00",
            end_time="11:30",
            note=f"{title}的贴士",
            facts=kw.pop("facts", _facts()),
            **kw,
        )
    return PlannedItem(kind=ItemKind.POI, title=title, unresolved_name=title, **kw)


def _draft(name: str = "南京 3 天") -> PlannedTrip:
    days = tuple(
        PlannedDay(
            day=date(2026, 10, 1 + i),
            seq_in_stay=i,
            theme=f"第 {i + 1} 天主题",
            items=(
                _item(f"景点{i + 1}", f"B000A8UIN{i}"),
                PlannedItem(kind=ItemKind.MEAL, title=f"餐厅{i + 1}", note="好吃"),
            ),
        )
        for i in range(3)
    )
    stay = PlannedStay(
        city_name="南京",
        city_adcode=NANJING,
        seq=0,
        days=days,
    )
    return PlannedTrip(name=name, start_date=START, stays=(stay,), query="南京三日游")


# ─── 落库与读回 ──────────────────────────────────────────────


def test_save_returns_a_trip_id(db: sqlite3.Connection) -> None:
    trip_id = save_planned_trip(_draft(), conn=db)
    assert trip_id.startswith("trip_")


def test_round_trip_preserves_the_whole_shape(db: sqlite3.Connection) -> None:
    original = _draft()
    trip_id = save_planned_trip(original, conn=db)
    loaded = load_trip(trip_id, conn=db)

    assert loaded is not None
    assert loaded.name == original.name
    assert loaded.status == "draft"
    assert loaded.plan.start_date == original.start_date
    assert loaded.plan.total_days == original.total_days
    assert loaded.plan.query == original.query
    assert loaded.plan.city_names == original.city_names


def test_round_trip_preserves_days_and_items(db: sqlite3.Connection) -> None:
    trip_id = save_planned_trip(_draft(), conn=db)
    loaded = load_trip(trip_id, conn=db)
    assert loaded is not None

    original_stay = _draft().stays[0]
    loaded_stay = loaded.plan.stays[0]

    assert [d.day for d in loaded_stay.days] == [d.day for d in original_stay.days]
    assert [d.theme for d in loaded_stay.days] == [d.theme for d in original_stay.days]
    assert [i.title for i in loaded_stay.days[0].items] == [
        i.title for i in original_stay.days[0].items
    ]


def test_round_trip_preserves_hard_facts_from_the_poi_table(db: sqlite3.Connection) -> None:
    """硬事实存在 poi 表里，读回时重新拼到天项上。"""
    trip_id = save_planned_trip(_draft(), conn=db)
    loaded = load_trip(trip_id, conn=db)
    assert loaded is not None

    item = loaded.plan.stays[0].days[0].items[0]
    assert item.facts is not None
    assert item.facts.lat_gcj02 == pytest.approx(NANJING_LAT)
    assert item.facts.lng_gcj02 == pytest.approx(NANJING_LNG)
    assert item.facts.rating == pytest.approx(4.7)
    assert item.facts.open_time == "08:30-17:00"
    assert item.facts.photo == "https://example.invalid/a.jpg"


def test_round_trip_preserves_item_times_and_notes(db: sqlite3.Connection) -> None:
    trip_id = save_planned_trip(_draft(), conn=db)
    loaded = load_trip(trip_id, conn=db)
    assert loaded is not None

    items = loaded.plan.stays[0].days[0].items
    assert items[0].start_time == "09:00"
    assert items[0].end_time == "11:30"
    assert items[0].note == "景点1的贴士"
    assert items[1].kind is ItemKind.MEAL


def test_meal_items_have_no_facts(db: sqlite3.Connection) -> None:
    trip_id = save_planned_trip(_draft(), conn=db)
    loaded = load_trip(trip_id, conn=db)
    assert loaded is not None
    assert loaded.plan.stays[0].days[0].items[1].facts is None


def test_poi_rows_are_written_with_gcj02_columns(db: sqlite3.Connection) -> None:
    save_planned_trip(_draft(), conn=db)

    row = db.execute(
        "SELECT name, lat_gcj02, lng_gcj02, city_adcode, rating FROM poi WHERE amap_poi_id = ?",
        ("B000A8UIN0",),
    ).fetchone()
    assert row is not None
    assert row["name"] == "景点1"
    assert row["lat_gcj02"] == pytest.approx(NANJING_LAT)
    assert row["lng_gcj02"] == pytest.approx(NANJING_LNG)
    assert row["city_adcode"] == NANJING


def test_city_row_is_created(db: sqlite3.Connection) -> None:
    save_planned_trip(_draft(), conn=db)
    row = db.execute("SELECT name FROM city WHERE adcode = ?", (NANJING,)).fetchone()
    assert row is not None and row["name"] == "南京"


def test_load_missing_trip_returns_none(db: sqlite3.Connection) -> None:
    assert load_trip("trip_不存在", conn=db) is None


def test_status_starts_as_draft(db: sqlite3.Connection) -> None:
    trip_id = save_planned_trip(_draft(), conn=db)
    loaded = load_trip(trip_id, conn=db)
    assert loaded is not None and loaded.status == "draft"


def test_set_status_confirms_the_trip(db: sqlite3.Connection) -> None:
    trip_id = save_planned_trip(_draft(), conn=db)
    set_status(trip_id, "confirmed", conn=db)
    loaded = load_trip(trip_id, conn=db)
    assert loaded is not None and loaded.status == "confirmed"


def test_unknown_status_is_rejected(db: sqlite3.Connection) -> None:
    trip_id = save_planned_trip(_draft(), conn=db)
    with pytest.raises(ValueError, match="未知的行程状态"):
        set_status(trip_id, "已取消", conn=db)


# ─── 城市解析的硬门禁 ────────────────────────────────────────


def test_missing_city_adcode_blocks_the_save(db: sqlite3.Connection) -> None:
    """城市名是人给的，行政区划代码必须以高德为准。解析不到时明确失败，
    绝不塞一个编造的代码进去——那会让天气与预算的城市归属全部错位。"""
    stay = PlannedStay(
        city_name="某个查不到的地方",
        city_adcode=None,
        seq=0,
        days=(PlannedDay(day=START, seq_in_stay=0, items=(_item("景点"),)),),
    )
    draft = PlannedTrip(name="测试", start_date=START, stays=(stay,))

    with pytest.raises(UnresolvedCityError, match="某个查不到的地方"):
        save_planned_trip(draft, conn=db)


def test_failed_save_rolls_back_the_whole_trip(db: sqlite3.Connection) -> None:
    """事务必须整体回滚。

    构造一个「第一座城市合法、第二座城市解析不到」的行程：这样行程行、
    第一个城市停留与它的天项都已经写进去了，失败必须把它们全部撤销，
    否则库里会留下半截行程。
    """
    good = PlannedStay(
        city_name="南京", city_adcode=NANJING, seq=0,
        days=(PlannedDay(day=START, seq_in_stay=0, items=(_item("中山陵"),)),),
    )
    bad = PlannedStay(
        city_name="查不到的地方", city_adcode=None, seq=1,
        days=(PlannedDay(day=date(2026, 10, 2), seq_in_stay=0, items=(_item("某景点"),)),),
    )

    with pytest.raises(UnresolvedCityError, match="查不到的地方"):
        save_planned_trip(
            PlannedTrip(name="半截行程", start_date=START, stays=(good, bad)), conn=db
        )

    for table in ("trip", "city_stay", "day", "day_item", "poi", "city"):
        count = db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        assert count == 0, f"{table} 里残留了 {count} 行，事务没有整体回滚"


# ─── 待对齐队列 ──────────────────────────────────────────────


def test_unresolved_spot_is_queued_for_alignment(db: sqlite3.Connection) -> None:
    stay = PlannedStay(
        city_name="南京", city_adcode=NANJING, seq=0,
        days=(PlannedDay(
            day=START, seq_in_stay=0, theme="第一天",
            items=(_item("某个对不上的景点", poi_id=None),),
        ),),
    )
    save_planned_trip(PlannedTrip(name="测试", start_date=START, stays=(stay,)), conn=db)

    row = db.execute(
        "SELECT mention_name, city_adcode, context_snippet, status FROM alignment_task"
    ).fetchone()
    assert row is not None
    assert row["mention_name"] == "某个对不上的景点"
    assert row["city_adcode"] == NANJING
    assert row["status"] == "pending"
    assert "南京" in row["context_snippet"]


def test_alignment_queue_does_not_duplicate_on_replan(db: sqlite3.Connection) -> None:
    """重新规划不能让待对齐队列堆出重复任务。"""
    stay = PlannedStay(
        city_name="南京", city_adcode=NANJING, seq=0,
        days=(PlannedDay(day=START, seq_in_stay=0, items=(_item("对不上", poi_id=None),)),),
    )
    draft = PlannedTrip(name="测试", start_date=START, stays=(stay,))

    trip_id = save_planned_trip(draft, conn=db)
    replace_plan(trip_id, draft, conn=db)
    replace_plan(trip_id, draft, conn=db)

    count = db.execute("SELECT COUNT(*) AS n FROM alignment_task").fetchone()["n"]
    assert count == 1


def test_aligned_spots_are_not_queued(db: sqlite3.Connection) -> None:
    save_planned_trip(_draft(), conn=db)
    count = db.execute("SELECT COUNT(*) AS n FROM alignment_task").fetchone()["n"]
    assert count == 0


# ─── POI 是实体真源，被多次行程复用时刷新而不重复 ────────────────


def test_saving_twice_does_not_duplicate_pois(db: sqlite3.Connection) -> None:
    save_planned_trip(_draft(), conn=db)
    save_planned_trip(_draft(name="第二次"), conn=db)

    count = db.execute("SELECT COUNT(*) AS n FROM poi").fetchone()["n"]
    assert count == 3  # 三次行程共用同一批 POI，不应变成 6


def test_reused_poi_refreshes_its_hard_facts(db: sqlite3.Connection) -> None:
    """同一个 POI 第二次出现时以新数据为准——硬事实只能来自官方接口（ADR-0001）。"""
    save_planned_trip(_draft(), conn=db)

    updated = PlannedStay(
        city_name="南京", city_adcode=NANJING, seq=0,
        days=(PlannedDay(
            day=START, seq_in_stay=0,
            items=(_item("景点1", "B000A8UIN0", facts=_facts(rating=4.9, open_time="09:00-18:00")),),
        ),),
    )
    save_planned_trip(
        PlannedTrip(name="刷新", start_date=START, stays=(updated,)), conn=db
    )

    row = db.execute(
        "SELECT rating, open_time FROM poi WHERE amap_poi_id = ?", ("B000A8UIN0",)
    ).fetchone()
    assert row is not None
    assert row["rating"] == pytest.approx(4.9)
    assert row["open_time"] == "09:00-18:00"


# ─── 列表、替换与删除 ────────────────────────────────────────


def test_list_trips_returns_a_summary(db: sqlite3.Connection) -> None:
    save_planned_trip(_draft(), conn=db)
    summaries = list_trips(conn=db)

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.name == "南京 3 天"
    assert summary.total_days == 3
    assert summary.city_names == ("南京",)
    assert summary.start_date == START


def test_list_trips_is_empty_initially(db: sqlite3.Connection) -> None:
    assert list_trips(conn=db) == []


def test_replace_plan_keeps_the_id_and_created_at(db: sqlite3.Connection) -> None:
    trip_id = save_planned_trip(_draft(), conn=db)
    before = load_trip(trip_id, conn=db)
    assert before is not None

    replace_plan(trip_id, _draft(name="南京 3 天（改）"), conn=db)
    after = load_trip(trip_id, conn=db)

    assert after is not None
    assert after.id == before.id
    assert after.created_at == before.created_at
    assert after.name == "南京 3 天（改）"


def test_replace_plan_swaps_the_days(db: sqlite3.Connection) -> None:
    """重新规划是替换而不是追加，否则天数会翻倍。"""
    trip_id = save_planned_trip(_draft(), conn=db)

    shorter = PlannedTrip(
        name="南京 1 天",
        start_date=START,
        stays=(
            PlannedStay(
                city_name="南京", city_adcode=NANJING, seq=0,
                days=(PlannedDay(day=START, seq_in_stay=0, items=(_item("只玩一天"),)),),
            ),
        ),
    )
    replace_plan(trip_id, shorter, conn=db)

    loaded = load_trip(trip_id, conn=db)
    assert loaded is not None
    assert loaded.plan.total_days == 1
    assert loaded.plan.stays[0].days[0].items[0].title == "只玩一天"


def test_replace_plan_leaves_no_orphan_days(db: sqlite3.Connection) -> None:
    trip_id = save_planned_trip(_draft(), conn=db)
    replace_plan(
        trip_id,
        PlannedTrip(
            name="南京 1 天", start_date=START,
            stays=(PlannedStay(
                city_name="南京", city_adcode=NANJING, seq=0,
                days=(PlannedDay(day=START, seq_in_stay=0, items=(_item("一天"),)),),
            ),),
        ),
        conn=db,
    )

    assert db.execute("SELECT COUNT(*) AS n FROM city_stay").fetchone()["n"] == 1
    assert db.execute("SELECT COUNT(*) AS n FROM day").fetchone()["n"] == 1
    assert db.execute("SELECT COUNT(*) AS n FROM day_item").fetchone()["n"] == 1


def test_replace_plan_on_missing_trip_raises(db: sqlite3.Connection) -> None:
    with pytest.raises(LookupError):
        replace_plan("trip_不存在", _draft(), conn=db)


def test_delete_trip_cascades(db: sqlite3.Connection) -> None:
    trip_id = save_planned_trip(_draft(), conn=db)
    assert delete_trip(trip_id, conn=db) is True

    assert db.execute("SELECT COUNT(*) AS n FROM trip").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM city_stay").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM day").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM day_item").fetchone()["n"] == 0


def test_delete_missing_trip_reports_false(db: sqlite3.Connection) -> None:
    assert delete_trip("trip_不存在", conn=db) is False


def test_deleting_a_trip_keeps_its_pois(db: sqlite3.Connection) -> None:
    """POI 是实体真源，不属于任何一次行程，删行程不该带走它。"""
    trip_id = save_planned_trip(_draft(), conn=db)
    delete_trip(trip_id, conn=db)

    assert db.execute("SELECT COUNT(*) AS n FROM poi").fetchone()["n"] == 3


# ─── 多城市形状已经就位 ──────────────────────────────────────


def test_two_city_trip_persists_and_loads(db: sqlite3.Connection) -> None:
    """M1 只规划单城市，但表结构与落库路径必须已经支持多城市。"""
    beijing = PlannedStay(
        city_name="北京", city_adcode="110100", seq=0,
        days=(
            PlannedDay(day=START, seq_in_stay=0, theme="故宫", items=(_item("故宫", "BJP001"),)),
            PlannedDay(day=date(2026, 10, 2), seq_in_stay=1, theme="长城", items=(_item("八达岭", "BJP002"),)),
        ),
    )
    xian = PlannedStay(
        city_name="西安", city_adcode="610100", seq=1,
        days=(PlannedDay(day=date(2026, 10, 3), seq_in_stay=0, theme="兵马俑", items=(_item("兵马俑", "XAP001"),)),),
    )
    draft = PlannedTrip(name="京西 3 天", start_date=START, stays=(beijing, xian))

    trip_id = save_planned_trip(draft, conn=db)
    loaded = load_trip(trip_id, conn=db)

    assert loaded is not None
    assert loaded.plan.city_names == ("北京", "西安")
    assert loaded.plan.total_days == 3
    assert loaded.plan.stays[1].days[0].day == date(2026, 10, 3)
