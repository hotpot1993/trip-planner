"""数据工作台接口的测试。

工作台是「人工介入」的入口（Q47），它承载的是一条设计红线：
**接口只呈现，不自动决定**。对不上的提及不会因为「前五名里有一个看起来挺像」
就被自动接受——那正是跨城挂错、挂到停车场这类静默错误的来源。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lushu.app import create_app
from lushu.domain.poi import CandidatePoi
from lushu.services import knowledge_store as ks
from lushu.services.ingest import ImportRequest, import_document
from lushu.services.pipeline import extract_documents
from lushu.store import initialize, transaction

BODY = """北京旅行篇章：故宫参观攻略

故宫现在只有午门能进，北门（神武门）只出不进。正确的走法是从天安门东站B口
出来，穿过天安门城楼的门洞，再往前走到午门。这一段路要走二十分钟左右。

故宫不卖现场票，全部要提前预约，提前7天的晚上8点在官方小程序放票。"""


def gugong() -> CandidatePoi:
    return CandidatePoi(
        poi_id="B000A8UIN8",
        name="故宫博物院",
        typecode="110201",
        adcode="110101",
        city_name="北京市",
        lng_gcj02=116.397,
        lat_gcj02=39.918,
    )


class FakeLlm:
    def __init__(self, *claims: dict) -> None:
        self.payload = {"claims": list(claims)}

    def invoke(self, messages: list[dict]) -> Any:
        from lushu.adapters.extract import _ExtractionOut

        return _ExtractionOut.model_validate(self.payload)


def a_claim(**overrides: object) -> dict:
    base = {
        "subject_name": "故宫",
        "subject_type": "poi",
        "polarity": "avoid",
        "facet": "entrance",
        "text": "故宫只能从午门进入",
        "quote": "故宫现在只有午门能进，北门（神武门）只出不进。",
    }
    base.update(overrides)
    return base


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    """接到临时库上的测试客户端。

    `lushu.config.DB_PATH` 是运行时定下来的，此处把它换掉，
    让接口层读写这个临时库而不是真实的 data/lushu.db。
    """
    db = tmp_path / "test.db"
    initialize(db)
    monkeypatch.setattr("lushu.config.DB_PATH", db)
    # store.connect 的默认参数在导入时就绑定了，所以要换它而不是只换 config
    import lushu.store.connection as connection

    original = connection.connect

    def patched(db_path=None):
        return original(db_path if db_path is not None else db)

    monkeypatch.setattr(connection, "connect", patched)

    app = create_app()
    return TestClient(app)


def seed_city(db: Path, adcode: str = "110100", name: str = "北京") -> None:
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO city (adcode, name, updated_at) VALUES (?, ?, '2026-09-12')",
            (adcode, name),
        )


class TestStatsEndpoint:
    def test_reports_every_stage(self, client: TestClient) -> None:
        response = client.get("/api/workbench/stats")

        assert response.status_code == 200
        payload = response.json()
        for field in (
            "documents",
            "groups",
            "claims",
            "high_confidence",
            "evidence",
            "extract_documents",
            "align_pending",
        ):
            assert field in payload

    def test_empty_database_is_all_zeros(self, client: TestClient) -> None:
        payload = client.get("/api/workbench/stats").json()

        assert payload["documents"] == 0
        assert payload["claims"] == 0
        assert payload["align_pending"] == 0


class TestAlignmentsEndpoint:
    def _seed_pending(self, monkeypatch) -> None:
        """跑一遍提纯，把候选挂到待对齐队列上。"""
        import lushu.store.connection as connection
        from lushu.store import connect as real_connect

        db = connection.connect.__wrapped__ if hasattr(connection.connect, "__wrapped__") else None
        # 直接用被 patch 过的 connect（它的默认参数已经指向临时库）
        conn = real_connect()
        try:
            import_document(ImportRequest(body=BODY, title="北京攻略"), conn=conn)
            conn.execute(
                "INSERT INTO city (adcode, name, updated_at) VALUES ('110100', '北京', '2026-09-12')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO poi (amap_poi_id, name, city_adcode, adcode, typecode, "
                "lat_gcj02, lng_gcj02, fetched_at) VALUES "
                "('B000A8UIN8', '故宫博物院', '110100', '110101', '110201', 39.918, 116.397, "
                "'2026-09-12')"
            )
            conn.commit()
        finally:
            conn.close()

        from lushu.adapters.extract import extract as real_extract

        def patched(**kwargs):
            return real_extract(
                title=kwargs.get("title"),
                body=kwargs["body"],
                model="fake",
                conn=kwargs.get("conn"),
                llm=FakeLlm(a_claim()),
            )

        monkeypatch.setattr("lushu.adapters.extract.extract", patched)
        extract_documents()
        del db

    def test_lists_pending_with_candidates_and_claims(self, client: TestClient, monkeypatch) -> None:
        self._seed_pending(monkeypatch)

        response = client.get("/api/workbench/alignments")

        assert response.status_code == 200
        tasks = response.json()
        assert len(tasks) == 1
        task = tasks[0]
        assert task["mention_name"] == "故宫"
        assert task["source_title"] == "北京攻略"
        # 人工只看一个提及名是没法判断的，候选结论全文必须在
        assert len(task["claims"]) == 1
        assert task["claims"][0]["quote"] == "故宫现在只有午门能进，北门（神武门）只出不进。"

    def test_broken_json_in_a_row_does_not_break_the_page(
        self, client: TestClient, monkeypatch
    ) -> None:
        """库里的 JSON 不能盲信——坏数据不该让整个页面打不开。"""
        from lushu.store import connect as real_connect

        conn = real_connect()
        try:
            conn.execute(
                "INSERT INTO alignment_task (id, mention_name, candidate_pois_json, "
                "extracted_claims_json, created_at) VALUES "
                "('at_broken', '某个提及', '这不是 JSON', '[\"字符串不是对象\"]', '2026-09-12')"
            )
            conn.commit()
        finally:
            conn.close()

        response = client.get("/api/workbench/alignments")

        assert response.status_code == 200
        tasks = response.json()
        assert len(tasks) == 1
        assert tasks[0]["candidates"] == []
        assert tasks[0]["claims"] == []


class TestResolveAlignment:
    def _pending_task_id(self) -> str:
        from lushu.store import connect as real_connect

        conn = real_connect()
        try:
            row = conn.execute("SELECT id FROM alignment_task LIMIT 1").fetchone()
        finally:
            conn.close()
        assert row is not None
        return row["id"]

    def _seed(self, monkeypatch) -> None:
        from lushu.store import connect as real_connect

        conn = real_connect()
        try:
            import_document(ImportRequest(body=BODY, title="北京攻略"), conn=conn)
            conn.execute(
                "INSERT INTO city (adcode, name, updated_at) VALUES ('110100', '北京', '2026-09-12')"
            )
            conn.commit()
        finally:
            conn.close()

        from lushu.adapters.extract import extract as real_extract

        def patched(**kwargs):
            return real_extract(
                title=kwargs.get("title"),
                body=kwargs["body"],
                model="fake",
                conn=kwargs.get("conn"),
                llm=FakeLlm(a_claim()),
            )

        monkeypatch.setattr("lushu.adapters.extract.extract", patched)
        extract_documents()

    def test_resolving_to_a_poi_creates_a_claim(self, client: TestClient, monkeypatch) -> None:
        self._seed(monkeypatch)
        task_id = self._pending_task_id()

        response = client.post(
            f"/api/workbench/alignments/{task_id}/resolve",
            json={"poi_id": "B000A8UIN8"},
        )

        assert response.status_code == 200, response.text
        claim = response.json()
        assert claim["poi_id"] == "B000A8UIN8"
        assert claim["subject_name"] == "故宫"

        # 结论真的落库了
        claims = client.get("/api/workbench/claims").json()
        assert len(claims) == 1
        assert claims[0]["independent_source_count"] == 1
        assert claims[0]["confidence"] == "single_source"

    def test_evidence_is_reachable(self, client: TestClient, monkeypatch) -> None:
        """「这条说法到底谁说的」必须点得出来。"""
        self._seed(monkeypatch)
        task_id = self._pending_task_id()
        client.post(f"/api/workbench/alignments/{task_id}/resolve", json={"poi_id": "B000A8UIN8"})

        claim_id = client.get("/api/workbench/claims").json()[0]["claim_id"]
        response = client.get(f"/api/workbench/claims/{claim_id}/evidence")

        assert response.status_code == 200
        evidence = response.json()
        assert len(evidence) == 1
        assert "午门" in evidence[0]["quote"]

    def test_discard_marks_it_as_not_a_place(self, client: TestClient, monkeypatch) -> None:
        self._seed(monkeypatch)
        task_id = self._pending_task_id()

        response = client.post(
            f"/api/workbench/alignments/{task_id}/resolve", json={"discard": True}
        )

        assert response.status_code == 200
        assert response.json() is None
        # 队列里没有了，也没有生成结论
        assert client.get("/api/workbench/alignments").json() == []
        assert client.get("/api/workbench/claims").json() == []

    def test_missing_choice_is_rejected(self, client: TestClient, monkeypatch) -> None:
        """不给 poi_id 也不 discard 时不许含糊过去。"""
        self._seed(monkeypatch)
        task_id = self._pending_task_id()

        response = client.post(f"/api/workbench/alignments/{task_id}/resolve", json={})

        assert response.status_code == 422
        assert "discard" in response.text or "poi_id" in response.text

    def test_unknown_task_is_404(self, client: TestClient) -> None:
        response = client.post(
            "/api/workbench/alignments/at_nope/resolve", json={"poi_id": "B000A8UIN8"}
        )

        assert response.status_code == 404

    def test_double_resolve_is_409(self, client: TestClient, monkeypatch) -> None:
        """处置过的待办不许再处置一次。"""
        self._seed(monkeypatch)
        task_id = self._pending_task_id()
        client.post(f"/api/workbench/alignments/{task_id}/resolve", json={"poi_id": "B000A8UIN8"})

        response = client.post(
            f"/api/workbench/alignments/{task_id}/resolve", json={"poi_id": "B000A8UIN8"}
        )

        assert response.status_code == 409

    def test_unknown_poi_is_rejected_not_silently_accepted(
        self, client: TestClient, monkeypatch
    ) -> None:
        """查不到的 POI 不许当成对齐成功。"""
        self._seed(monkeypatch)
        task_id = self._pending_task_id()

        response = client.post(
            f"/api/workbench/alignments/{task_id}/resolve", json={"poi_id": "不存在的编号"}
        )

        assert response.status_code == 422


class TestExtractionsEndpoint:
    def test_dropped_counts_are_visible(self, client: TestClient, monkeypatch) -> None:
        """`dropped_count` 是「模型编了引文」的体检指标，必须看得见。"""
        from lushu.store import connect as real_connect

        conn = real_connect()
        try:
            import_document(ImportRequest(body=BODY, title="北京攻略"), conn=conn)
            conn.execute(
                "INSERT INTO city (adcode, name, updated_at) VALUES ('110100', '北京', '2026-09-12')"
            )
            conn.commit()
        finally:
            conn.close()

        from lushu.adapters.extract import extract as real_extract

        def patched(**kwargs):
            return real_extract(
                title=kwargs.get("title"),
                body=kwargs["body"],
                model="fake",
                conn=kwargs.get("conn"),
                llm=FakeLlm(
                    a_claim(),
                    a_claim(text="每天限流八万", quote="故宫每天限流八万人需要提前十天预约"),
                ),
            )

        monkeypatch.setattr("lushu.adapters.extract.extract", patched)
        extract_documents()

        response = client.get("/api/workbench/extractions")

        assert response.status_code == 200
        runs = response.json()
        assert len(runs) == 1
        assert runs[0]["candidate_count"] == 2
        assert runs[0]["accepted_count"] == 1
        assert runs[0]["dropped_count"] == 1
        assert runs[0]["source_title"] == "北京攻略"


class TestClaimsEndpoint:
    def test_filters_by_confidence(self, client: TestClient) -> None:
        from lushu.store import connect as real_connect

        conn = real_connect()
        try:
            conn.execute(
                "INSERT INTO city (adcode, name, updated_at) VALUES ('110100', '北京', '2026-09-12')"
            )
            conn.execute(
                "INSERT INTO poi (amap_poi_id, name, city_adcode, lat_gcj02, lng_gcj02, fetched_at) "
                "VALUES ('B000A8UIN8', '故宫博物院', '110100', 39.918, 116.397, '2026-09-12')"
            )
            conn.commit()
        finally:
            conn.close()

        from lushu.store import transaction as real_transaction

        with real_transaction() as conn:
            from lushu.domain.knowledge import ClaimEvidence

            conn.execute(
                "INSERT INTO source_document (id, site, body_text, body_sha256, imported_at, "
                "import_kind) VALUES ('src_a', 'manual', '正文', 'sha', '2026-09-12', 'paste')"
            )
            ks.save_claim(
                conn=conn,
                subject_type="poi",
                subject_name="故宫",
                poi_id="B000A8UIN8",
                polarity="avoid",
                facet="entrance",
                text="只有午门能进",
                evidence=[ClaimEvidence(source_document_id="src_a", quote="只有午门能进")],
                first_seen_at="2026-09-12",
                verify_due_at=None,
            )

        assert len(client.get("/api/workbench/claims?confidence=single_source").json()) == 1
        assert client.get("/api/workbench/claims?confidence=high").json() == []

    def test_invalid_confidence_is_rejected(self, client: TestClient) -> None:
        assert client.get("/api/workbench/claims?confidence=差不多吧").status_code == 422
