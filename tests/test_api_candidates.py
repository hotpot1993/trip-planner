"""候选池接口。

M5 的验收条件是「打开城市看到的是网友推荐而非一片 POI」。这个接口就是那句话
的落点，所以测试盯的是**两类候选不能混**：`knowledge`（网友真的写过）
与 `amap`（只是地图上有）。把一片高德 POI 说成「推荐」是这套东西最不该犯的错。
"""

from __future__ import annotations

from lushu.api import candidate_routes  # noqa: F401  （确保路由可导入）
from lushu.domain.knowledge import ClaimEvidence
from lushu.services import knowledge_store as ks
from tests.test_api_booking import stub_cities  # noqa: F401  （夹具靠导入进本模块）

NOW = "2026-09-13T10:00:00"


def _seed_city_with_claims(api_client) -> None:
    from lushu.config import DB_PATH
    from lushu.store import connect as real_connect

    conn = real_connect(DB_PATH)
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO city (adcode, name, updated_at) "
                "VALUES ('110100', '北京', ?)",
                (NOW,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO poi (amap_poi_id, name, city_adcode, adcode, "
                "typecode, lat_gcj02, lng_gcj02, open_time, fetched_at) VALUES "
                "('B_GUGONG', '故宫博物院', '110100', '110101', '110201', 39.918, "
                "116.397, '08:30-17:00', ?)",
                (NOW,),
            )
            conn.execute(
                "INSERT INTO source_document (id, site, body_text, body_sha256, "
                "imported_at, import_kind) VALUES ('src_pool', 'manual', '正文', "
                "'sha_pool', ?, 'paste')",
                (NOW,),
            )
    finally:
        conn.close()


def _add_claim(*, poi_id: str, text: str, polarity: str, confidence: str = "high") -> None:
    from lushu.config import DB_PATH
    from lushu.store import connect as real_connect

    conn = real_connect(DB_PATH)
    try:
        with conn:
            claim_id = ks.save_claim(
                conn=conn,
                subject_type="poi",
                subject_name="故宫",
                poi_id=poi_id,
                polarity=polarity,
                facet="photo" if polarity == "highlight" else "queue",
                text=text,
                evidence=[ClaimEvidence(source_document_id="src_pool", quote=text)],
                first_seen_at=NOW,
                verify_due_at="2027-01-01",
            )
            conn.execute(
                "UPDATE claim SET confidence = ?, independent_source_count = 3 WHERE id = ?",
                (confidence, claim_id),
            )
    finally:
        conn.close()


class TestCityCandidates:
    def test_returns_recommended_places_with_their_claims(self, api_client) -> None:
        _seed_city_with_claims(api_client)
        _add_claim(poi_id="B_GUGONG", text="想拍没人的太和殿要开门就冲", polarity="highlight")
        _add_claim(poi_id="B_GUGONG", text="不卖现场票，必须提前预约", polarity="avoid")

        payload = api_client.get("/api/cities/110100/candidates").json()

        assert payload["city_adcode"] == "110100"
        assert len(payload["candidates"]) == 1
        item = payload["candidates"][0]
        assert item["name"] == "故宫博物院"
        assert item["source"] == "knowledge"
        assert item["recommended"] is True
        assert [claim["text"] for claim in item["highlights"]] == [
            "想拍没人的太和殿要开门就冲"
        ]
        assert [claim["text"] for claim in item["avoids"]] == ["不卖现场票，必须提前预约"]
        assert item["claim_count"] == 2

    def test_claim_carries_confidence_and_sources(self, api_client) -> None:
        """界面上要能显示「几个人这么说」——那是置信度的全部依据。"""
        _seed_city_with_claims(api_client)
        _add_claim(poi_id="B_GUGONG", text="一条多源说法", polarity="highlight")

        claim = api_client.get("/api/cities/110100/candidates").json()["candidates"][0][
            "highlights"
        ][0]

        assert claim["confidence"] == "high"
        assert claim["independent_source_count"] == 3
        assert claim["evidence_count"] == 1

    def test_the_sentence_comes_from_the_server(self, api_client) -> None:
        """「N 个独立来源」那句话由服务端出，前端不自己拼。

        原先行程页与城市页各写了一遍同一个三元表达式——两份措辞就是两个
        可以各自跑偏的地方，而其中一份已经把 2 个来源说成了「只有 1 个来源」。
        """
        _seed_city_with_claims(api_client)
        _add_claim(poi_id="B_GUGONG", text="一条多源说法", polarity="highlight")

        claim = api_client.get("/api/cities/110100/candidates").json()["candidates"][0][
            "highlights"
        ][0]

        assert claim["confidence_text"] == "3 个独立来源"

    def test_empty_city_without_name_is_422_not_an_empty_pool(self, api_client) -> None:
        """库里没有、也没给城市名时，回落不了。

        返回一个空候选池会让人以为「这座城市没什么可去的」——
        要说清缺的是城市名，而不是让人误解。
        """
        response = api_client.get("/api/cities/999999/candidates")

        assert response.status_code == 422
        assert "城市名" in response.json()["detail"]

    def test_covered_flag_is_reported(self, api_client) -> None:
        _seed_city_with_claims(api_client)
        _add_claim(poi_id="B_GUGONG", text="只有一条", polarity="highlight")

        payload = api_client.get("/api/cities/110100/candidates").json()
        # 一条撑不起规划，界面上要说实话
        assert payload["covered"] is False


class TestAmapFallback:
    def test_uncovered_city_falls_back_and_is_marked(self, api_client, monkeypatch) -> None:
        """回落搜高德时，候选必须标成 amap。

        把一片高德 POI 说成「网友推荐」是这套东西最不该犯的错。
        """
        from lushu.adapters.poi import PoiSearchResult
        from lushu.domain.poi import CandidatePoi

        found = CandidatePoi(
            poi_id="B_KM",
            name="滇池",
            typecode="110201",
            adcode="530102",
            city_name="昆明市",
            lng_gcj02=102.66,
            lat_gcj02=24.88,
        )

        def fake_search(keywords: str, city: str) -> PoiSearchResult:
            return PoiSearchResult(
                candidates=(found,), query=keywords, city=city, raw_count=1
            )

        monkeypatch.setattr("lushu.adapters.poi.search_pois", fake_search)

        payload = api_client.get("/api/cities/530100/candidates?name=昆明").json()

        assert len(payload["candidates"]) == 1
        item = payload["candidates"][0]
        assert item["source"] == "amap"
        assert item["recommended"] is False
        assert item["highlights"] == []
        assert payload["recommended_count"] == 0
        assert payload["covered"] is False
