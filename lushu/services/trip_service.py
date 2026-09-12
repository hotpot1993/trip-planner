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

from lushu.domain.planned import PlannedDay, PlannedStay, PlannedTrip, StaySpec, lay_out
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
    "resolve_specs",
    "set_status",
    "update_stays",
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


# ─── 改城市与天数 ────────────────────────────────────────────


async def update_stays(
    trip_id: str,
    *,
    specs: Sequence[StaySpec],
    start_date: date | None = None,
) -> PlannedTrip:
    """改城市停留与天数，重新铺排日期，保留仍然存在的天项。

    「北京 3 天 + 西安 3 天」改成「北京 2 天 + 西安 3 天」之后，总天数由 6 变 5，
    日期整体前移。保留规则是**按「日期 + 城市」配对**：

    - 某一天新布局后仍然存在（日期没变、城市也没变），它原有的事项原样保留
    - 被删掉的天连同它的事项一起消失
    - 改了城市名等于换了一座城市，那几天的事项不保留

    这个规则不完美——把北京从 3 天改成 2 天，第 3 天的事项会消失而不是顺延。
    顺延需要判断哪些事项值得保留，那是一件需要用户参与的事，留到 M2 做差异确认。
    """
    current = load_trip(trip_id)
    if current is None:
        raise LookupError(f"行程不存在：{trip_id}")

    resolved = await resolve_specs(specs)
    anchor = start_date or current.plan.start_date
    laid_out = lay_out(anchor, resolved)
    stays = _preserve_items(current.plan, laid_out)

    name = current.name
    if current.name == _default_name_from_stays(current.plan):
        # 名字是自动生成的，城市或天数变了就跟着更新，否则会名不副实
        name = _default_name(resolved)

    draft = PlannedTrip(
        name=name,
        start_date=anchor,
        stays=stays,
        query=current.plan.query,
    )
    replace_plan(trip_id, draft)
    return draft


def _default_name_from_stays(plan: PlannedTrip) -> str:
    cities = "、".join(s.city_name for s in plan.stays)
    return f"{cities} {plan.total_days} 天"


def _preserve_items(
    current: PlannedTrip, laid_out: tuple[PlannedStay, ...]
) -> tuple[PlannedStay, ...]:
    """把旧布局里仍然对得上的内容搬到新布局上。

    主题与天项都要搬。主题是引擎按当天景点归纳出来的一句话（如「钟山风景区」），
    它和天项一样是内容，只搬天项会让改完天数的那一天变成「有景点但没主题」。
    """
    kept: dict[tuple[date, str], PlannedDay] = {
        (day.day, stay.city_name): day
        for stay in current.stays
        for day in stay.days
        if day.items or day.theme
    }

    preserved: list[PlannedStay] = []
    for stay in laid_out:
        days = tuple(
            _merge_day(day, kept.get((day.day, stay.city_name))) for day in stay.days
        )
        preserved.append(
            PlannedStay(
                city_name=stay.city_name,
                city_adcode=stay.city_adcode,
                seq=stay.seq,
                days=days,
            )
        )
    return tuple(preserved)


def _merge_day(layout_day: PlannedDay, previous: PlannedDay | None) -> PlannedDay:
    """把旧内容并进新铺出来的一天。旧的一天不存在时原样返回空的那天。"""
    if previous is None:
        return layout_day
    return PlannedDay(
        day=layout_day.day,
        seq_in_stay=layout_day.seq_in_stay,
        theme=previous.theme,
        items=previous.items,
    )


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
