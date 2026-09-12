"""规划流水线的薄包装。

引擎的 `planning/graph.py` 提供了 `run_stream()`：它逐节点产出进度事件，
最后产出一个结果事件，里面带着最终计划或缺失的必填项。

**它不写任何数据库。** 这一条很重要——ADR-0007 规定行程真相在本项目的表里，
而这个入口天然满足：引擎只负责生成，落库由 `lushu.services.trip_store` 负责。
引擎自带的 `RunManager`、`PlanningFinalizer`、`itineraries` 表在本项目里全部不用。

需要配置 `AMAP_API_KEY`（景点与餐饮搜索）与 LLM 的 Key，否则会抛出可读的错误。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class PlanStage:
    """流水线的一个阶段事件，用于向前端推进度。"""

    node: str
    label: str


@dataclass
class PlanOutcome:
    """一次规划的结果。"""

    success: bool
    plan: dict[str, Any] | None = None
    missing_fields: list[str] = field(default_factory=list)
    stages: list[PlanStage] = field(default_factory=list)
    history: list[str] = field(default_factory=list)


async def stream_plan(
    query: str,
    *,
    start_date: date | None = None,
    days: int | None = None,
    destination: str | None = None,
    max_review_rounds: int | None = None,
) -> AsyncIterator[PlanStage | PlanOutcome]:
    """运行规划流水线，逐个产出阶段事件，最后产出一个 PlanOutcome。

    只传引擎认识的覆盖项，且只在有值时才传——`TravelPlanState` 对未提供的
    字段用默认值，显式传 None 会覆盖掉引擎自己的推导。
    """
    # 延迟导入：必须先加载 config，再触碰引擎
    from third_party.floattrip.planning.graph import run_stream

    overrides: dict[str, Any] = {}
    if start_date is not None:
        overrides["travel_start_date"] = start_date
    if days is not None:
        overrides["days"] = days
    if destination:
        overrides["destination"] = destination
    if max_review_rounds is not None:
        overrides["max_review_rounds"] = max_review_rounds

    stages: list[PlanStage] = []
    async for event in run_stream(query, **overrides):
        if event.get("type") == "stage":
            stage = PlanStage(
                node=str(event.get("node") or ""),
                label=str(event.get("label") or event.get("node") or ""),
            )
            stages.append(stage)
            yield stage
        elif event.get("type") == "result":
            yield PlanOutcome(
                success=bool(event.get("success")),
                plan=event.get("plan"),
                missing_fields=[str(x) for x in (event.get("missing_fields") or [])],
                stages=stages,
                history=[str(x) for x in (event.get("history") or [])],
            )


async def run_plan(
    query: str,
    *,
    on_stage: Callable[[PlanStage], None] | None = None,
    **overrides: Any,
) -> PlanOutcome:
    """跑完流水线并返回结果。

    `on_stage` 用于把进度推给调用方（例如 SSE 推送或日志），
    不改变流水线本身的行为。
    """
    outcome: PlanOutcome | None = None
    async for event in stream_plan(query, **overrides):
        if isinstance(event, PlanStage):
            if on_stage is not None:
                on_stage(event)
        else:
            outcome = event

    if outcome is None:
        # 流水线异常中断时不能返回一个看起来成功的空结果
        return PlanOutcome(success=False, missing_fields=["流水线没有产出结果"])
    return outcome
