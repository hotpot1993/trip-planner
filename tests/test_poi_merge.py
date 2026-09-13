"""把「同一处地方的两个实体」并成一个。

这是 ADR-0002 的一次补救：高德是实体真源，一个地方只该有一行 `poi`。
不合并的后果是具体的——预约规则挂在新实体上，而旧行程的天项指向旧实体，
于是规则永远匹配不到那些行程；M5 的候选池同理。

测试的重点是**合并的顺序**：先改引用再删旧行。反过来的话，要么撞外键，
要么留下一批指不到实体的引用（而外键约束只在特定情况下才拦得住）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lushu.domain.poi import PoiIdentity, pick_survivor
from lushu.services import poi_merge
from lushu.store import connect, initialize, transaction

NOW = "2026-09-13T10:00:00"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "merge.db"
    initialize(path)
    return path


def _poi(
    db: Path,
    poi_id: str,
    name: str,
    *,
    typecode: str | None = None,
    parent: str | None = None,
    adcode: str = "610103",
) -> None:
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO city (adcode, name, updated_at) VALUES ('610100', '西安', ?)",
            (NOW,),
        )
        conn.execute(
            "INSERT INTO poi (amap_poi_id, name, city_adcode, adcode, typecode, parent_poi_id, "
            "lat_gcj02, lng_gcj02, fetched_at) VALUES (?, ?, '610100', ?, ?, ?, 34.3, 108.9, ?)",
            (poi_id, name, adcode, typecode, parent, NOW),
        )


def _trip_item(db: Path, poi_id: str) -> None:
    """给某份行程排一项，指向这个 POI。"""
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO trip (id, name, start_date, created_at, updated_at) "
            "VALUES ('trip_1', '西安三日', '2026-10-01', ?, ?)",
            (NOW, NOW),
        )
        conn.execute(
            "INSERT OR IGNORE INTO city_stay (id, trip_id, city_adcode, city_name, seq, stay_days) "
            "VALUES ('cs_1', 'trip_1', '610100', '西安', 0, 3)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO day (id, trip_id, city_stay_id, date, seq_in_stay) "
            "VALUES ('day_1', 'trip_1', 'cs_1', '2026-10-01', 0)"
        )
        conn.execute(
            "INSERT INTO day_item (id, day_id, seq, kind, poi_id, title, origin) "
            "VALUES (?, 'day_1', 0, 'poi', ?, '某个景点', 'ai')",
            (f"di_{poi_id}", poi_id),
        )


def _rule(db: Path, poi_id: str) -> None:
    with transaction(db) as conn:
        conn.execute(
            "INSERT INTO booking_rule (poi_id, booking_required, advance_days, status, updated_at) "
            "VALUES (?, 1, 7, 'draft', ?)",
            (poi_id, NOW),
        )


class TestPickSurvivor:
    def test_prefers_the_row_with_amap_detail(self) -> None:
        """有 typecode 的说明真查过高德，而不是引擎回填的一个名字。"""
        loser = PoiIdentity(poi_id="X000A8UIN2", name="陕西历史博物馆")
        winner = PoiIdentity(poi_id="B001D03PEX", name="陕西历史博物馆", typecode="140100")
        assert pick_survivor([loser, winner]).poi_id == winner.poi_id

    def test_falls_back_to_the_amap_looking_id(self) -> None:
        a = PoiIdentity(poi_id="X000A8UIN2", name="某地")
        b = PoiIdentity(poi_id="B000A8UIN8", name="某地")
        assert pick_survivor([a, b]).poi_id == b.poi_id

    def test_then_prefers_the_more_referenced_row(self) -> None:
        a = PoiIdentity(poi_id="B1", name="某地", references=1)
        b = PoiIdentity(poi_id="B2", name="某地", references=9)
        assert pick_survivor([a, b]).poi_id == b.poi_id

    def test_is_stable_for_identical_rows(self) -> None:
        """同样的输入每次必须选出同一个赢家，否则「跑两次结果不一样」没法查。"""
        rows = [PoiIdentity(poi_id="B2", name="某地"), PoiIdentity(poi_id="B1", name="某地")]
        first = pick_survivor(rows).poi_id
        second = pick_survivor(list(reversed(rows))).poi_id
        assert first == second == "B1"

    def test_empty_gives_nothing(self) -> None:
        assert pick_survivor([]) is None


class TestFindDuplicates:
    def test_finds_same_name_pairs(self, db: Path) -> None:
        _poi(db, "X000A8UIN2", "陕西历史博物馆")
        _poi(db, "B001D03PEX", "陕西历史博物馆", typecode="140100")

        groups = poi_merge.find_duplicates(connect(db))
        assert len(groups) == 1
        assert groups[0].survivor.poi_id == "B001D03PEX"
        assert [item.poi_id for item in groups[0].losers] == ["X000A8UIN2"]

    def test_unique_names_are_not_reported(self, db: Path) -> None:
        _poi(db, "B1", "故宫博物院", typecode="110201")
        _poi(db, "B2", "天安门", typecode="110201")
        assert poi_merge.find_duplicates(connect(db)) == []

    def test_reference_counts_are_gathered(self, db: Path) -> None:
        _poi(db, "X1", "某景点")
        _poi(db, "B1", "某景点", typecode="140100")
        _trip_item(db, "X1")

        groups = poi_merge.find_duplicates(connect(db))
        loser = groups[0].losers[0]
        assert loser.references == 1


class TestMerge:
    def test_references_move_before_the_row_disappears(self, db: Path) -> None:
        """顺序反了就会撞外键，或者留下一批指不到实体的引用。"""
        _poi(db, "X000A8UIN2", "陕西历史博物馆")
        _poi(db, "B001D03PEX", "陕西历史博物馆", typecode="140100")
        _trip_item(db, "X000A8UIN2")
        _rule(db, "X000A8UIN2")

        groups, report = poi_merge.merge_duplicates(conn=connect(db))

        assert len(groups) == 1
        assert report.merged == 1
        assert report.moved["day_item.poi_id"] == 1
        assert report.moved["booking_rule.poi_id"] == 1

        conn = connect(db)
        assert conn.execute("SELECT COUNT(*) AS n FROM poi").fetchone()["n"] == 1
        assert (
            conn.execute("SELECT poi_id FROM day_item").fetchone()["poi_id"] == "B001D03PEX"
        )
        assert (
            conn.execute("SELECT poi_id FROM booking_rule").fetchone()["poi_id"]
            == "B001D03PEX"
        )

    def test_children_are_reparented(self, db: Path) -> None:
        _poi(db, "X1", "某景区")
        _poi(db, "B1", "某景区", typecode="110201")
        _poi(db, "B2", "某景区-停车场", typecode="150904", parent="X1")

        poi_merge.merge_duplicates(conn=connect(db))

        conn = connect(db)
        parent = conn.execute(
            "SELECT parent_poi_id FROM poi WHERE amap_poi_id = 'B2'"
        ).fetchone()["parent_poi_id"]
        assert parent == "B1"

    def test_no_self_parent_is_left_behind(self, db: Path) -> None:
        """旧实体可能正是留下那行的父节点。

        改完引用之后它会指向自己，而「沿 parent 链走到根」（ADR-0009）
        遇到自己当自己父节点就是个死循环。
        """
        _poi(db, "B1", "某景区", typecode="110201", parent="X1")
        _poi(db, "X1", "某景区")

        poi_merge.merge_duplicates(conn=connect(db))

        conn = connect(db)
        parent = conn.execute(
            "SELECT parent_poi_id FROM poi WHERE amap_poi_id = 'B1'"
        ).fetchone()["parent_poi_id"]
        assert parent is None

    def test_missing_fields_are_filled_not_overwritten(self, db: Path) -> None:
        """硬事实以官方接口为准（ADR-0001），留下的那行是查过高德的那行。"""
        _poi(db, "X1", "某景点")
        _poi(db, "B1", "某景点", typecode="140100")
        with transaction(db) as conn:
            conn.execute("UPDATE poi SET address = '旧地址' WHERE amap_poi_id = 'X1'")
            conn.execute("UPDATE poi SET address = '高德地址' WHERE amap_poi_id = 'B1'")

        poi_merge.merge_duplicates(conn=connect(db))

        conn = connect(db)
        assert (
            conn.execute("SELECT address FROM poi WHERE amap_poi_id = 'B1'").fetchone()[
                "address"
            ]
            == "高德地址"
        )

    def test_empty_field_is_filled_from_the_loser(self, db: Path) -> None:
        _poi(db, "X1", "某景点")
        _poi(db, "B1", "某景点", typecode="140100")
        with transaction(db) as conn:
            conn.execute("UPDATE poi SET address = '旧行里的地址' WHERE amap_poi_id = 'X1'")

        poi_merge.merge_duplicates(conn=connect(db))

        conn = connect(db)
        assert (
            conn.execute("SELECT address FROM poi WHERE amap_poi_id = 'B1'").fetchone()[
                "address"
            ]
            == "旧行里的地址"
        )

    def test_dry_run_writes_nothing(self, db: Path) -> None:
        _poi(db, "X1", "某景点")
        _poi(db, "B1", "某景点", typecode="140100")

        groups, report = poi_merge.merge_duplicates(conn=connect(db), dry_run=True)

        assert len(groups) == 1
        assert report.merged == 0
        assert connect(db).execute("SELECT COUNT(*) AS n FROM poi").fetchone()["n"] == 2

    def test_reference_counts_helps_before_and_after(self, db: Path) -> None:
        _poi(db, "X1", "某景点")
        _poi(db, "B1", "某景点", typecode="140100")
        _trip_item(db, "X1")

        conn = connect(db)
        assert poi_merge.reference_counts(conn, "X1") == {"day_item.poi_id": 1}
        assert poi_merge.reference_counts(conn, "B1") == {}
