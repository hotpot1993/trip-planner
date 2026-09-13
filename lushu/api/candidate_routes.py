"""候选池接口。

M5 的验收条件是「打开城市看到的是网友推荐而非一片 POI」。这个接口就是那句话
的落点：给定城市，先摆出网友真的写过的地方——带打卡与避坑的原文结论、
置信度、独立来源数——再用高德补齐坐标与开放时间。

两条要在响应里说清的：

- **来源**（`source`）：`knowledge` 是网友推荐过，`amap` 只是地图上有。
  把一片高德 POI 说成「推荐」是这套东西最不该犯的错。
- **覆盖度**（`covered`）：知识库还很薄时要说实话，不能让空池子冒充
  「这座城市没什么可去的」。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from lushu.services import candidate_pool

router = APIRouter(prefix="/api/cities", tags=["候选池"])


class ClaimBriefOut(BaseModel):
    """一条挂在这个地方上的结论。只带界面要显示的那几样。"""

    claim_id: str
    text: str
    facet: str
    confidence: str
    independent_source_count: int
    evidence_count: int
    verify_due_at: str | None


class CandidateOut(BaseModel):
    poi_id: str
    name: str
    source: str  # knowledge | amap
    address: str | None
    lat_gcj02: float | None
    lng_gcj02: float | None
    rating: float | None
    open_time: str | None
    highlights: list[ClaimBriefOut]
    avoids: list[ClaimBriefOut]
    claim_count: int
    high_confidence_count: int
    recommended: bool
    booking_days: int | None
    booking_time: str | None


class CityPoolOut(BaseModel):
    city_adcode: str
    candidates: list[CandidateOut]
    recommended_count: int
    covered: bool
    amap_error: str | None


def _claim_brief(claim) -> ClaimBriefOut:
    return ClaimBriefOut(
        claim_id=claim.claim_id,
        text=claim.text,
        facet=claim.facet,
        confidence=claim.confidence.value,
        independent_source_count=claim.independent_source_count,
        evidence_count=claim.evidence_count,
        verify_due_at=claim.verify_due_at,
    )


@router.get("/{adcode}/candidates", response_model=CityPoolOut, summary="候选池")
def city_candidates(
    adcode: str,
    name: str | None = Query(default=None, description="城市名，回落搜高德时要用"),
    fill: bool = Query(default=False, description="是否用高德补齐缺失的开放时间与评分"),
) -> CityPoolOut:
    """一座城市的候选池。

    `fill=true` 才会去问高德（每个缺字段的候选一次请求，慢且要配额），
    默认只读本地库——界面首屏不该等一串网络请求。

    **适配器在服务层里接**：接口层不许 import `lushu.adapters`，
    架构边界测试会拦（实测拦过一次）。
    """
    pool = candidate_pool.pool_for_city(
        adcode,
        city_name=name,
        fill=fill,
    )
    if not pool.candidates and pool.amap_error is None and not name:
        # 库里没有，而且调用方也没给城市名，回落不了。说清缺什么，
        # 而不是返回一个空的候选池让人以为这城市没地方可去。
        raise HTTPException(
            status_code=422,
            detail="这座城市还没有攻略数据。带上 ?name=城市名 才能回落搜高德。",
        )

    return CityPoolOut(
        city_adcode=pool.city_adcode,
        candidates=[
            CandidateOut(
                poi_id=item.poi_id,
                name=item.name,
                source=item.source,
                address=item.address,
                lat_gcj02=item.lat_gcj02,
                lng_gcj02=item.lng_gcj02,
                rating=item.rating,
                open_time=item.open_time,
                highlights=[_claim_brief(claim) for claim in item.highlights],
                avoids=[_claim_brief(claim) for claim in item.avoids],
                claim_count=item.claim_count,
                high_confidence_count=item.high_confidence_count,
                recommended=item.recommended,
                booking_days=item.booking_days,
                booking_time=item.booking_time,
            )
            for item in pool.candidates
        ],
        recommended_count=pool.recommended_count,
        covered=pool.covered,
        amap_error=pool.amap_error,
    )
