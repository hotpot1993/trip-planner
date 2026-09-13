"""把软经验挂到行程的天项上（设计 5.4）。

设计里的分层是：**硬约束注入 prompt**（开放时间、闭馆日、放票日），
**软经验生成后挂载**（避坑指南与打卡建议原文）。理由是后者不注入，
LLM 就无法把它们当成自己写的内容混进行程正文——编不出
「我在故宫拍到了没人的太和殿」。

所以这一层是只读的挂载，测试也盯这一点：不产生任何新内容，
只把知识库里已有的原文贴上去。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lushu.domain.knowledge import ClaimEvidence
from lushu.services import knowledge_store as ks
from lushu.services import trip_insights as ti
from lushu.store import connect, initialize, transaction
from tests.test_api_booking import stub_cities  # noqa: F401  （夹具靠导入进本模块）

NOW = "2026-09-13T10:00:00"
POI_ID = "B000A8UIN8"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "insights.db"
    initialize(path)
    return path


def _trip_with_item(db: Path, *, poi_id: str | None = POI_ID) -> str:
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO city (adcode, name, updated_at) VALUES ('110100', '北京', ?)",
            (NOW,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO poi (amap_poi_id, name, city_adcode, adcode, typecode, "
            "lat_gcj02, lng_gcj02, fetched_at) VALUES (?, '故宫博物院', '110100', '110101', "
            "'110201', 39.918, 116.397, ?)",
            (POI_ID, NOW),
        )
        conn.execute(
            "INSERT INTO trip (id, name, start_date, created_at, updated_at) "
            "VALUES ('trip_1', '北京三日', '2026-10-01', ?, ?)",
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
            (poi_id,),
        )
    return "trip_1"


def _claim(
    db: Path,
    *,
    text: str,
    polarity: str = "highlight",
    confidence: str = "high",
    sources: int = 3,
    poi_id: str = POI_ID,
) -> None:
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO source_document (id, site, body_text, body_sha256, "
            "imported_at, import_kind) VALUES (?, 'manual', '正文', ?, ?, 'paste')",
            (f"src_{text[:4]}", text[:8].encode().hex()[:12], NOW),
        )
        claim_id = ks.save_claim(
            conn=conn,
            subject_type="poi",
            subject_name="故宫",
            poi_id=poi_id,
            polarity=polarity,
            facet="photo" if polarity == "highlight" else "queue",
            text=text,
            evidence=[ClaimEvidence(source_document_id=f"src_{text[:4]}", quote=text)],
            first_seen_at=NOW,
            verify_due_at="2027-03-01",
        )
        conn.execute(
            "UPDATE claim SET confidence = ?, independent_source_count = ? WHERE id = ?",
            (confidence, sources, claim_id),
        )


class TestTripInsights:
    def test_attaches_claims_to_the_item(self, db: Path) -> None:
        _trip_with_item(db)
        _claim(db, text="想拍没人的太和殿要开门就冲")
        _claim(db, text="不卖现场票，必须提前预约", polarity="avoid")

        data = ti.insights_for_trip("trip_1", conn=connect(db))

        assert data.covered_items == 1
        assert data.total_claims == 2
        item = data.for_poi(POI_ID)
        assert item is not None
        assert [c.text for c in item.highlights] == ["想拍没人的太和殿要开门就冲"]
        assert [c.text for c in item.avoids] == ["不卖现场票，必须提前预约"]

    def test_keeps_the_original_wording(self, db: Path) -> None:
        """原文照登，不改一个字——转述会让「证据」变成「我们的说法」。"""
        _trip_with_item(db)
        _claim(db, text="珍宝馆和钟表馆在东边，要另外买票，各 10 块，值得看")

        data = ti.insights_for_trip("trip_1", conn=connect(db))
        item = data.for_poi(POI_ID)
        assert item is not None
        assert item.highlights[0].text == "珍宝馆和钟表馆在东边，要另外买票，各 10 块，值得看"

    def test_single_source_is_marked(self, db: Path) -> None:
        """单源的说法照样显示，但要让用户看得出它是单源。"""
        _trip_with_item(db)
        _claim(db, text="只有我一个人这么说", confidence="single_source", sources=1)

        item = ti.insights_for_trip("trip_1", conn=connect(db)).for_poi(POI_ID)
        assert item is not None
        assert item.highlights[0].single_source is True

    def test_high_confidence_comes_first(self, db: Path) -> None:
        _trip_with_item(db)
        _claim(db, text="单源的说法", confidence="single_source", sources=1)
        _claim(db, text="多源的说法", sources=5)

        item = ti.insights_for_trip("trip_1", conn=connect(db)).for_poi(POI_ID)
        assert item is not None
        assert item.highlights[0].text == "多源的说法"

    def test_item_without_poi_gets_nothing(self, db: Path) -> None:
        """没对上实体的天项挂不上任何知识。

        那正是 M3 的对齐要解决的问题，界面上用「待对齐」标出来，
        而不是在这里假装有内容。
        """
        _trip_with_item(db, poi_id=None)
        _claim(db, text="一条结论")

        data = ti.insights_for_trip("trip_1", conn=connect(db))
        assert data.covered_items == 0
        assert data.total_claims == 0

    def test_poi_without_claims_gets_nothing(self, db: Path) -> None:
        _trip_with_item(db)
        data = ti.insights_for_trip("trip_1", conn=connect(db))
        assert data.by_poi == {}

    def test_other_trips_are_not_mixed_in(self, db: Path) -> None:
        _trip_with_item(db)
        _claim(db, text="这条属于 trip_1")

        assert ti.insights_for_trip("trip_none", conn=connect(db)).total_claims == 0

    def test_only_poi_kind_items_count(self, db: Path) -> None:
        """餐饮与休息项挂的是同一张表，但它们不是景点，不该去查知识库。"""
        _trip_with_item(db)
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO day_item (id, day_id, seq, kind, poi_id, title, origin) "
                "VALUES ('di_2', 'day_1', 1, 'meal', ?, '四季民福', 'ai')",
                (POI_ID,),
            )
        _claim(db, text="一条结论")

        data = ti.insights_for_trip("trip_1", conn=connect(db))
        # 只算那一个 poi 项
        assert data.covered_items == 1
        assert data.total_claims == 1

    def test_display_cap_does_not_drop_the_query(self, db: Path) -> None:
        """`max_per_kind` 是显示上限：界面不一次铺完，但查得到全部。"""
        _trip_with_item(db)
        for index in range(5):
            _claim(db, text=f"第 {index} 条打卡建议")

        data = ti.insights_for_trip("trip_1", conn=connect(db), max_per_kind=3)
        item = data.for_poi(POI_ID)
        assert item is not None
        assert len(item.highlights) == 3


class TestInsightsEndpoint:
    def test_returns_grouped_insights(
        self,
        api_client,
        stub_cities,  # noqa: F811 — 模块级 import 进来的夹具，pytest 按名字找它
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
                conn.execute(
                    "INSERT INTO source_document (id, site, body_text, body_sha256, "
                    "imported_at, import_kind) VALUES ('src_i', 'manual', '正文', 'sha_i', ?, "
                    "'paste')",
                    (NOW,),
                )
                ks.save_claim(
                    conn=conn,
                    subject_type="poi",
                    subject_name="故宫",
                    poi_id="B_G",
                    polarity="avoid",
                    facet="entrance",
                    text="只有午门能进",
                    evidence=[ClaimEvidence(source_document_id="src_i", quote="只有午门能进")],
                    first_seen_at=NOW,
                    verify_due_at=None,
                )
        finally:
            conn.close()

        payload = api_client.get(f"/api/trips/{trip_id}/insights").json()

        assert payload["covered_items"] == 1
        assert payload["total_claims"] == 1
        item = payload["by_poi"]["B_G"]
        assert item["poi_name"] == "故宫博物院"
        assert item["avoids"][0]["text"] == "只有午门能进"
        assert item["avoids"][0]["single_source"] is True
        # 那句话由服务端出，前端只渲染（见 domain.knowledge.describe_confidence）
        assert item["avoids"][0]["confidence_text"] == "待验证的个例（只有 1 个来源）"

    def test_missing_trip_is_404(
        self,
        api_client,
        stub_cities,  # noqa: F811 — 同上
    ) -> None:
        assert api_client.get("/api/trips/trip_nope/insights").status_code == 404
