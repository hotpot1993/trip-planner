"""金标准标注与评测接口的测试。

这一层要守的是一条态度：**样本不足时不许把数字说得比实际可信**。
所以「标注必须落回原文」「未标注的素材不进分母」「样本量与指标并排返回」
三件事都在这里钉住。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lushu.app import create_app
from lushu.services.ingest import ImportRequest, import_document
from lushu.store import initialize, transaction

BODY = """北京旅行篇章：故宫参观攻略

故宫现在只有午门能进，北门（神武门）只出不进。正确的走法是从天安门东站B口
出来，穿过天安门城楼的门洞，再往前走到午门。这一段路要走二十分钟左右。

故宫不卖现场票，全部要提前预约，提前7天的晚上8点在官方小程序放票。"""


class FakeLlm:
    def __init__(self, *claims: dict) -> None:
        self.payload = {"claims": list(claims)}

    def invoke(self, messages: list[dict]) -> Any:
        from lushu.adapters.extract import ExtractionOut

        return ExtractionOut.model_validate(self.payload)


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
def db(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "gold.db"
    initialize(path)
    monkeypatch.setattr("lushu.config.DB_PATH", path)
    return path


@pytest.fixture
def client(db: Path) -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def document_id(db: Path) -> str:
    from lushu.store import connect

    conn = connect()
    try:
        with conn:
            return import_document(
                ImportRequest(body=BODY, title="北京攻略"), conn=conn
            ).document_id
    finally:
        conn.close()


def _seed_poi(db: Path) -> None:
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO city (adcode, name, updated_at) VALUES ('110100', '北京', '2026-09-12')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO poi (amap_poi_id, name, city_adcode, adcode, typecode, "
            "lat_gcj02, lng_gcj02, fetched_at) VALUES "
            "('B000A8UIN8', '故宫博物院', '110100', '110101', '110201', 39.918, 116.397, "
            "'2026-09-12')"
        )


class TestDocumentList:
    def test_lists_documents_with_annotation_state(
        self, client: TestClient, document_id: str
    ) -> None:
        response = client.get("/api/workbench/gold")

        assert response.status_code == 200
        items = response.json()
        assert len(items) == 1
        assert items[0]["document_id"] == document_id
        assert items[0]["in_gold_set"] is False
        assert items[0]["labeled"] == 0


class TestContext:
    def test_returns_body_and_candidates(
        self, client: TestClient, db: Path, document_id: str, monkeypatch
    ) -> None:
        """标注时既要看得到原文，也要看得到模型说了什么。"""
        _seed_poi(db)
        monkeypatch.setattr("lushu.adapters.extract.extract", _fake_extract(a_claim()))
        from lushu.services.pipeline import extract_documents
        from lushu.store import connect

        conn = connect()
        try:
            extract_documents(conn=conn)
        finally:
            conn.close()

        response = client.get(f"/api/workbench/gold/{document_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["body"].startswith("北京旅行篇章")
        assert len(payload["candidates"]) == 1
        assert payload["candidates"][0]["subject_name"] == "故宫"
        assert payload["labels"] == []

    def test_unknown_document_is_404(self, client: TestClient) -> None:
        assert client.get("/api/workbench/gold/src_missing").status_code == 404


class TestAddLabel:
    def test_add_and_read_back(self, client: TestClient, document_id: str) -> None:
        response = client.post(
            f"/api/workbench/gold/{document_id}/labels",
            json={
                "quote": "故宫不卖现场票，全部要提前预约，",
                "polarity": "avoid",
                "subject_name": "故宫",
                "facet": "queue",
            },
        )

        assert response.status_code == 201
        label = response.json()
        assert label["char_start"] is not None
        assert label["verdict"] == "exact"

        context = client.get(f"/api/workbench/gold/{document_id}").json()
        assert len(context["labels"]) == 1

    def test_quote_not_in_body_is_422(self, client: TestClient, document_id: str) -> None:
        """引用原文里没有的话 → 422。金标准的可信度全靠这条。"""
        response = client.post(
            f"/api/workbench/gold/{document_id}/labels",
            json={"quote": "故宫每天限流八万人", "polarity": "avoid"},
        )

        assert response.status_code == 422
        assert "找不到" in response.json()["detail"]

    def test_bad_polarity_is_rejected(self, client: TestClient, document_id: str) -> None:
        response = client.post(
            f"/api/workbench/gold/{document_id}/labels",
            json={"quote": "故宫现在只有午门能进", "polarity": "maybe"},
        )
        assert response.status_code == 422

    def test_delete_label(self, client: TestClient, document_id: str) -> None:
        label = client.post(
            f"/api/workbench/gold/{document_id}/labels",
            json={"quote": "故宫现在只有午门能进", "polarity": "avoid"},
        ).json()

        assert client.delete(f"/api/workbench/gold/labels/{label['label_id']}").status_code == 204
        assert client.delete(f"/api/workbench/gold/labels/{label['label_id']}").status_code == 404


class TestDone:
    def test_mark_done_without_labels(self, client: TestClient, document_id: str) -> None:
        """一篇读完全是废话的素材：零标注也能标记完成，进召回率的分母。"""
        response = client.post(
            f"/api/workbench/gold/{document_id}/done", json={"done": True}
        )

        assert response.status_code == 200
        assert response.json()["in_gold_set"] is True

    def test_blind_annotation_is_recorded(self, client: TestClient, document_id: str) -> None:
        payload = client.post(
            f"/api/workbench/gold/{document_id}/done",
            json={"done": True, "model_output_seen": False, "annotator": "我"},
        ).json()
        assert payload["model_output_seen"] is False

    def test_unmark(self, client: TestClient, document_id: str) -> None:
        client.post(f"/api/workbench/gold/{document_id}/done", json={"done": True})
        payload = client.post(
            f"/api/workbench/gold/{document_id}/done", json={"done": False}
        ).json()
        assert payload["in_gold_set"] is False

    def test_unknown_document_is_404(self, client: TestClient) -> None:
        assert (
            client.post("/api/workbench/gold/src_x/done", json={"done": True}).status_code == 404
        )


class TestEvalEndpoint:
    def test_empty_eval_reports_none_not_zero(self, client: TestClient) -> None:
        """没有标注时三个指标是「算不出来」，不是 0。

        报 0 会让人以为「提纯一条都没对」，报 null 才是实话。
        """
        payload = client.get("/api/workbench/eval").json()

        assert payload["sample_size"] == 0
        assert payload["recall"] is None
        assert payload["precision"] is None
        assert payload["alignment_accuracy"] is None
        assert payload["ready"] is False

    def test_sample_size_is_reported_with_the_metrics(
        self, client: TestClient, db: Path, document_id: str, monkeypatch
    ) -> None:
        _seed_poi(db)
        monkeypatch.setattr("lushu.adapters.extract.extract", _fake_extract(a_claim()))
        from lushu.services.pipeline import extract_documents
        from lushu.store import connect

        conn = connect()
        try:
            extract_documents(conn=conn)
        finally:
            conn.close()

        client.post(
            f"/api/workbench/gold/{document_id}/labels",
            json={
                "quote": "故宫现在只有午门能进，北门（神武门）只出不进。",
                "polarity": "avoid",
                "subject_name": "故宫",
                "expected_poi_id": "B000A8UIN8",
            },
        )
        client.post(f"/api/workbench/gold/{document_id}/done", json={"done": True})

        payload = client.get("/api/workbench/eval").json()
        assert payload["sample_size"] == 1
        assert payload["ready"] is False  # 1 篇远不够 30 篇
        assert payload["hits"] == 1
        assert payload["recall"] == pytest.approx(1.0)
        assert payload["precision"] == pytest.approx(1.0)
        # 预测没跑过对齐，所以挂不上 POI：抽取算对，对齐算错
        assert payload["unaligned"] == 1
        assert payload["alignment_accuracy"] == pytest.approx(0.0)

    def test_missed_quotes_are_listed(
        self, client: TestClient, db: Path, document_id: str, monkeypatch
    ) -> None:
        """漏抽的原文片段要列出来——只看百分比无从下手。"""
        _seed_poi(db)
        monkeypatch.setattr("lushu.adapters.extract.extract", _fake_extract(a_claim()))
        from lushu.services.pipeline import extract_documents
        from lushu.store import connect

        conn = connect()
        try:
            extract_documents(conn=conn)
        finally:
            conn.close()

        client.post(
            f"/api/workbench/gold/{document_id}/labels",
            json={"quote": "故宫不卖现场票，全部要提前预约，", "polarity": "avoid"},
        )
        client.post(f"/api/workbench/gold/{document_id}/done", json={"done": True})

        payload = client.get("/api/workbench/eval").json()
        assert payload["documents"][0]["missed"] == ["故宫不卖现场票，全部要提前预约，"]
        assert payload["documents"][0]["predicted_total"] == 1


def _fake_extract(*claims: dict):
    """替换提纯适配器，但保留它自己的引文校验。

    真实的 `extract` 要在**替换之前**取到手：在替身里回调
    `lushu.adapters.extract.extract` 拿到的是替身自己，会无限递归——踩过一次。
    载荷也靠显式关键字传，不靠闭包。
    """
    from lushu.adapters.extract import extract as real_extract

    def patched(**kwargs):
        return real_extract(
            title=kwargs.get("title"),
            body=kwargs["body"],
            model="fake-model",
            conn=kwargs.get("conn"),
            llm=FakeLlm(*claims),
        )

    return patched
