"""行程接口。"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from lushu.domain.planned import PlannedTrip, StaySpec
from lushu.engine import lookup_city
from lushu.services import trip_service
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
