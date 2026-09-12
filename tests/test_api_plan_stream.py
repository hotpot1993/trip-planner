"""规划流式接口的测试。

用打桩的 trip_service 驱动，不需要 Key 与网络。验证的是事件序列的形状：
阶段事件按顺序到达、结束时必有一条终止事件，且失败也能变成一条可读的事件
而不是断掉的连接。
"""

from __future__ import annotations

import json

import pytest

from lushu.engine import PlanStage
from lushu.services.trip_service import CityNotFoundError, MissingInputError


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """把 SSE 响应体拆成 (事件名, 数据) 列表。"""
    events: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        if name:
            events.append((name, json.loads(data) if data else {}))
    return events


@pytest.fixture
def stub_planner(monkeypatch: pytest.MonkeyPatch):
    """替换 trip_service.plan_and_save，按需产出阶段事件或抛错。"""
    from lushu.services import trip_service

    def install(*, stages=("intent", "planner", "finalize"), trip_id="trip_测试", error=None):
        # 流水线在跑之前会先检查 Key；这里模拟「环境配好了、引擎被替换掉」
        monkeypatch.setenv("AMAP_API_KEY", "测试用的高德 Key")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "测试用的模型 Key")

        async def fake_plan_and_save(request, *, on_stage=None):
            if on_stage:
                for node in stages:
                    on_stage(PlanStage(node=node, label=f"{node} 的中文名"))
            if error is not None:
                raise error
            return trip_service.PlannedTripResult(
                trip_id=trip_id,
                plan=_fake_plan(),
                stages=[],
            )

        monkeypatch.setattr(trip_service, "plan_and_save", fake_plan_and_save)
        return fake_plan_and_save

    return install


def _fake_plan():
    """一份形状正确的最小行程，用来构造 PlannedTripResult。"""
    from datetime import date

    from lushu.domain.planned import PlannedTrip, StaySpec, lay_out

    anchor = date(2026, 10, 1)
    return PlannedTrip(
        name="南京 3 天",
        start_date=anchor,
        stays=lay_out(anchor, [StaySpec("南京", 3, "320100")]),
    )


# ─── 成功路径 ────────────────────────────────────────────────


