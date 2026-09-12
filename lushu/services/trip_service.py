"""行程用例的编排。

把「用户想要一个行程」翻译成领域对象与落库操作。需要外部信息时（城市行政区划）
走 `lushu.engine`，本模块不直接触碰 vendored 代码。

两条路径：

- **手工骨架**：用户给出城市与天数，铺成空的逐日行程，之后自己往里填。它是 M2
  城市停留增删改的基础，也保证了「引擎不可用时仍能建行程」。
- **规划生成**：跑引擎的流水线，把产出经 `plan_converter` 转换成自有模型后落库。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date

from lushu.domain.planned import PlannedTrip, StaySpec, lay_out
from lushu.engine import PlanStage, resolve_city, run_plan
from lushu.services.plan_converter import plan_to_trip
from lushu.services.trip_store import (
    StoredTrip,
    delete_trip,
    list_trips,
    load_trip,
    replace_plan,
    save_planned_trip,
    set_status,
)

__all__ = [
    "CityNotFoundError",
    "MissingInputError",
    "PlanRequest",
    "PlannedTripResult",
    "confirm_trip",
    "create_skeleton",
    "delete_trip",
    "get_trip",
    "list_trips",
    "plan_and_save",
    "replan_trip",
    "replace_plan",
    "set_status",
]


class CityNotFoundError(RuntimeError):
    """用户给的城市名在高德查不到，或只查到省市一级的结果。

    这是**必须让用户看见**的错误：城市标识决定天气与预算的城市归属，
    编一个代码进去只会让后面全部错位（ADR-0002）。
    """

    def __init__(self, city_names: Sequence[str]) -> None:
        self.city_names = tuple(city_names)
        super().__init__("以下城市无法识别：" + "、".join(self.city_names))


class MissingInputError(RuntimeError):
    """引擎认为需求还缺信息，需要向用户追问。这是对话式流程的正常分支。"""

    def __init__(self, missing_fields: Sequence[str]) -> None:
        self.missing_fields = tuple(missing_fields)
        super().__init__("还需要补充：" + "、".join(self.missing_fields))


@dataclass(frozen=True)
class PlanRequest:
    """一次规划请求。"""

    query: str
    start_date: date | None = None
    days: int | None = None
    name: str | None = None


@dataclass
class PlannedTripResult:
    """一次规划生成的结果。"""

    trip_id: str
    plan: PlannedTrip
    stages: list[PlanStage] = field(default_factory=list)


# ─── 城市解析 ────────────────────────────────────────────────


async def resolve_specs(specs: Sequence[StaySpec]) -> tuple[StaySpec, ...]:
    """把城市停留规格里的行政区划代码补齐。

    用户只给城市名，代码必须以高德为准。查不到就明确失败，不猜。
    """
    resolved: list[StaySpec] = []
    missing: list[str] = []

    for spec in specs:
        if spec.city_adcode:
            resolved.append(spec)
            continue
        match = await asyncio.to_thread(resolve_city, spec.city_name)
        if match is None:
            missing.append(spec.city_name)
            continue
        resolved.append(
            StaySpec(
                city_name=match.name or spec.city_name,
                stay_days=spec.stay_days,
                city_adcode=match.adcode,
            )
        )

    if missing:
        raise CityNotFoundError(missing)
    return tuple(resolved)


# ─── 手工骨架 ────────────────────────────────────────────────


async def create_skeleton(
    *,
    start_date: date,
    specs: Sequence[StaySpec],
    name: str | None = None,
    query: str = "",
) -> str:
    """按城市与天数铺出一份空的逐日行程，返回行程标识。"""
    resolved = await resolve_specs(specs)
    stays = lay_out(start_date, resolved)
    trip_name = name or _default_name(resolved)
    return save_planned_trip(
        PlannedTrip(name=trip_name, start_date=start_date, stays=stays, query=query)
    )


def _default_name(specs: Sequence[StaySpec]) -> str:
    cities = "、".join(s.city_name for s in specs)
    total = sum(s.stay_days for s in specs)
    return f"{cities} {total} 天"


# ─── 规划生成 ────────────────────────────────────────────────


async def plan_and_save(
    request: PlanRequest,
    *,
    on_stage: Callable[[PlanStage], None] | None = None,
) -> PlannedTripResult:
    """跑规划流水线并把产出落库，返回新建的行程。

    引擎不可用（缺 Key、网络失败）时异常会原样抛出，由接口层翻译成可读提示。
    """
    outcome = await run_plan(
        request.query,
        on_stage=on_stage,
        start_date=request.start_date,
        days=request.days,
    )
    if not outcome.success or not outcome.plan:
        raise MissingInputError(outcome.missing_fields or ["出行需求"])

    draft = await _convert_outcome(outcome.plan, request)
    trip_id = save_planned_trip(draft)
    return PlannedTripResult(trip_id=trip_id, plan=draft, stages=list(outcome.stages))


async def replan_trip(
    trip_id: str,
    request: PlanRequest,
    *,
    on_stage: Callable[[PlanStage], None] | None = None,
) -> PlannedTripResult:
    """重新规划并**替换**原有内容，保留行程标识与创建时间。

    这是破坏性操作：用户手工调过的顺序会被覆盖，所以调用方必须先弹
    「旧 → 新」差异让用户确认（Q48）。
    """
    outcome = await run_plan(
        request.query,
        on_stage=on_stage,
        start_date=request.start_date,
        days=request.days,
    )
    if not outcome.success or not outcome.plan:
        raise MissingInputError(outcome.missing_fields or ["出行需求"])

    draft = await _convert_outcome(outcome.plan, request)
    replace_plan(trip_id, draft)
    return PlannedTripResult(trip_id=trip_id, plan=draft, stages=list(outcome.stages))


async def _convert_outcome(plan: dict, request: PlanRequest) -> PlannedTrip:
    destination = str(plan.get("destination") or "").strip()
    if not destination:
        raise CityNotFoundError(["（规划产出没有目的地）"])

    match = await asyncio.to_thread(resolve_city, destination)
    if match is None:
        raise CityNotFoundError([destination])

    return plan_to_trip(
        plan,
        start_date=request.start_date,
        city_adcodes={destination: match.adcode},
        trip_name=request.name,
    )


# ─── 查询与状态 ──────────────────────────────────────────────


def get_trip(trip_id: str) -> StoredTrip | None:
    return load_trip(trip_id)


def confirm_trip(trip_id: str) -> None:
    """确认行程。确认之后才允许导出路书。"""
    set_status(trip_id, "confirmed")
