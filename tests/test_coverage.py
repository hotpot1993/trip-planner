"""行程的知识覆盖：哪些景点有人写过，哪些只是地图上恰好有。

设计 5.1 的封闭世界约束是「排程不得引入候选池之外的景点」。但候选池
**还没有接进排程的景点搜索**（那要改 vendored 的 `attraction_search_node`），
所以现在硬性拒绝整份行程会把每一份都毙掉——它会把高德搜出来的一切都判为违规。

有用的是如实报出来。这条信息本身就是产品价值：一个只是地图上有的地方，
没有任何人说过它值得去，也不知道要预约、什么时候去、注意什么。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lushu.domain.knowledge import ClaimEvidence
from lushu.services import coverage
from lushu.services import knowledge_store as ks
from lushu.store import connect, initialize, transaction
from tests.test_api_booking import stub_cities  # noqa: F401  （夹具靠导入进本模块）

NOW = "2026-09-13T10:00:00"
POI_COVERED = "B_COVERED"
POI_BARE = "B_BARE"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "coverage.db"
    initialize(path)
    return path


def _seed(db: Path, *, covered_claim: bool = True) -> None:
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO city (adcode, name, updated_at) VALUES ('110100', '北京', ?)",
            (NOW,),
        )
        for poi_id, name in ((POI_COVERED, "故宫博物院"), (POI_BARE, "某个没人提过的地方")):
            conn.execute(
                "INSERT INTO poi (amap_poi_id, name, city_adcode, adcode, typecode, "
                "lat_gcj02, lng_gcj02, fetched_at) VALUES (?, ?, '110100', '110101', "
                "'110201', 39.918, 116.397, ?)",
                (poi_id, name, NOW),
            )
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
            "VALUES ('di_1', 'day_1', 0, 'poi', ?, '故宫博物院', 'ai')",
            (POI_COVERED,),
        )
        conn.execute(
            "INSERT INTO day_item (id, day_id, seq, kind, poi_id, title, origin) "
            "VALUES ('di_2', 'day_1', 1, 'poi', ?, '某个没人提过的地方', 'ai')",
            (POI_BARE,),
        )
        if covered_claim:
            conn.execute(
                "INSERT INTO source_document (id, site, body_text, body_sha256, imported_at, "
                "import_kind) VALUES ('src_1', 'manual', '正文', 'sha_1', ?, 'paste')",
                (NOW,),
            )
            ks.save_claim(
                conn=conn,
                subject_type="poi",
                subject_name="故宫",
                poi_id=POI_COVERED,
                polarity="avoid",
                facet="entrance",
                text="只有午门能进",
                evidence=[ClaimEvidence(source_document_id="src_1", quote="只有午门能进")],
                first_seen_at=NOW,
                verify_due_at=None,
            )


class TestCoverage:
    def test_splits_recommended_from_bare(self, db: Path) -> None:
        _seed(db)
        result = coverage.coverage_for_trip("trip_1", conn=connect(db))

        assert result.total == 2
        assert result.recommended == 1
        assert [item.title for item in result.bare_items] == ["某个没人提过的地方"]

    def test_ratio_is_computed(self, db: Path) -> None:
        _seed(db)
        result = coverage.coverage_for_trip("trip_1", conn=connect(db))
        assert result.ratio == pytest.approx(0.5)

    def test_claim_count_travels_along(self, db: Path) -> None:
        _seed(db)
        result = coverage.coverage_for_trip("trip_1", conn=connect(db))
        covered = next(item for item in result.items if item.poi_id == POI_COVERED)
        assert covered.claim_count == 1
        assert covered.recommended

    def test_nothing_recommended_is_not_an_error(self, db: Path) -> None:
        """整份行程一个推荐都没有是正常的——这座城市还没有攻略数据。

        报的是事实，不是错误：候选池还没接进排程的景点搜索，
        现在硬性拒绝只会把每一份行程都毙掉。
        """
        _seed(db, covered_claim=False)
        result = coverage.coverage_for_trip("trip_1", conn=connect(db))
        assert result.recommended == 0
        assert result.total == 2
        assert result.ratio == 0.0

    def test_unresolved_items_are_counted(self, db: Path) -> None:
        """没对上实体的天项连「有没有人推荐过」都判不了，必须让人看见。"""
        _seed(db)
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO day_item (id, day_id, seq, kind, title, origin) "
                "VALUES ('di_3', 'day_1', 2, 'poi', '还没对齐的地方', 'ai')"
            )
        result = coverage.coverage_for_trip("trip_1", conn=connect(db))
        assert result.unresolved == 1

    def test_meals_are_not_counted(self, db: Path) -> None:
        _seed(db)
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO day_item (id, day_id, seq, kind, poi_id, title, origin) "
                "VALUES ('di_4', 'day_1', 3, 'meal', ?, '四季民福', 'ai')",
                (POI_BARE,),
            )
        result = coverage.coverage_for_trip("trip_1", conn=connect(db))
        # 餐饮不参与「景点有没有人推荐过」这件事
        assert result.total == 2

    def test_empty_trip_has_no_ratio(self, db: Path) -> None:
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO trip (id, name, start_date, created_at, updated_at) "
                "VALUES ('trip_2', '空行程', '2026-10-01', ?, ?)",
                (NOW, NOW),
            )
        result = coverage.coverage_for_trip("trip_2", conn=connect(db))
        # 一个景点都没有时比例算不出来，那是 None 不是 0
        assert result.ratio is None


class TestCoverageEndpoint:
    def test_reports_the_split(
        self,
        api_client,
        stub_cities,  # noqa: F811 — 模块级 import 进来的夹具
    ) -> None:
        created = api_client.post(
            "/api/trips",
            json={"start_date": "2026-10-01", "cities": [{"name": "北京", "days": 1}]},
        ).json()
        trip_id = created["id"]

        from lushu.config import DB_PATH
        from lushu.store import connect as real_connect

        conn = real_connect(DB_PATH)
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO poi (amap_poi_id, name, city_adcode, adcode, "
                    "typecode, lat_gcj02, lng_gcj02, fetched_at) VALUES "
                    "('B_G', '故宫博物院', '110100', '110101', '110201', 39.918, 116.397, ?)",
                    (NOW,),
                )
                day = conn.execute(
                    "SELECT id FROM day WHERE trip_id = ? LIMIT 1", (trip_id,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO day_item (id, day_id, seq, kind, poi_id, title, origin) "
                    "VALUES ('di_g', ?, 0, 'poi', 'B_G', '故宫博物院', 'ai')",
                    (day["id"],),
                )
        finally:
            conn.close()

        payload = api_client.get(f"/api/trips/{trip_id}/coverage").json()

        assert payload["total"] == 1
        # 没有任何结论 → 只是地图上有的那种
        assert payload["recommended"] == 0
        assert payload["ratio"] == 0.0
        assert payload["items"][0]["recommended"] is False

    def test_missing_trip_is_404(
        self,
        api_client,
        stub_cities,  # noqa: F811
    ) -> None:
        assert api_client.get("/api/trips/trip_nope/coverage").status_code == 404
