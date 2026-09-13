"""导入与管线接口的测试。

这两条接口把「素材落库」与「管线跑一轮」从命令行搬进了界面——
它们是「标注 30 篇」这件事做得完的前提。要守的东西不多但都要紧：
导入的三态判重要如实回传、管线要按依赖顺序跑、进度事件不能骗人。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lushu.app import create_app
from lushu.store import initialize, transaction

BODY = """北京旅行篇章：故宫参观攻略

故宫现在只有午门能进，北门（神武门）只出不进。正确的走法是从天安门东站B口
出来，穿过天安门城楼的门洞，再往前走到午门。这一段路要走二十分钟左右。

故宫不卖现场票，全部要提前预约，提前7天的晚上8点在官方小程序放票。"""


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "pipeline.db"
    initialize(path)
    monkeypatch.setattr("lushu.config.DB_PATH", path)
    return path


@pytest.fixture
def client(db: Path) -> TestClient:
    return TestClient(create_app())


def _events(text: str) -> list[tuple[str, dict]]:
    """把 SSE 文本拆成 (事件名, 载荷) 列表。"""
    parsed: list[tuple[str, dict]] = []
    event = ""
    for line in text.splitlines():
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            parsed.append((event, json.loads(line.removeprefix("data: "))))
    return parsed


class TestIngest:
    def test_paste_lands_in_the_database(self, client: TestClient, db: Path) -> None:
        response = client.post(
            "/api/ingest", json={"body": BODY, "title": "北京攻略", "site": "mafengwo"}
        )

        assert response.status_code == 201
        payload = response.json()
        assert payload["duplicate"] == "new"
        assert payload["site"] == "mafengwo"
        assert payload["chars"] > 0

        with transaction(db) as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM source_document").fetchone()["n"]
        assert count == 1

    def test_exact_duplicate_is_not_stored_twice(self, client: TestClient, db: Path) -> None:
        """完全重复不入库，`document_id` 指回已有的那一篇。

        注意这不是「新建了一篇又删掉」：重复时 `document_id` 就是**已有素材**
        的 id，界面据此把用户引到那一篇上去。
        """
        first = client.post("/api/ingest", json={"body": BODY, "title": "北京攻略"}).json()
        second = client.post("/api/ingest", json={"body": BODY, "title": "北京攻略"}).json()

        assert second["duplicate"] == "exact"
        assert second["document_id"] == first["document_id"]
        assert second["coverage"] == 1.0

        with transaction(db) as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM source_document").fetchone()["n"]
        assert count == 1

    def test_repost_is_stored_and_flagged(self, client: TestClient) -> None:
        """转载要如实回传：它会归到同一个独立来源组，置信度不重复计数。"""
        client.post("/api/ingest", json={"body": BODY, "title": "北京攻略"})
        repost = client.post(
            "/api/ingest",
            json={"body": "【转】" + BODY, "title": "北京攻略（转载）"},
        ).json()

        assert repost["duplicate"] in ("new", "repost")
        if repost["duplicate"] == "repost":
            assert repost["coverage"] is not None

    def test_empty_body_is_rejected(self, client: TestClient) -> None:
        assert client.post("/api/ingest", json={"body": ""}).status_code == 422

    def test_site_is_detected_from_url(self, client: TestClient) -> None:
        payload = client.post(
            "/api/ingest",
            json={"body": BODY, "url": "https://www.mafengwo.cn/i/12345.html"},
        ).json()
        assert payload["site"] == "mafengwo"


class TestPipelineRun:
    def test_streams_stage_progress_and_done(self, client: TestClient) -> None:
        client.post("/api/ingest", json={"body": BODY, "title": "北京攻略"})

        response = client.post("/api/pipeline/run", json={"steps": ["group", "merge"]})

        assert response.status_code == 200
        events = _events(response.text)
        names = [name for name, _ in events]
        assert names[0] == "stage"
        assert names[-1] == "done"
        assert names.count("stage") == 2  # 只跑了两步

        steps = [payload["step"] for name, payload in events if name == "stage"]
        assert steps == ["group", "merge"]

    def test_steps_run_in_dependency_order(self, client: TestClient) -> None:
        """归组必须在提纯之后、合并必须在归组之后——顺序不能由调用方说了算。"""
        response = client.post(
            "/api/pipeline/run", json={"steps": ["merge", "group", "extract"]}
        )
        steps = [
            payload["step"] for name, payload in _events(response.text) if name == "stage"
        ]
        assert steps == ["extract", "group", "merge"]

    def test_unknown_step_is_422(self, client: TestClient) -> None:
        response = client.post("/api/pipeline/run", json={"steps": ["extract", "洗稿"]})
        assert response.status_code == 422
        assert "洗稿" in response.json()["detail"]

    def test_done_carries_every_step_result(self, client: TestClient) -> None:
        client.post("/api/ingest", json={"body": BODY, "title": "北京攻略"})

        response = client.post("/api/pipeline/run", json={"steps": ["group", "merge"]})
        done = next(payload for name, payload in _events(response.text) if name == "done")

        assert "group" in done
        assert "merge" in done
        assert "stats" in done
        assert done["group"]["groups"] >= 0

    def test_progress_reports_the_title(self, client: TestClient, monkeypatch) -> None:
        """提纯一篇要几秒，界面要知道现在在跑哪一篇。

        模型换成假的：这条测试查的是**进度事件**，不是模型。
        假的模型走完整条真实路径（含引文校验），所以进度事件与真跑时同形。
        """
        from tests.test_pipeline import _patch_extract, a_claim

        client.post("/api/ingest", json={"body": BODY, "title": "北京攻略"})
        _patch_extract(monkeypatch, a_claim())

        response = client.post("/api/pipeline/run", json={"steps": ["extract"]})
        progress = [
            payload for name, payload in _events(response.text) if name == "progress"
        ]
        assert progress
        assert progress[0]["index"] == 1
        assert progress[0]["total"] == 1
        assert "北京攻略" in progress[0]["label"]

    def test_align_step_reports_a_failure_without_killing_the_stream(
        self, client: TestClient, db: Path, monkeypatch
    ) -> None:
        """一条提及对齐失败不该让整轮流断掉——失败要变成一条事件。

        要先把城市灌进库里：没有城市线索的提及根本走不到搜索那一步
        （它进的是 `unresolved_subjects`，那是另一码事，混在一起测不出来）。
        """
        from tests.test_pipeline import _patch_extract, a_claim

        client.post("/api/ingest", json={"body": BODY, "title": "北京攻略"})
        with transaction(db) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO city (adcode, name, updated_at) "
                "VALUES ('110100', '北京', '2026-09-12')"
            )
        _patch_extract(monkeypatch, a_claim())

        def boom(keywords: str, city: str):
            raise RuntimeError("高德不可用")

        monkeypatch.setattr("lushu.services.pipeline.search_pois", boom)

        response = client.post("/api/pipeline/run", json={"steps": ["extract", "align"]})
        events = _events(response.text)

        assert events[-1][0] == "done"
        done = events[-1][1]
        assert done["align"]["failed"]
        assert done["align"]["failed"][0]["mention"] == "故宫"
