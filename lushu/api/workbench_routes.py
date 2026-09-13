"""数据工作台的接口。

三类队列共用一套形状（Q47）：待对齐、待复核预约规则、待标注金标准。
M3 做了**待对齐**、**提纯概览**与**金标准标注**（在 `gold_routes.py`），
M4 补上**预约规则的复核**。

一条贯穿本模块的原则：**接口只呈现，不自动决定**。对不上的提及不会因为
「前五名里有一个看起来挺像」就被自动接受，那种自动决定正是设计里
最危险的失败模式（跨城挂错、挂到停车场）。预约规则的复核同理：
接口给出规则全文、来源链接与体检结果，签不签字由人定。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from fastapi import Path as PathParam
from pydantic import BaseModel

from lushu.domain.booking import RuleStatus, Severity, lint_rule
from lushu.services import booking_store, pipeline
from lushu.services import knowledge_store as ks

router = APIRouter(prefix="/api/workbench", tags=["数据工作台"])


# ─── 响应体 ──────────────────────────────────────────────────


class CandidateOut(BaseModel):
    """高德给的一个候选，附带它为什么被拒绝。"""

    poi_id: str
    name: str
    typecode: str | None = None
    address: str | None = None
    adcode: str | None = None
    name_score: float | None = None
    usable: bool = True
    reject_reason: str | None = None


class PendingClaimOut(BaseModel):
    """还没落库的一条候选结论。"""

    subject_name: str
    subject_type: str
    polarity: str
    facet: str
    text: str
    quote: str
    char_start: int | None = None
    char_end: int | None = None
    quote_verdict: str | None = None


class AlignmentTaskOut(BaseModel):
    """待对齐队列的一条。"""

    task_id: str
    mention_name: str
    city_adcode: str | None
    context_snippet: str | None
    source_document_id: str | None
    source_title: str | None = None
    candidates: list[CandidateOut]
    claims: list[PendingClaimOut]
    created_at: str


class ClaimOut(BaseModel):
    """已落库的一条结论。"""

    claim_id: str
    subject_type: str
    subject_name: str | None
    poi_id: str | None
    city_adcode: str | None
    polarity: str
    facet: str
    text: str
    confidence: str
    independent_source_count: int
    status: str
    first_seen_at: str
    verify_due_at: str | None
    evidence_count: int


class EvidenceOut(BaseModel):
    """一条结论的证据。进路书的就是 quote 这一段。"""

    quote: str
    site: str
    url: str | None = None
    title: str | None = None
    char_start: int | None = None
    char_end: int | None = None


class ExtractionRunOut(BaseModel):
    """一次提纯运行。`dropped_count` 是「模型编了引文」的直接证据。"""

    run_id: str
    source_document_id: str
    source_title: str | None = None
    model: str
    prompt_version: str
    candidate_count: int
    accepted_count: int
    dropped_count: int
    duration_ms: int | None
    status: str
    error: str | None
    created_at: str


class PipelineStatsOut(BaseModel):
    """链路各环节的规模。"""

    documents: int
    groups: int
    claims: int
    high_confidence: int
    evidence: int
    extract_documents: int
    extract_candidates: int
    extract_accepted: int
    extract_dropped: int
    align_pending: int
    align_resolved: int
    align_discarded: int


class ResolveIn(BaseModel):
    """人工处置一条待对齐。

    `poi_id` 给了就是「这条提及指的就是它」；
    `discard=True` 表示「这条提及本来就不是一个地点」，那个结论按设计丢掉。
    两者必须给一个——不提供「什么都不做就算处理了」这种含糊状态。
    """

    poi_id: str | None = None
    discard: bool = False


# ─── 待对齐队列 ──────────────────────────────────────────────


@router.get("/alignments", response_model=list[AlignmentTaskOut])
def list_alignments(limit: int = Query(default=100, ge=1, le=500)) -> list[AlignmentTaskOut]:
    """待对齐队列。

    每条要带**候选与拒绝理由**：人工只看一个提及名是没法判断的，
    得知道高德返回了什么、哪些被城市或类型门禁挡掉了。
    """

    from lushu.store import connect

    conn = connect()
    try:
        tasks = ks.pending_alignments(conn=conn, limit=limit)
        titles = _document_titles(conn, [task.source_document_id for task in tasks])
    finally:
        conn.close()

    return [_task_out(task, titles.get(task.source_document_id or "")) for task in tasks]


def _task_out(task: ks.PendingAlignment, title: str | None) -> AlignmentTaskOut:
    return AlignmentTaskOut(
        task_id=task.task_id,
        mention_name=task.mention_name,
        city_adcode=task.city_adcode,
        context_snippet=task.context_snippet,
        source_document_id=task.source_document_id,
        source_title=title,
        candidates=[CandidateOut(**item) for item in task.candidates],
        claims=[PendingClaimOut(**item) for item in task.claims],
        created_at=task.created_at,
    )


def _document_titles(conn, document_ids: list[str | None]) -> dict[str, str]:
    """批量取素材标题，供界面显示「这条来自哪篇」。"""
    ids = sorted({item for item in document_ids if item})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, COALESCE(title, '(无标题)') AS title FROM source_document "
        f"WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    return {row["id"]: row["title"] for row in rows}


@router.post("/alignments/{task_id}/resolve", response_model=ClaimOut | None)
def resolve_alignment(task_id: str, payload: ResolveIn) -> ClaimOut | None:
    """处置一条待对齐。

    三种结局：

    - 指定 `poi_id`：把这条待办的候选结论挂到该 POI 上，落库成结论
    - `discard=True`：判定「这不是一个地点」，那条结论按设计丢掉
    - POI 取不到、城市没入库：422，不做任何写入

    刻意**不**提供「自动选最高分」的分支：那正是设计里要避免的自动决定。

    落库逻辑在 `lushu/services/pipeline.py`，与自动对齐走同一段代码——
    差别只在主体从哪来：自动那一步来自高德搜索，这一步来自人。
    """
    from lushu.store import connect, transaction

    conn = connect()
    try:
        row = conn.execute(
            "SELECT status FROM alignment_task WHERE id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="没有这条待办")
    if row["status"] != ks.ALIGN_STATUS_PENDING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"这条待办已经处置过了（{row['status']}）",
        )

    if not payload.discard and not payload.poi_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="必须给出 poi_id，或显式 discard 表示这不是一个地点",
        )

    try:
        with transaction() as conn:
            created = pipeline.resolve_alignment_task(
                conn=conn,
                task_id=task_id,
                poi_id=payload.poi_id,
                discard=payload.discard,
            )
    except pipeline.PipelineError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    if not created or not payload.poi_id:
        # 判定为非地点，或者全部候选的引文都已回不到原文——如实返回空
        return None

    conn = connect()
    try:
        claims_now = ks.claims_for_poi(payload.poi_id, conn=conn)
    finally:
        conn.close()
    return _claim_out(claims_now[0]) if claims_now else None


# ─── 结论与提纯概览 ──────────────────────────────────────────


def _claim_out(claim: ks.ClaimRow) -> ClaimOut:
    return ClaimOut(
        claim_id=claim.claim_id,
        subject_type=claim.subject_type,
        subject_name=claim.subject_name,
        poi_id=claim.poi_id,
        city_adcode=claim.city_adcode,
        polarity=claim.polarity,
        facet=claim.facet,
        text=claim.text,
        confidence=claim.confidence.value,
        independent_source_count=claim.independent_source_count,
        status=claim.status,
        first_seen_at=claim.first_seen_at,
        verify_due_at=claim.verify_due_at,
        evidence_count=claim.evidence_count,
    )


@router.get("/claims", response_model=list[ClaimOut])
def list_claims(
    poi_id: str | None = Query(default=None),
    city_adcode: str | None = Query(default=None),
    confidence: str | None = Query(default=None, pattern="^(high|single_source)$"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[ClaimOut]:
    """攻略知识库里的结论。M5 的候选池会从这里取。"""
    rows = ks.list_claims(
        poi_id=poi_id, city_adcode=city_adcode, confidence=confidence, limit=limit
    )
    return [_claim_out(row) for row in rows]


@router.get("/claims/{claim_id}/evidence", response_model=list[EvidenceOut])
def claim_evidence(claim_id: str = PathParam()) -> list[EvidenceOut]:
    """一条结论的全部证据。

    这是「这条说法到底谁说的」的答案，也是产品可信度的落点：
    没有这一段，界面上的每一条建议都只是无出处的断言。
    """
    rows = ks.evidence_for_claim(claim_id)
    return [
        EvidenceOut(
            quote=row["quote"],
            site=row["site"],
            url=row["url"],
            title=row["title"],
            char_start=row["char_start"],
            char_end=row["char_end"],
        )
        for row in rows
    ]


@router.get("/extractions", response_model=list[ExtractionRunOut])
def list_extractions(limit: int = Query(default=50, ge=1, le=500)) -> list[ExtractionRunOut]:
    """最近的提纯运行。

    `dropped_count` 不为零的篇要重点看——那说明模型编了原文里没有的引文，
    而被引文校验挡住了。这个数字是「提纯不产生新事实」这条设计的体检指标。
    """
    from lushu.store import connect

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT r.*, COALESCE(d.title, '(无标题)') AS title FROM extraction_run r "
            "LEFT JOIN source_document d ON d.id = r.source_document_id "
            "ORDER BY r.created_at DESC, r.rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    return [
        ExtractionRunOut(
            run_id=row["id"],
            source_document_id=row["source_document_id"],
            source_title=row["title"],
            model=row["model"],
            prompt_version=row["prompt_version"],
            candidate_count=row["candidate_count"],
            accepted_count=row["accepted_count"],
            dropped_count=row["dropped_count"],
            duration_ms=row["duration_ms"],
            status=row["status"],
            error=row["error"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


@router.get("/stats", response_model=PipelineStatsOut)
def workbench_stats() -> PipelineStatsOut:
    """链路概览。工作台首页用它决定「现在该做什么」。"""
    return PipelineStatsOut(**pipeline.pipeline_stats())  # type: ignore[arg-type]


class LocalPoiOut(BaseModel):
    """库里已有的一个 POI。"""

    poi_id: str
    name: str
    label: str
    city_name: str | None
    parent_id: str | None
    parent_name: str | None
    typecode: str | None
    address: str | None
    is_root: bool


@router.get("/pois", response_model=list[LocalPoiOut])
def local_pois(
    q: str = Query(default="", max_length=60),
    city_adcode: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[LocalPoiOut]:
    """在库里已有的 POI 里按名称找。

    标注金标准时要填「这条提及指的是哪个景点」，那个字段应当填**管线能真正
    产出的实体**。去问一次高德会给更多候选，但也会给出管线当下对不上的，
    那样的金标准会把对齐准确率变成一个够不着的指标。
    """
    return [
        LocalPoiOut(
            poi_id=row.poi_id,
            name=row.name,
            label=row.label,
            city_name=row.city_name,
            parent_id=row.parent_id,
            parent_name=row.parent_name,
            typecode=row.typecode,
            address=row.address,
            is_root=row.is_root,
        )
        for row in ks.search_local_pois(q, city_adcode=city_adcode, limit=limit)
    ]


# ─── 待复核的预约规则（Q47 的第三类队列）────────────────────────


class BookingRuleOut(BaseModel):
    """一条预约规则，连同它的体检结果。

    `findings` 要与规则**一起**返回：复核是「看着体检结果签字」，
    把两者分两次请求，界面就有机会显示一条没有体检结论的规则。
    """

    poi_id: str
    poi_name: str
    status: str  # draft | reviewed
    booking_required: bool
    advance_days: int | None
    release_time: str | None
    channels: list[dict]
    requires_real_name: bool | None
    id_required_note: str | None
    closed_days_weekdays: list[int]
    evidence_url: str | None
    reviewed_at: str | None
    verify_due_at: str | None
    reviewer_note: str | None
    errors: list[str]
    warnings: list[str]

    @property
    def can_review(self) -> bool:
        return not self.errors


class ChannelIn(BaseModel):
    name: str
    kind: str
    url: str | None = None


class ReviewIn(BaseModel):
    """复核一条规则。

    允许在这一步补上来源链接与备注——人一边核一边发现问题、顺手补材料，
    是这条流程里最自然的动作。
    """

    evidence_url: str | None = None
    note: str | None = None
    # 撤回复核。规则会变（湖南博物院 2026-07 刚从「提前 7 天」改成「提前 5 天」），
    # 发现不对时要能立刻停止展示，而不是只能删掉重来。
    revoke: bool = False


def _rule_out(rule, name: str, *, today: date) -> BookingRuleOut:
    findings = lint_rule(rule, today=today, poi_name=name)
    return BookingRuleOut(
        poi_id=rule.poi_id,
        poi_name=name,
        status=rule.status.value,
        booking_required=rule.booking_required,
        advance_days=rule.advance_days,
        release_time=rule.release_time,
        channels=[
            {"name": item.name, "kind": item.kind.value, "url": item.url}
            for item in rule.channels
        ],
        requires_real_name=rule.requires_real_name,
        id_required_note=rule.id_required_note,
        closed_days_weekdays=list(rule.closed_days.weekdays),
        evidence_url=rule.evidence_url,
        reviewed_at=rule.reviewed_at.isoformat() if rule.reviewed_at else None,
        verify_due_at=rule.verify_due_at.isoformat() if rule.verify_due_at else None,
        reviewer_note=rule.reviewer_note,
        errors=[item.message for item in findings if item.severity is Severity.ERROR],
        warnings=[item.message for item in findings if item.severity is not Severity.ERROR],
    )


@router.get("/booking-rules", response_model=list[BookingRuleOut])
def list_booking_rules(
    pending_only: bool = Query(default=False),
) -> list[BookingRuleOut]:
    """预约规则的复核队列。

    `pending_only=true` 只看草案——复核队列里最该先看的就是这些：
    它们现在**不对用户可见**，于是行程上不会提醒，而这正是最怕白跑的地方。
    """
    rules = booking_store.all_rules()
    if pending_only:
        rules = [item for item in rules if item.status is RuleStatus.DRAFT]

    names = booking_store.poi_names()
    today = date.today()
    return [
        _rule_out(rule, names.get(rule.poi_id, rule.poi_id), today=today) for rule in rules
    ]


@router.post("/booking-rules/{poi_id}/review", response_model=BookingRuleOut)
def review_booking_rule(poi_id: str, payload: ReviewIn) -> BookingRuleOut:
    """复核（或撤回复核）一条预约规则。

    体检有 error 时**拒绝复核**并说明是哪一关没过——这是这道门禁唯一的意义：
    与其把一条算不出放票日的规则放出去，不如让它继续待在草案里报错。
    """
    from lushu.store import transaction

    rule = booking_store.get_rule(poi_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="没有这条预约规则")

    if payload.revoke:
        from lushu.domain.booking import BookingRule as RuleModel
        from lushu.services.booking_store import write_rule

        revoked = RuleModel(
            poi_id=rule.poi_id,
            booking_required=rule.booking_required,
            status=RuleStatus.DRAFT,
            advance_days=rule.advance_days,
            release_time=rule.release_time,
            channels=rule.channels,
            requires_real_name=rule.requires_real_name,
            id_required_note=rule.id_required_note,
            closed_days=rule.closed_days,
            evidence_url=rule.evidence_url,
            reviewed_at=None,
            reviewer_note=rule.reviewer_note,
        )
        with transaction() as conn:
            write_rule(conn=conn, rule=revoked, now=_now())
        names = booking_store.poi_names()
        return _rule_out(revoked, names.get(poi_id, poi_id), today=date.today())

    with transaction() as conn:
        ok = booking_store.review_rule(
            conn=conn,
            poi_id=poi_id,
            evidence_url=payload.evidence_url,
            note=payload.note,
        )

    names = booking_store.poi_names()
    today = date.today()
    if not ok:
        current = booking_store.get_rule(poi_id)
        assert current is not None
        out = _rule_out(current, names.get(poi_id, poi_id), today=today)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"复核没过：{'；'.join(out.errors) or '缺少来源链接'}",
        )

    updated = booking_store.get_rule(poi_id)
    assert updated is not None
    return _rule_out(updated, names.get(poi_id, poi_id), today=today)


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