def test_stream_returns_event_stream_content_type(api_client, stub_planner) -> None:
    stub_planner()
    response = api_client.post("/api/plan/stream", json={"query": "南京三日游"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")


def test_stages_arrive_in_order(api_client, stub_planner) -> None:
    stub_planner(stages=("intent", "attraction_search", "planner", "finalize"))
    response = api_client.post("/api/plan/stream", json={"query": "南京三日游"})

    events = _parse_sse(response.text)
    stages = [data["node"] for name, data in events if name == "stage"]
    assert stages == ["intent", "attraction_search", "planner", "finalize"]


def test_stage_carries_a_readable_label(api_client, stub_planner) -> None:
    stub_planner(stages=("intent",))
    events = _parse_sse(api_client.post("/api/plan/stream", json={"query": "x"}).text)

    stage = next(data for name, data in events if name == "stage")
    assert stage["label"] == "intent 的中文名"


def test_stream_ends_with_done(api_client, stub_planner) -> None:
    stub_planner(trip_id="trip_abc123")
    events = _parse_sse(api_client.post("/api/plan/stream", json={"query": "x"}).text)

    name, data = events[-1]
    assert name == "done"
    assert data["trip_id"] == "trip_abc123"
    assert data["name"] == "南京 3 天"
    assert data["total_days"] == 3


def test_done_is_the_only_terminal_event_on_success(api_client, stub_planner) -> None:
    stub_planner()
    events = _parse_sse(api_client.post("/api/plan/stream", json={"query": "x"}).text)

    terminal = [name for name, _ in events if name in ("done", "error")]
    assert terminal == ["done"]


def test_query_reaches_the_service(api_client, stub_planner, monkeypatch) -> None:
    seen: dict = {}

    from lushu.services import trip_service

    async def fake(request, *, on_stage=None):
        seen["query"] = request.query
        seen["days"] = request.days
        return trip_service.PlannedTripResult(trip_id="trip_x", plan=_fake_plan(), stages=[])

    monkeypatch.setattr(trip_service, "plan_and_save", fake)
    api_client.post("/api/plan/stream", json={"query": "带父母去南京，别太赶", "days": 3})

    assert seen["query"] == "带父母去南京，别太赶"
    assert seen["days"] == 3


# ─── 失败路径 ────────────────────────────────────────────────


def test_missing_input_becomes_an_error_event(api_client, stub_planner) -> None:
    """对话式追问的依据：缺什么就推一条事件回去，连接不能断。"""
    stub_planner(error=MissingInputError(["出行日期", "天数"]))
    response = api_client.post("/api/plan/stream", json={"query": "出去玩"})

    assert response.status_code == 200  # 连接本身是成功的，错误在事件里
    events = _parse_sse(response.text)

    name, data = events[-1]
    assert name == "error"
    assert data["error"] == "missing_input"
    assert data["missing_fields"] == ["出行日期", "天数"]


def test_city_not_found_becomes_an_error_event(api_client, stub_planner) -> None:
    stub_planner(error=CityNotFoundError(["某个查不到的地方"]))
    events = _parse_sse(api_client.post("/api/plan/stream", json={"query": "x"}).text)

    name, data = events[-1]
    assert name == "error"
    assert data["error"] == "city_not_found"
    assert data["city_names"] == ["某个查不到的地方"]


def test_unexpected_failure_becomes_an_error_event(api_client, stub_planner) -> None:
    """程序缺陷也要变成一条可读事件，不能让前端对着断掉的连接干等。"""
    stub_planner(error=RuntimeError("引擎内部炸了"))
    events = _parse_sse(api_client.post("/api/plan/stream", json={"query": "x"}).text)

    name, data = events[-1]
    assert name == "error"
    assert data["error"] == "internal"
    assert "引擎内部炸了" in data["message"]


def test_stages_before_a_failure_are_still_delivered(api_client, stub_planner) -> None:
    """失败的进度仍要送达——用户至少知道卡在了哪一步。"""
    stub_planner(stages=("intent", "attraction_search"), error=MissingInputError(["日期"]))
    events = _parse_sse(api_client.post("/api/plan/stream", json={"query": "x"}).text)

    assert [data["node"] for name, data in events if name == "stage"] == [
        "intent",
        "attraction_search",
    ]
    assert events[-1][0] == "error"


# ─── 请求校验 ────────────────────────────────────────────────


def test_empty_query_is_rejected(api_client, stub_planner) -> None:
    stub_planner()
    assert api_client.post("/api/plan/stream", json={"query": ""}).status_code == 422


def test_frames_are_well_formed(api_client, stub_planner) -> None:
    """每一帧都必须是 `event: 名\\ndata: JSON\\n\\n` 的形状，否则前端解析会歪。"""
    stub_planner(stages=("intent",))
    text = api_client.post("/api/plan/stream", json={"query": "x"}).text

    for block in [b for b in text.split("\n\n") if b.strip()]:
        lines = block.splitlines()
        assert lines[0].startswith("event: "), block
        assert lines[1].startswith("data: "), block
        json.loads(lines[1][len("data: ") :])  # 必须是合法 JSON


# ─── 缺配置（真的跑一次才发现的分类问题）────────────────────


def test_missing_keys_reach_the_client_as_config_missing(
    api_client, monkeypatch
) -> None:
    """不替换任何东西，让真实的 plan_and_save 跑一遍。

    缺 Key 必须是一条 config_missing 事件，而不是 internal——
    前者告诉用户去填 .env.local，后者让人以为程序坏了。
    """
    monkeypatch.delenv("AMAP_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    response = api_client.post("/api/plan/stream", json={"query": "南京三日游"})
    assert response.status_code == 200

    name, data = _parse_sse(response.text)[-1]
    assert name == "error"
    assert data["error"] == "config_missing"
    assert "AMAP_API_KEY" in data["missing_keys"]
    assert ".env.local" in data["message"]
