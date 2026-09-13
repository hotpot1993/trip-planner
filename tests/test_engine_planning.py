"""规划流水线包装的测试。

用假的 run_stream 驱动，不需要 API Key 也不需要网络。验证的是：
包装层忠实地把阶段事件与结果事件分流，且只把有值的覆盖项传给引擎
（显式传 None 会覆盖掉引擎自己的推导）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from lushu.engine.planning import PlanOutcome, PlanStage, compose_query, run_plan, stream_plan

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


# ─── 候选池接进景点搜索 ──────────────────────────────────────


class TestSpotSourceWiring:
    """`spot_source` 真的接到了引擎上吗。

    这条链有三跳（`trip_service` → `run_plan` → `stream_plan` → 替换引擎的
    搜索函数），任何一跳上参数被改名或漏掉，**行为都会静默退回纯高德搜索**——
    没有报错，只是网友推荐过的地方不再优先。所以每一跳都要有一条会红的测试。
    """

    @pytest.fixture(autouse=True)
    def _restore_search(self):
        yield
        from lushu.engine import pool_search

        pool_search.install(None)

    async def test_a_provider_is_installed_before_the_engine_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lushu.engine import pool_search
        from third_party.floattrip.planning import graph as engine_graph
        from third_party.floattrip.planning import nodes

        seen: list[object] = []

        async def fake(query: str, **overrides: Any):
            # 引擎开始跑的那一刻，搜索函数应当已经被换掉
            seen.append(nodes.fetch_city_spots_async)
            yield {"type": "result", "success": True, "plan": FAKE_PLAN}

        monkeypatch.setattr(engine_graph, "run_stream", fake)

        async for _ in stream_plan("去南京", spot_source=lambda _city: []):
            pass

        assert seen, "引擎没被跑到"
        assert seen[0] is not pool_search._state["original"], (
            "给了候选池，引擎却还是原来那个纯高德搜索——参数在哪一跳被漏掉了"
        )

    async def test_without_a_provider_the_engine_keeps_its_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lushu.engine import pool_search
        from third_party.floattrip.planning import graph as engine_graph
        from third_party.floattrip.planning import nodes

        # 先换成带池子的，再规划一次**不带**的：不能残留上一次的状态
        pool_search.install(lambda _city: [])

        seen: list[object] = []

        async def fake(query: str, **overrides: Any):
            seen.append(nodes.fetch_city_spots_async)
            yield {"type": "result", "success": True, "plan": FAKE_PLAN}

        monkeypatch.setattr(engine_graph, "run_stream", fake)

        async for _ in stream_plan("去南京"):
            pass

        assert seen[0] is pool_search._state["original"], "没给池子却换了搜索函数"

    async def test_the_planning_entry_passes_the_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """第一跳：规划入口必须把候选池交出去。

        跑到「引擎返回失败」就停——这一跳要钉的是**有没有传**，
        不是真去排队（那要 API Key 与网络）。
        """
        from lushu.services import candidate_pool, trip_service

        captured: dict[str, Any] = {}

        async def fake_run_plan(query: str, **overrides: Any) -> PlanOutcome:
            captured.update(overrides)
            return PlanOutcome(success=False, missing_fields=["出行需求"])

        monkeypatch.setattr(trip_service, "run_plan", fake_run_plan)
        monkeypatch.setattr(trip_service, "_require_engine_config", lambda: None)

        with pytest.raises(trip_service.MissingInputError):
            await trip_service.plan_and_save(trip_service.PlanRequest(query="去南京"))

        assert captured["spot_source"] is candidate_pool.pool_pois


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


# ─── 把结构化条件折进 query ──────────────────────────────────
#
# 引擎的意图节点只从 query 文本里抽取日期与目的地，初始状态里的那几个字段
# 会被它无条件覆盖。所以这一步不是可选的美化，而是唯一的传递通道。


def test_compose_leaves_a_bare_query_alone() -> None:
    assert compose_query("  想去南京  ") == "想去南京"


def test_compose_appends_the_start_date() -> None:
    composed = compose_query("去南京", start_date=date(2026, 10, 1))
    assert composed.startswith("去南京")
    assert "2026-10-01 出发" in composed


def test_compose_appends_the_days() -> None:
    assert "共 3 天" in compose_query("去南京", days=3)


def test_compose_appends_the_destination() -> None:
    assert "目的地是南京" in compose_query("出去玩", destination="南京")


def test_compose_combines_everything() -> None:
    composed = compose_query("去南京", start_date=date(2026, 10, 1), days=3, destination="南京")

    assert composed.startswith("去南京（")
    assert composed.endswith("）")
    assert "目的地是南京" in composed
    assert "2026-10-01 出发" in composed
    assert "共 3 天" in composed


def test_compose_joins_conditions_in_a_stable_order() -> None:
    composed = compose_query("x", start_date=date(2026, 10, 1), days=3, destination="南京")
    assert composed.index("目的地") < composed.index("出发") < composed.index("共")


@pytest.mark.asyncio
async def test_conditions_are_folded_into_the_query_sent_to_the_engine(fake_run_stream) -> None:
    """这是真正起作用的那条通道，必须钉死。"""
    captured = fake_run_stream(_typical_events())
    await run_plan("去南京", start_date=date(2026, 10, 1), days=3)

    assert "2026-10-01 出发" in captured["query"]
    assert "共 3 天" in captured["query"]


@pytest.mark.asyncio
async def test_empty_conditions_do_not_touch_the_query(fake_run_stream) -> None:
    captured = fake_run_stream(_typical_events())
    await run_plan("去南京")
    assert captured["query"] == "去南京"
