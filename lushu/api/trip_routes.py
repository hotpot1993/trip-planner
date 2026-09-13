"""行程接口。"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from lushu.domain.knowledge import describe_confidence
from lushu.domain.planned import PlannedTrip, StaySpec
from lushu.engine import lookup_city
from lushu.services import transfer_service, trip_service, weather_service
from lushu.services.trip_store import StoredTrip, TripSummary

router = APIRouter(prefix="/api", tags=["行程"])


# ─── 请求体 ──────────────────────────────────────────────────


class CitySpecIn(BaseModel):
    """一座城市停留的输入。"""

    name: str = Field(description="城市名，如『南京』")
    days: int = Field(ge=1, le=30, description="停留天数")
    adcode: str | None = Field(default=None, description="行政区划代码，通常留空由服务端解析")


class CreateTripIn(BaseModel):
    """手工创建行程骨架。"""

    start_date: date
    cities: list[CitySpecIn] = Field(min_length=1)
    name: str | None = None
    query: str = ""


class PlanIn(BaseModel):
    """触发规划。"""

    query: str = Field(min_length=1, description="用户的出行需求原话")
    start_date: date | None = None
    days: int | None = Field(default=None, ge=1, le=60)
    name: str | None = None


class UpdateStaysIn(BaseModel):
    """改城市停留与天数。"""

    cities: list[CitySpecIn] = Field(min_length=1)
    start_date: date | None = Field(default=None, description="留空则沿用原来的出发日期")


# ─── 响应体 ──────────────────────────────────────────────────


class TripSummaryOut(BaseModel):
    id: str
    name: str
    start_date: date
    total_days: int
    status: str
    city_names: list[str]
    updated_at: str


class ItemOut(BaseModel):
    kind: str
    title: str
    poi_id: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    note: str | None = None
    # 坐标优先来自 `poi` 表（ADR-0002）；餐饮没有实体，取天项自己记下的
    # 那一份（迁移 10）——路书就是靠它算出「走多久到那家店」的
    lat_gcj02: float | None = None
    lng_gcj02: float | None = None
    address: str | None = None
    rating: float | None = None
    open_time: str | None = None
    photo: str | None = None


class DayOut(BaseModel):
    date: date
    seq_in_stay: int
    theme: str | None = None
    items: list[ItemOut]


class StayOut(BaseModel):
    city_name: str
    city_adcode: str | None
    seq: int
    stay_days: int
    days: list[DayOut]


class TransferOut(BaseModel):
    """城际转移。它落在某一天上，但本身不是那天的一个事项。"""

    id: str
    from_city_name: str
    to_city_name: str
    day_index: int
    day: date
    mode: str
    service_no: str | None = None
    from_station: str | None = None
    to_station: str | None = None
    dep_time: str | None = None
    arr_time: str | None = None
    duration_min: int | None = None
    price: float | None = None
    price_source: str
    # 估价必须让用户看得见，不能与实价长得一样
    is_reference_price: bool
    has_tickets: bool | None = None
    advice_reason: str | None = None
    note: str | None = None
    alternatives: list[dict] = Field(default_factory=list)


class BudgetItemOut(BaseModel):
    category: str
    label: str
    amount: float
    currency: str
    is_reference_price: bool
    source: str | None = None
    note: str | None = None


class BudgetPanelOut(BaseModel):
    """预算面板。城际交通单独成栏（设计的明确要求）。"""

    intercity: list[BudgetItemOut]
    others: list[BudgetItemOut]
    intercity_total: float
    other_total: float
    total: float
    has_reference_prices: bool


class DayWeatherOut(BaseModel):
    date: date
    text: str
    night_text: str | None = None
    temp_min: float | None = None
    temp_max: float | None = None
    temperature_text: str
    precipitation_probability: float | None = None
    is_bad_outdoor: bool


class CityWeatherOut(BaseModel):
    """一座城市的预报。多城市行程里天气按城市分开呈现。"""

    city_name: str
    # 两个来源都没取到预报时为 None——**没有数据就没有来源**，
    # 不能让界面显示一个没给数据的来源名（原先就是那样：空预报配「来源 高德」）
    source: str | None
    source_label: str | None
    days: list[DayWeatherOut]
    note: str | None = None


class TripWeatherOut(BaseModel):
    start_date: date
    end_date: date
    cities: list[CityWeatherOut]


class BookingChannelOut(BaseModel):
    """预约渠道。规则必须有渠道，否则用户知道要预约也无处可去。"""

    name: str
    kind: str  # web | miniapp | official_account | phone
    url: str | None = None


class BookingAlertOut(BaseModel):
    """预约清单上的一条。

    `headline` 是给用户看的一行结论（「3 天后放票」「放票日已过 2 天，立刻确认」），
    它由领域层算好，界面不再自己拼——拼文案的地方一多，
    同一件事在不同页面上就会有两种说法。
    """

    poi_id: str
    poi_name: str
    city_name: str | None
    visit_date: date
    release_date: date | None
    days_until_release: int | None
    urgency: str  # overdue | today | soon | later
    headline: str
    release_time: str | None
    channels: list[BookingChannelOut]
    requires_real_name: bool | None
    id_required_note: str | None
    # 复核时写下的「坑」。它原本只出现在复核界面，等于查到了却没告诉用户。
    note: str | None


class TripBookingOut(BaseModel):
    trip_id: str
    today: date
    alerts: list[BookingAlertOut]
    # 行程里有规则、但规则还是草案（未经复核）的景点名。
    # 「清单里没有」与「规则还没复核」在用户眼里是同一件事，除非我们说出来。
    pending_review: list[str]


class TripDetailOut(BaseModel):
    id: str
    name: str
    status: str
    start_date: date
    end_date: date
    total_days: int
    city_names: list[str]
    query: str
    created_at: str
    updated_at: str
    stays: list[StayOut]
    transfers: list[TransferOut] = Field(default_factory=list)
    budget: BudgetPanelOut


class CityOut(BaseModel):
    name: str
    adcode: str
    level: str
    province: str | None = None
    lat_gcj02: float | None = None
    lng_gcj02: float | None = None


# ─── 序列化 ──────────────────────────────────────────────────


def _summary_out(summary: TripSummary) -> TripSummaryOut:
    return TripSummaryOut(
        id=summary.id,
        name=summary.name,
        start_date=summary.start_date,
        total_days=summary.total_days,
        status=summary.status,
        city_names=list(summary.city_names),
        updated_at=summary.updated_at,
    )


def _detail_out(stored: StoredTrip) -> TripDetailOut:
    plan: PlannedTrip = stored.plan
    return TripDetailOut(
        id=stored.id,
        name=stored.name,
        status=stored.status,
        start_date=plan.start_date,
        end_date=plan.end_date,
        total_days=plan.total_days,
        city_names=list(plan.city_names),
        query=plan.query,
        created_at=stored.created_at,
        updated_at=stored.updated_at,
        stays=[
            StayOut(
                city_name=stay.city_name,
                city_adcode=stay.city_adcode,
                seq=stay.seq,
                stay_days=stay.stay_days,
                days=[
                    DayOut(
                        date=day.day,
                        seq_in_stay=day.seq_in_stay,
                        theme=day.theme,
                        items=[_item_out(item) for item in day.items],
                    )
                    for day in stay.days
                ],
            )
            for stay in plan.stays
        ],
        transfers=[
            _transfer_out(transfer) for transfer in trip_service.load_transfers(stored.id)
        ],
        budget=_budget_panel(trip_service.load_budget(stored.id)),
    )


def _transfer_out(transfer) -> TransferOut:
    return TransferOut(
        id=transfer.id,
        from_city_name=transfer.from_city_name,
        to_city_name=transfer.to_city_name,
        day_index=transfer.day_index,
        day=transfer.day,
        mode=transfer.mode,
        service_no=transfer.service_no,
        from_station=transfer.from_station,
        to_station=transfer.to_station,
        dep_time=transfer.dep_time,
        arr_time=transfer.arr_time,
        duration_min=transfer.duration_min,
        price=transfer.price,
        price_source=transfer.price_source,
        is_reference_price=transfer.is_reference_price,
        has_tickets=transfer.has_tickets,
        advice_reason=transfer.advice_reason,
        note=transfer.note,
        alternatives=list(transfer.alternatives),
    )


def _budget_panel(items) -> BudgetPanelOut:
    """把预算项拆成「城际交通」与「其余」两栏。

    城际交通独立成栏是设计的明确要求：它是多城市行程独有的开销，
    混在总账里看不出「这趟多花的钱其实都在路上」。
    """
    def to_out(item) -> BudgetItemOut:
        return BudgetItemOut(
            category=item.category,
            label=item.label,
            amount=item.amount,
            currency=item.currency,
            is_reference_price=item.is_reference_price,
            source=item.source,
            note=item.note,
        )

    intercity = [to_out(item) for item in items if item.category == "intercity"]
    others = [to_out(item) for item in items if item.category != "intercity"]
    intercity_total = sum(item.amount for item in intercity)
    other_total = sum(item.amount for item in others)

    return BudgetPanelOut(
        intercity=intercity,
        others=others,
        intercity_total=round(intercity_total, 2),
        other_total=round(other_total, 2),
        total=round(intercity_total + other_total, 2),
        has_reference_prices=any(item.is_reference_price for item in items),
    )


def _item_out(item) -> ItemOut:
    facts = item.facts
    return ItemOut(
        kind=item.kind.value,
        title=item.title,
        poi_id=item.poi_id,
        start_time=item.start_time,
        end_time=item.end_time,
        note=item.note,
        lat_gcj02=facts.lat_gcj02 if facts else None,
        lng_gcj02=facts.lng_gcj02 if facts else None,
        address=facts.address if facts else None,
        rating=facts.rating if facts else None,
        open_time=facts.open_time if facts else None,
        photo=facts.photo if facts else None,
    )


# ─── 接口 ────────────────────────────────────────────────────


@router.get("/trips", response_model=list[TripSummaryOut], summary="列出行程")
def list_trips() -> list[TripSummaryOut]:
    return [_summary_out(s) for s in trip_service.list_trips()]


@router.post(
    "/trips",
    response_model=TripDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="手工创建行程骨架",
)
async def create_trip(payload: CreateTripIn) -> TripDetailOut:
    """按城市与天数铺出一份空的逐日行程。

    它是「引擎不可用时仍能建行程」的保底路径，也是 M2 城市停留增删改的基础。
    城市名会用高德解析成行政区划代码，查不到直接报错，不猜。
    """
    specs = [
        StaySpec(city_name=c.name, stay_days=c.days, city_adcode=c.adcode)
        for c in payload.cities
    ]
    trip_id = await trip_service.create_skeleton(
        start_date=payload.start_date,
        specs=specs,
        name=payload.name,
        query=payload.query,
    )
    stored = trip_service.get_trip(trip_id)
    if stored is None:  # pragma: no cover - 刚写入就查不到属于严重故障
        raise HTTPException(status_code=500, detail="行程写入后立即读取失败")
    return _detail_out(stored)


@router.get("/trips/{trip_id}", response_model=TripDetailOut, summary="行程详情")
def get_trip(trip_id: str) -> TripDetailOut:
    stored = trip_service.get_trip(trip_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"行程不存在：{trip_id}")
    return _detail_out(stored)


@router.delete("/trips/{trip_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除行程")
def delete_trip(trip_id: str) -> None:
    if not trip_service.delete_trip(trip_id):
        raise HTTPException(status_code=404, detail=f"行程不存在：{trip_id}")


@router.post("/trips/{trip_id}/confirm", response_model=TripDetailOut, summary="确认行程")
def confirm_trip(trip_id: str) -> TripDetailOut:
    """确认之后行程才允许导出成路书。"""
    stored = trip_service.get_trip(trip_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"行程不存在：{trip_id}")

    trip_service.confirm_trip(trip_id)
    confirmed = trip_service.get_trip(trip_id)
    if confirmed is None:  # pragma: no cover
        raise HTTPException(status_code=500, detail="确认后立即读取失败")
    return _detail_out(confirmed)


@router.put("/trips/{trip_id}/stays", response_model=TripDetailOut, summary="改城市与天数")
async def update_stays(trip_id: str, payload: UpdateStaysIn) -> TripDetailOut:
    """重新设置城市停留与天数，总行程天数随之重算。

    这是「用户可动态添加城市并设置各城市停留天数，系统自动计算总行程天数」
    的落点。仍然存在的天（日期与城市都没变）会保留原有内容。
    """
    if trip_service.get_trip(trip_id) is None:
        raise HTTPException(status_code=404, detail=f"行程不存在：{trip_id}")

    specs = [
        StaySpec(city_name=c.name, stay_days=c.days, city_adcode=c.adcode)
        for c in payload.cities
    ]
    await trip_service.update_stays(trip_id, specs=specs, start_date=payload.start_date)

    stored = trip_service.get_trip(trip_id)
    if stored is None:  # pragma: no cover
        raise HTTPException(status_code=500, detail="更新后立即读取失败")
    return _detail_out(stored)


@router.post(
    "/trips/plan",
    response_model=TripDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="触发生成并落库",
)
async def plan_trip(payload: PlanIn) -> TripDetailOut:
    """跑规划流水线并把产出存成一份新行程。

    这是同步接口：流水线要跑一到几分钟，期间没有进度反馈。带进度的流式接口
    在 M1 的下一步加。缺 Key 或需求不全时会返回可读的错误。
    """
    result = await trip_service.plan_and_save(
        trip_service.PlanRequest(
            query=payload.query,
            start_date=payload.start_date,
            days=payload.days,
            name=payload.name,
        )
    )
    stored = trip_service.get_trip(result.trip_id)
    if stored is None:  # pragma: no cover
        raise HTTPException(status_code=500, detail="行程写入后立即读取失败")
    return _detail_out(stored)


@router.post(
    "/trips/{trip_id}/transfers/refresh",
    response_model=list[TransferOut],
    summary="刷新城际转移",
)
async def refresh_transfers(trip_id: str) -> list[TransferOut]:
    """重新查一遍两两城市之间的车次，并同步城际交通的预算栏。

    会真的问 12306。超出 14 天预售期的行程查不到车次是**正常**的——那时按
    距离给出建议并把原因写明，而不是当成没有铁路。
    """
    if trip_service.get_trip(trip_id) is None:
        raise HTTPException(status_code=404, detail=f"行程不存在：{trip_id}")

    transfers = await transfer_service.refresh_transfers(trip_id)
    return [_transfer_out(transfer) for transfer in transfers]


@router.get("/trips/{trip_id}/weather", response_model=TripWeatherOut, summary="多城市天气")
async def trip_weather(trip_id: str) -> TripWeatherOut:
    """按城市分别取预报。

    多城市行程里天气必须按城市分开——「北京下雨」和「西安下雨」对行程安排的
    含义完全不同。主力是 Open-Meteo（16 天），高德兜底（约 4 天）。
    """
    stored = trip_service.get_trip(trip_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"行程不存在：{trip_id}")

    adcodes = [stay.city_adcode for stay in stored.plan.stays if stay.city_adcode]
    cities = trip_service.load_cities(adcodes)

    forecasts = await weather_service.forecast_for_cities(
        [cities[code] for code in adcodes if code in cities],
        stored.plan.start_date,
        stored.plan.end_date,
    )

    return TripWeatherOut(
        start_date=stored.plan.start_date,
        end_date=stored.plan.end_date,
        cities=[
            CityWeatherOut(
                city_name=forecast.city_name,
                # 两个来源都没取到时是 None：界面据此不显示来源，而不是
                # 显示一个没给数据的来源名
                source=forecast.source.value if forecast.source else None,
                source_label=forecast.source.label if forecast.source else None,
                days=[
                    DayWeatherOut(
                        date=day.day,
                        text=day.text,
                        night_text=day.night_text,
                        temp_min=day.temp_min,
                        temp_max=day.temp_max,
                        temperature_text=day.temperature_text,
                        precipitation_probability=day.precipitation_probability,
                        is_bad_outdoor=day.is_bad_outdoor,
                    )
                    for day in forecast.days
                ],
                note=forecast.note,
            )
            for forecast in forecasts
        ],
    )


class TripInsightsOut(BaseModel):
    """行程上挂着的软经验，按 POI 分组。

    **与行程详情分开一个接口**：软经验是知识库的内容，行程是自有数据，
    两者的读取时机不同（知识库会随复核与重新对齐变化）。
    混进详情响应里，会让一次行程读取依赖整个知识库。
    """

    trip_id: str
    by_poi: dict[str, ItemInsightsOut]
    covered_items: int
    total_claims: int


class InsightOut(BaseModel):
    claim_id: str
    text: str
    facet: str
    confidence: str
    independent_source_count: int
    # 「N 个独立来源」那句话由服务端出（domain.knowledge.describe_confidence）。
    # 让前端自己拼的话，同一句话会散在行程页、城市页、工作台各一份，
    # 而它们迟早会不一致——原先就是那样，其中一份还把 2 个来源说成「只有 1 个」。
    confidence_text: str
    evidence_count: int
    verify_due_at: str | None
    single_source: bool


class ItemInsightsOut(BaseModel):
    poi_id: str
    poi_name: str
    highlights: list[InsightOut]
    avoids: list[InsightOut]


class ItemCoverageOut(BaseModel):
    """一个排进行程的地方，以及有没有人推荐过它。"""

    poi_id: str
    title: str
    city_adcode: str | None
    city_name: str | None
    recommended: bool
    claim_count: int
    booking_required: bool | None


class TripCoverageOut(BaseModel):
    """这份行程里，有多少地方是网友真的推荐过的。

    设计 5.1 的封闭世界约束是「排程不得引入候选池之外的景点」。候选池**还没有
    接进排程的景点搜索**（那要改 vendored 的节点），所以现在硬性拒绝整份行程
    会把每一份都毙掉。有用的是如实报出来。
    """

    trip_id: str
    total: int
    recommended: int
    ratio: float | None
    unresolved: int
    items: list[ItemCoverageOut]


@router.get("/trips/{trip_id}/roadbook.html", summary="路书（单文件 HTML）")
def trip_roadbook(trip_id: str) -> Response:
    """把这份行程导出成单个 HTML 文件：手机优先、离线可读。

    设计第八节说「生成后必须跑一次契约校验，有错误必须修复后重跑」。
    这里**有错误就返回 422**，不产出一份到了当地打不开的路书——
    那比没有更糟，人会以为带上了。校验的提醒项不影响下载。
    """
    from urllib.parse import quote

    from lushu.domain.roadbook import errors, validate
    from lushu.services import roadbook_render as render
    from lushu.services import roadbook_service

    stored = trip_service.get_trip(trip_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"行程不存在：{trip_id}")

    book, _warnings = roadbook_service.assemble(trip_id)
    problems = validate(book)
    fatal = errors(problems)
    if fatal:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="路书没通过契约校验：" + "；".join(str(item) for item in fatal),
        )

    html = render.render(book)
    if render.external_resources(html):
        # 兜底：渲染器自己保证零外部加载，这里再确认一次。
        # 真出现了说明渲染器有 bug，宁可报错也不要发一份离线打不开的东西。
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="路书里有会自动加载的外部资源，不满足离线可读",
        )

    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"roadbook.html\"; filename*=UTF-8''{quote(book.name)}.html"
            ),
            "Cache-Control": "no-store",
        },
    )


@router.get("/trips/{trip_id}/coverage", response_model=TripCoverageOut, summary="行程的知识覆盖")
def trip_coverage(trip_id: str) -> TripCoverageOut:
    """行程里的景点，哪些有人写过、哪些只是地图上恰好有。"""
    from lushu.services import coverage as coverage_service

    stored = trip_service.get_trip(trip_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"行程不存在：{trip_id}")

    data = coverage_service.coverage_for_trip(trip_id)
    return TripCoverageOut(
        trip_id=trip_id,
        total=data.total,
        recommended=data.recommended,
        ratio=data.ratio,
        unresolved=data.unresolved,
        items=[
            ItemCoverageOut(
                poi_id=item.poi_id,
                title=item.title,
                city_adcode=item.city_adcode,
                city_name=item.city_name,
                recommended=item.recommended,
                claim_count=item.claim_count,
                booking_required=item.booking_required,
            )
            for item in data.items
        ],
    )


@router.get("/trips/{trip_id}/insights", response_model=TripInsightsOut, summary="行程上的软经验")
def trip_insights(trip_id: str) -> TripInsightsOut:
    """把知识库里挂在这份行程各个景点上的结论取出来。

    设计 5.4：**软经验不注入 prompt，生成后挂载**。模型没见过这些文字，
    也就编不出「我在故宫拍到了没人的太和殿」。
    """
    from lushu.services import trip_insights as insights_service

    stored = trip_service.get_trip(trip_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"行程不存在：{trip_id}")

    data = insights_service.insights_for_trip(trip_id)

    def one(item) -> InsightOut:
        return InsightOut(
            claim_id=item.claim_id,
            text=item.text,
            facet=item.facet,
            confidence=item.confidence,
            independent_source_count=item.independent_source_count,
            confidence_text=describe_confidence(item.independent_source_count),
            evidence_count=item.evidence_count,
            verify_due_at=item.verify_due_at,
            single_source=item.single_source,
        )

    return TripInsightsOut(
        trip_id=trip_id,
        by_poi={
            poi_id: ItemInsightsOut(
                poi_id=item.poi_id,
                poi_name=item.poi_name,
                highlights=[one(claim) for claim in item.highlights],
                avoids=[one(claim) for claim in item.avoids],
            )
            for poi_id, item in data.by_poi.items()
        },
        covered_items=data.covered_items,
        total_claims=data.total_claims,
    )


@router.get("/trips/{trip_id}/booking", response_model=TripBookingOut, summary="预约清单")
def trip_booking(
    trip_id: str, today: Annotated[date | None, Query()] = None
) -> TripBookingOut:
    """这份行程的预约清单，按距今剩余天数倒排。

    真正有用的不是「这个景点要预约」，而是「还有 3 天放票，现在就得盯着」（Q11）。

    `pending_review` 是**必须与清单一同返回**的东西：草案状态的规则被静默跳过
    （Q10 的硬门禁），少提醒是对的，但不能让用户以为那个景点不用预约——
    「清单里没有」与「规则还没复核」在用户眼里是同一件事，除非我们说出来。
    """
    from lushu.services import booking_store

    stored = trip_service.get_trip(trip_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"行程不存在：{trip_id}")

    alerts = booking_store.alerts_for_trip(trip_id, today=today)
    pending = booking_store.pending_rule_pois(trip_id)

    return TripBookingOut(
        trip_id=trip_id,
        today=(today or date.today()).isoformat(),
        alerts=[
            BookingAlertOut(
                poi_id=alert.poi_id,
                poi_name=alert.poi_name,
                city_name=alert.city_name,
                visit_date=alert.visit_date.isoformat(),
                release_date=alert.release_date.isoformat() if alert.release_date else None,
                days_until_release=alert.days_until_release,
                urgency=alert.urgency.value,
                headline=alert.headline,
                release_time=alert.release_time,
                channels=[
                    BookingChannelOut(name=item.name, kind=item.kind.value, url=item.url)
                    for item in alert.channels
                ],
                requires_real_name=alert.requires_real_name,
                id_required_note=alert.id_required_note,
                note=alert.note,
            )
            for alert in alerts
        ],
        pending_review=pending,
    )


@router.get("/cities/resolve", response_model=list[CityOut], summary="解析城市")
def resolve_cities(
    name: str = Query(min_length=1, description="城市名或关键词"),
) -> list[CityOut]:
    """把城市名解析成行政区划候选。

    返回列表而不是单个结果：省市同名时选择权应该交给用户，而不是让服务端
    替他挑一个（挑错了整份行程的天数分配都会错位）。
    """
    return [
        CityOut(
            name=m.name,
            adcode=m.adcode,
            level=m.level,
            province=m.province,
            lat_gcj02=m.lat_gcj02,
            lng_gcj02=m.lng_gcj02,
        )
        for m in lookup_city(name)
    ]


# ─── 待补坐标的餐饮项 ───────────────────────────────────────────
#
# 自动判据（`services/meal_coords`）只写分得清的那些。分不清的怎么办？
# **让人来认**——机器分不清「绿柳居」的 24 家分店，人一眼就知道是夫子庙那家。
# 这一组接口是那个人工入口：列出待办、摊开候选、按人选定的写进去。


class MealCandidateOut(BaseModel):
    """一个可以指定的候选店。"""

    poi_id: str
    name: str
    address: str | None
    lat_gcj02: float
    lng_gcj02: float
    # 离**当天最近那处景点**的距离。算不出就是 None——
    # 「算不出」不能写成 0，0 米看起来像就在旁边。
    distance_m: int | None
    nearest_anchor: str | None
    name_score: float


class PendingMealOut(BaseModel):
    """一处没有坐标的餐饮项。"""

    item_id: str
    day_date: str
    title: str
    city_name: str
    anchors: list[str]


class PendingMealsOut(BaseModel):
    trip_id: str
    items: list[PendingMealOut]


class MealCandidatesOut(BaseModel):
    item_id: str
    title: str
    city_name: str
    anchors: list[str]
    candidates: list[MealCandidateOut]
    # 筛出多少个（可能多于列出来的）。「还有更远的没显示」要说得出口。
    total: int
    note: str


class PinMealIn(BaseModel):
    """人选定的那一家的坐标。

    传坐标而不是传 `poi_id`：候选来自高德搜索，不一定在我们的 `poi` 表里，
    为它写一行实体是错的（餐厅不该进实体表，ADR-0002 说的是景点）。
    """

    lat_gcj02: float
    lng_gcj02: float
    address: str | None = None


def _pending_out(trip_id: str) -> PendingMealsOut:
    from lushu.services import meal_coords

    return PendingMealsOut(
        trip_id=trip_id,
        items=[
            PendingMealOut(
                item_id=slot.item_id,
                day_date=slot.day_date,
                title=slot.title,
                city_name=slot.city_name,
                anchors=[anchor.title for anchor in slot.anchors],
            )
            for slot in meal_coords.pending_by_trip(trip_id)
        ],
    )


def _require_trip(trip_id: str) -> None:
    if trip_service.get_trip(trip_id) is None:
        raise HTTPException(status_code=404, detail=f"行程不存在：{trip_id}")


@router.get(
    "/trips/{trip_id}/pending-meals",
    response_model=PendingMealsOut,
    summary="没有坐标的餐饮项",
)
def trip_pending_meals(trip_id: str) -> PendingMealsOut:
    """列出待补坐标的餐饮项。

    **只查库，不联网。** 候选要打高德（每项一处锚点一次请求），放在这里会让
    每次打开行程页都慢十几秒、还把接口额度烧在一眼不看的东西上。所以要单独
    一个接口、点开某一项时才取。
    """
    _require_trip(trip_id)
    return _pending_out(trip_id)


@router.get(
    "/trips/{trip_id}/pending-meals/{item_id}/candidates",
    response_model=MealCandidatesOut,
    summary="这一项在高德上有哪些可能",
)
def trip_meal_candidates(trip_id: str, item_id: str) -> MealCandidatesOut:
    """把候选摊开来给人认，**按离当天景点的距离排**。

    排序键是距离不是名字分：「绿柳居」的 24 家全都一样像，人能认出是哪一家
    靠的是「就在老门东里面」。
    """
    from lushu.services import meal_coords

    _require_trip(trip_id)
    slot = meal_coords.pending_item(trip_id, item_id)
    if slot is None:
        raise HTTPException(status_code=404, detail="这一项不是这份行程里待补坐标的餐饮项")

    listed = meal_coords.meal_candidates(slot)
    return MealCandidatesOut(
        item_id=slot.item_id,
        title=slot.title,
        city_name=slot.city_name,
        anchors=[anchor.title for anchor in slot.anchors],
        candidates=[
            MealCandidateOut(
                poi_id=item.poi_id,
                name=item.name,
                address=item.address,
                lat_gcj02=item.lat_gcj02,
                lng_gcj02=item.lng_gcj02,
                distance_m=item.distance_m,
                nearest_anchor=item.nearest_anchor,
                name_score=item.name_score,
            )
            for item in listed.candidates
        ],
        total=listed.total,
        note=listed.note,
    )


@router.post(
    "/trips/{trip_id}/pending-meals/{item_id}",
    response_model=PendingMealsOut,
    summary="指定一处餐饮的位置",
)
def pin_meal_coords(trip_id: str, item_id: str, payload: PinMealIn) -> PendingMealsOut:
    """把人选定的坐标写进天项，返回**刷新后的待办**。

    返回整份待办而不是 204：界面上少一次往返，也不会出现「写进去了但清单
    还显示着它」的半截状态。
    """
    from lushu.services import meal_coords

    _require_trip(trip_id)
    if meal_coords.pending_item(trip_id, item_id) is None:
        raise HTTPException(status_code=404, detail="这一项不是这份行程里待补坐标的餐饮项")

    written = meal_coords.pin_meal(
        item_id,
        lat_gcj02=payload.lat_gcj02,
        lng_gcj02=payload.lng_gcj02,
        address=payload.address,
    )
    if not written:
        # 有人（或自动补齐）在这中间填上了。不是错误，但要如实说。
        raise HTTPException(status_code=409, detail="这一项刚刚已经有坐标了，刷新看看")

    return _pending_out(trip_id)
