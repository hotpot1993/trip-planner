"""重新对齐：把落点过时的引用搬到新实体上。

`align_pending` 只处理待办的提及，一条待办解析之后就不会被重新检查——
所以**对齐算法的每一次改进都只对新数据生效**。这个工具补上那一步。

测试盯两件事：

1. **两类引用一起搬**：结论（`claim`）与行程天项（`day_item`）。
   只搬结论会留下更隐蔽的毛病——结论到了新实体上，而行程还指着旧实体，
   于是那些结论在行程上完全看不见。实测中山陵就是这样。
2. **算不出来就跳过，不猜**：目标实体没入库时写进去，而不是留下一个
   指不到东西的引用或被外键挡下。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lushu.domain.knowledge import ClaimEvidence
from lushu.domain.poi import CandidatePoi
from lushu.services import knowledge_store as ks
from lushu.services import realign
from lushu.store import connect, initialize, transaction

NOW = "2026-09-13T10:00:00"
OLD = "B_OLD"
NEW = "B_NEW"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "realign.db"
    initialize(path)
    return path


def _seed_poi(db: Path, poi_id: str, name: str, *, parent: str | None = None) -> None:
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO city (adcode, name, updated_at) VALUES ('110100', '北京', ?)",
            (NOW,),
        )
        conn.execute(
            "INSERT INTO poi (amap_poi_id, name, city_adcode, adcode, typecode, parent_poi_id, "
            "lat_gcj02, lng_gcj02, fetched_at) VALUES (?, ?, '110100', '110101', '110201', "
            "?, 39.918, 116.397, ?)",
            (poi_id, name, parent, NOW),
        )


def _trip_item(db: Path, *, poi_id: str, title: str, trip_id: str = "trip_1") -> None:
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO trip (id, name, start_date, created_at, updated_at) "
            "VALUES (?, '北京一日', '2026-10-01', ?, ?)",
            (trip_id, NOW, NOW),
        )
        conn.execute(
            "INSERT OR IGNORE INTO city_stay (id, trip_id, city_adcode, city_name, seq, stay_days) "
            "VALUES (?, ?, '110100', '北京', 0, 1)",
            (f"cs_{trip_id}", trip_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO day (id, trip_id, city_stay_id, date, seq_in_stay) "
            "VALUES (?, ?, ?, '2026-10-01', 0)",
            (f"day_{trip_id}", trip_id, f"cs_{trip_id}"),
        )
        conn.execute(
            "INSERT INTO day_item (id, day_id, seq, kind, poi_id, title, origin) "
            "VALUES (?, ?, 0, 'poi', ?, ?, 'ai')",
            (f"di_{poi_id}_{trip_id}", f"day_{trip_id}", poi_id, title),
        )


def _claim(db: Path, *, poi_id: str, subject: str, text: str = "只有午门能进") -> None:
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO source_document (id, site, body_text, body_sha256, "
            "imported_at, import_kind) VALUES ('src_1', 'manual', '正文', 'sha_1', ?, 'paste')",
            (NOW,),
        )
        ks.save_claim(
            conn=conn,
            subject_type="poi",
            subject_name=subject,
            poi_id=poi_id,
            polarity="avoid",
            facet="entrance",
            text=text,
            evidence=[ClaimEvidence(source_document_id="src_1", quote=text)],
            first_seen_at=NOW,
            verify_due_at=None,
        )


def _resolved(poi_id: str, name: str) -> CandidatePoi:
    return CandidatePoi(
        poi_id=poi_id,
        name=name,
        typecode="110201",
        adcode="110101",
        city_name="北京市",
        lng_gcj02=116.4,
        lat_gcj02=39.9,
    )


class TestStaleSubjects:
    def test_collects_claim_references(self, db: Path) -> None:
        _seed_poi(db, OLD, "中山陵")
        _claim(db, poi_id=OLD, subject="中山陵")

        subjects = realign.stale_subjects(conn=connect(db))

        assert len(subjects) == 1
        assert subjects[0].claim_count == 1
        assert subjects[0].item_count == 0

    def test_collects_itinerary_references(self, db: Path) -> None:
        """**行程天项也是引用。** 只收结论的话，结论搬走了而行程还指着旧实体，
        那些结论在行程上就永远看不见——实测中山陵就是这样。"""
        _seed_poi(db, OLD, "中山陵")
        _trip_item(db, poi_id=OLD, title="中山陵")

        subjects = realign.stale_subjects(conn=connect(db))

        assert len(subjects) == 1
        assert subjects[0].item_count == 1
        assert subjects[0].subject_name == "中山陵"

    def test_merges_both_kinds_of_reference(self, db: Path) -> None:
        _seed_poi(db, OLD, "中山陵")
        _claim(db, poi_id=OLD, subject="中山陵")
        _trip_item(db, poi_id=OLD, title="中山陵")

        subjects = realign.stale_subjects(conn=connect(db))

        assert len(subjects) == 1
        assert subjects[0].claim_count == 1
        assert subjects[0].item_count == 1
        assert subjects[0].reference_count == 2

    def test_meals_are_not_collected(self, db: Path) -> None:
        """餐饮不是景点，不参与实体对齐。"""
        _seed_poi(db, OLD, "某餐厅")
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO trip (id, name, start_date, created_at, updated_at) "
                "VALUES ('trip_1', '北京一日', '2026-10-01', ?, ?)",
                (NOW, NOW),
            )
            conn.execute(
                "INSERT INTO city_stay (id, trip_id, city_adcode, city_name, seq, stay_days) "
                "VALUES ('cs_1', 'trip_1', '110100', '北京', 0, 1)"
            )
            conn.execute(
                "INSERT INTO day (id, trip_id, city_stay_id, date, seq_in_stay) "
                "VALUES ('day_1', 'trip_1', 'cs_1', '2026-10-01', 0)"
            )
            conn.execute(
                "INSERT INTO day_item (id, day_id, seq, kind, poi_id, title, origin) "
                "VALUES ('di_1', 'day_1', 0, 'meal', ?, '某餐厅', 'ai')",
                (OLD,),
            )
        assert realign.stale_subjects(conn=connect(db)) == []


class TestApplyPlans:
    def _plan(self, subjects, resolved) -> realign.RealignPlan:
        return realign.RealignPlan(
            subject_name=subjects.subject_name,
            current_poi_id=subjects.poi_id,
            current_name=subjects.poi_name,
            new_poi_id=resolved.poi_id,
            new_name=resolved.name,
            reason="名字命中",
            claim_count=subjects.claim_count,
            item_count=subjects.item_count,
            resolved=resolved,
            resolved_city_adcode="110100",
        )

    def test_moves_claims_and_items_together(self, db: Path) -> None:
        _seed_poi(db, OLD, "中山陵")
        _seed_poi(db, NEW, "中山陵景区")
        _claim(db, poi_id=OLD, subject="中山陵")
        _trip_item(db, poi_id=OLD, title="中山陵")

        with transaction(db) as conn:
            plan = self._plan(
                realign.stale_subjects(conn=conn)[0], _resolved(NEW, "中山陵景区")
            )
            report = realign.apply_plans([plan], conn=conn)

        assert report.moved_claims == 1
        assert report.moved_items == 1
        assert report.moved_subjects == 1

        conn = connect(db)
        assert conn.execute("SELECT poi_id FROM claim").fetchone()["poi_id"] == NEW
        assert conn.execute("SELECT poi_id FROM day_item").fetchone()["poi_id"] == NEW

    def test_item_title_is_not_touched(self, db: Path) -> None:
        """标题是人看到的景点名，与它指向哪个实体是两回事。"""
        _seed_poi(db, OLD, "中山陵")
        _seed_poi(db, NEW, "中山陵景区")
        _trip_item(db, poi_id=OLD, title="中山陵")

        with transaction(db) as conn:
            plan = self._plan(
                realign.stale_subjects(conn=conn)[0], _resolved(NEW, "中山陵景区")
            )
            realign.apply_plans([plan], conn=conn)

        row = connect(db).execute("SELECT title FROM day_item").fetchone()
        assert row["title"] == "中山陵"

    def test_writes_the_target_entity_if_missing(self, db: Path) -> None:
        """「重跑一遍对齐」该有和跑对齐一样的效果，包括把认定的实体落库。

        否则会撞上先有鸡还是先有蛋：目标实体没入库，引用就搬不过去。
        """
        _seed_poi(db, OLD, "西安城墙")
        _trip_item(db, poi_id=OLD, title="西安城墙")

        with transaction(db) as conn:
            plan = self._plan(
                realign.stale_subjects(conn=conn)[0], _resolved(NEW, "西安城墙")
            )
            report = realign.apply_plans([plan], conn=conn)

        assert report.wrote_pois == 1
        assert report.moved_items == 1
        conn = connect(db)
        assert conn.execute("SELECT COUNT(*) AS n FROM poi").fetchone()["n"] == 2
        assert conn.execute("SELECT poi_id FROM day_item").fetchone()["poi_id"] == NEW

    def test_unchanged_plans_are_left_alone(self, db: Path) -> None:
        _seed_poi(db, OLD, "中山陵")
        _trip_item(db, poi_id=OLD, title="中山陵")

        plan = realign.RealignPlan(
            subject_name="中山陵",
            current_poi_id=OLD,
            current_name="中山陵",
            new_poi_id=None,
            new_name=None,
            reason="高德现在搜不到这个提及",
            item_count=1,
        )
        with transaction(db) as conn:
            report = realign.apply_plans([plan], conn=conn)

        assert report.moved_items == 0
        assert connect(db).execute("SELECT poi_id FROM day_item").fetchone()["poi_id"] == OLD

    def test_records_why_a_plan_was_skipped(self, db: Path) -> None:
        """算不出来时跳过并说明，不留下一个指不到东西的引用。"""
        _seed_poi(db, OLD, "中山陵")
        _trip_item(db, poi_id=OLD, title="中山陵")

        plan = realign.RealignPlan(
            subject_name="中山陵",
            current_poi_id=OLD,
            current_name="中山陵",
            new_poi_id=NEW,
            new_name="中山陵景区",
            reason="名字命中",
            item_count=1,
            resolved=None,  # 调用方没给出实体对象，也就写不进去
        )
        with transaction(db) as conn:
            report = realign.apply_plans([plan], conn=conn)

        assert report.skipped
        assert "还没入库" in report.skipped[0][1]
        assert connect(db).execute("SELECT poi_id FROM day_item").fetchone()["poi_id"] == OLD


class TestRealignSubject:
    def test_unchanged_when_city_is_unknown(self, db: Path) -> None:
        subject = realign.StaleSubject(
            subject_name="某地",
            poi_id=OLD,
            poi_name="某地",
            city_adcode=None,
            city_name=None,
        )
        plan = realign.realign_subject(
            subject, search=lambda *_: None, fetch=lambda *_: None
        )
        assert not plan.changed
        assert "城市线索" in plan.reason

    def test_unchanged_when_search_finds_nothing(self, db: Path) -> None:
        from lushu.adapters.poi import PoiSearchResult

        subject = realign.StaleSubject(
            subject_name="某地",
            poi_id=OLD,
            poi_name="某地",
            city_adcode="110100",
            city_name="北京",
        )
        plan = realign.realign_subject(
            subject,
            search=lambda k, c: PoiSearchResult(candidates=(), query=k, city=c, raw_count=0),
            fetch=lambda *_: None,
        )
        assert not plan.changed
        assert "搜不到" in plan.reason
