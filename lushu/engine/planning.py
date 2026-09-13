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


def compose_query(
    query: str,
    *,
    start_date: date | None = None,
    days: int | None = None,
    destination: str | None = None,
) -> str:
    """把结构化的出行条件折进需求原话。

    **这一步不能省。** 引擎的 `intent` 节点只从 query 文本里抽取目的地与日期
    （`effective_query = state.query`），然后把初始状态里的 `travel_start_date`、
    `travel_end_date`、`days`、`destination` **无条件覆盖掉**。所以「用户在界面上
    填了出发日期」这件事，必须变成一句人话交给它，否则填了等于没填——而且不会
    报错，只会反过来说「还需要补充出行日期」。
    """
    text = query.strip()
    conditions: list[str] = []
    if destination:
        conditions.append(f"目的地是{destination}")
    if start_date is not None:
        conditions.append(f"{start_date.isoformat()} 出发")
    if days is not None:
        conditions.append(f"共 {days} 天")
    if not conditions:
        return text
    return f"{text}（{'，'.join(conditions)}）"


async def stream_plan(
    query: str,
    *,
    start_date: date | None = None,
    days: int | None = None,
    destination: str | None = None,
    max_review_rounds: int | None = None,
    spot_source=None,
    pool_only_from: int | None = None,
) -> AsyncIterator[PlanStage | PlanOutcome]:
    """运行规划流水线，逐个产出阶段事件，最后产出一个 PlanOutcome。

    结构化的出行条件会折进 query（见 `compose_query`），同时也作为初始状态传入；
    真正起作用的是前者。

    `spot_source` 是候选池的提供者（`engine.pool_search.SpotProvider`）：
    给了它，景点搜索就是「候选池优先、高德补全」；不给就是纯高德搜索。
    `pool_only_from` 是封闭世界的门槛（池子够这么多就只用池子），
    由调用方从候选池那边传进来——那个数说的是「这座城市算不算有攻略数据」。
    """
    # 每次显式设定，不在两次规划之间残留状态
    from lushu.engine import pool_search

    # 延迟导入：必须先加载 config，再触碰引擎
    from third_party.floattrip.planning.graph import run_stream

    pool_search.install(
        spot_source,
        pool_only_from=pool_only_from if pool_only_from is not None else 1,
    )

    overrides: dict[str, Any] = {}
    if start_date is not None:
        overrides["travel_start_date"] = start_date
    if days is not None:
        overrides["days"] = days
    if destination:
        overrides["destination"] = destination
    if max_review_rounds is not None:
        overrides["max_review_rounds"] = max_review_rounds

    effective_query = compose_query(
        query, start_date=start_date, days=days, destination=destination
    )

    stages: list[PlanStage] = []
    async for event in run_stream(effective_query, **overrides):
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
    start_date: date | None = None,
    days: int | None = None,
    destination: str | None = None,
    **overrides: Any,
) -> PlanOutcome:
    """跑完流水线并返回结果。

    `on_stage` 用于把进度推给调用方（例如 SSE 推送或日志），
    不改变流水线本身的行为。
    """
    outcome: PlanOutcome | None = None
    async for event in stream_plan(
        query, start_date=start_date, days=days, destination=destination, **overrides
    ):
        if isinstance(event, PlanStage):
            if on_stage is not None:
                on_stage(event)
        else:
            outcome = event

    if outcome is None:
        # 流水线异常中断时不能返回一个看起来成功的空结果
        return PlanOutcome(success=False, missing_fields=["流水线没有产出结果"])
    return outcome
