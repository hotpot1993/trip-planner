"""规划流水线包装的测试。

用假的 run_stream 驱动，不需要 API Key 也不需要网络。验证的是：
包装层忠实地把阶段事件与结果事件分流，且只把有值的覆盖项传给引擎
（显式传 None 会覆盖掉引擎自己的推导）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from lushu.engine.planning import PlanOutcome, PlanStage, run_plan, stream_plan

FAKE_PLAN: dict[str, Any] = {
    "destination": "南京",
    "start_date": "2026-10-01",
    "days": [{"day": 1, "date": "2026-10-01", "theme": "钟山", "timeline": []}],
}


@pytest.fixture
def fake_run_stream(monkeypatch: pytest.MonkeyPatch):
    """替换引擎的 run_stream，并记录它收到的覆盖项。"""
    from third_party.floattrip.planning import graph as engine_graph

    captured: dict[str, Any] = {}

    def install(events: list[dict]) -> dict[str, Any]:
        async def fake(query: str, **overrides: Any):
            captured["query"] = query
            captured["overrides"] = overrides
            for event in events:
                yield event

        monkeypatch.setattr(engine_graph, "run_stream", fake)
        return captured

    return install


def _typical_events() -> list[dict]:
    return [
        {"type": "stage", "node": "intent", "label": "理解需求"},
        {"type": "stage", "node": "attraction_search", "label": "搜索景点"},
        {"type": "stage", "node": "finalize", "label": "组装计划"},
        {
            "type": "result",
            "success": True,
            "missing_fields": [],
            "history": ["intent：识别到南京"],
            "plan": FAKE_PLAN,
        },
    ]


# ─── 事件分流 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stages_are_yielded_before_the_outcome(fake_run_stream) -> None:
    fake_run_stream(_typical_events())

    seen: list[Any] = []
    async for event in stream_plan("南京三日游"):
        seen.append(event)

    assert isinstance(seen[-1], PlanOutcome)
    assert all(isinstance(e, PlanStage) for e in seen[:-1])
    assert len(seen) == 4


@pytest.mark.asyncio
async def test_stage_labels_are_carried(fake_run_stream) -> None:
    fake_run_stream(_typical_events())

    stages = [e async for e in stream_plan("x") if isinstance(e, PlanStage)]
    assert [s.node for s in stages] == ["intent", "attraction_search", "finalize"]
    assert stages[0].label == "理解需求"


@pytest.mark.asyncio
async def test_outcome_carries_the_plan(fake_run_stream) -> None:
    fake_run_stream(_typical_events())

    outcome = await run_plan("南京三日游")
    assert outcome.success is True
    assert outcome.plan == FAKE_PLAN
    assert outcome.missing_fields == []


@pytest.mark.asyncio
async def test_outcome_records_the_stages_it_saw(fake_run_stream) -> None:
    fake_run_stream(_typical_events())
    outcome = await run_plan("南京三日游")

    assert [s.node for s in outcome.stages] == ["intent", "attraction_search", "finalize"]


@pytest.mark.asyncio
async def test_history_is_carried(fake_run_stream) -> None:
    fake_run_stream(_typical_events())
    outcome = await run_plan("南京三日游")
    assert outcome.history == ["intent：识别到南京"]


@pytest.mark.asyncio
async def test_on_stage_callback_sees_every_stage(fake_run_stream) -> None:
    fake_run_stream(_typical_events())

    seen: list[str] = []
    await run_plan("南京三日游", on_stage=lambda s: seen.append(s.node))

    assert seen == ["intent", "attraction_search", "finalize"]


# ─── 缺信息的路径 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_fields_reach_the_caller(fake_run_stream) -> None:
    """对话式追问的依据：引擎说缺什么，我们就问什么。"""
    fake_run_stream([
        {"type": "stage", "node": "intent", "label": "理解需求"},
        {
            "type": "result",
            "success": False,
            "missing_fields": ["出行日期", "天数"],
            "history": [],
            "plan": None,
        },
    ])

    outcome = await run_plan("出去玩")
    assert outcome.success is False
    assert outcome.plan is None
    assert outcome.missing_fields == ["出行日期", "天数"]


# ─── 覆盖项 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_only_provided_overrides_are_forwarded(fake_run_stream) -> None:
    """显式传 None 会覆盖引擎自己的推导，所以没值就不传。"""
    captured = fake_run_stream(_typical_events())
    await run_plan("南京三日游")

    assert captured["overrides"] == {}


@pytest.mark.asyncio
async def test_provided_overrides_are_forwarded(fake_run_stream) -> None:
    captured = fake_run_stream(_typical_events())
    await run_plan(
        "南京三日游",
        start_date=date(2026, 10, 1),
        days=3,
        destination="南京",
        max_review_rounds=1,
    )

    overrides = captured["overrides"]
    assert overrides["travel_start_date"] == date(2026, 10, 1)
    assert overrides["days"] == 3
    assert overrides["destination"] == "南京"
    assert overrides["max_review_rounds"] == 1


@pytest.mark.asyncio
async def test_query_is_passed_through(fake_run_stream) -> None:
    captured = fake_run_stream(_typical_events())
    await run_plan("带父母去南京玩三天，不要太赶")

    assert captured["query"] == "带父母去南京玩三天，不要太赶"


@pytest.mark.asyncio
async def test_unknown_event_types_are_ignored(fake_run_stream) -> None:
    fake_run_stream([
        {"type": "token", "text": "正在思考"},
        *_typical_events(),
    ])

    outcome = await run_plan("x")
    assert outcome.success is True


@pytest.mark.asyncio
async def test_pipeline_without_a_result_reports_failure(fake_run_stream) -> None:
    """引擎异常中断时不能返回一个看起来成功的空结果。"""
    fake_run_stream([{"type": "stage", "node": "intent", "label": "理解需求"}])

    outcome = await run_plan("x")
    assert outcome.success is False
    assert outcome.missing_fields
