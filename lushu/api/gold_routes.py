"""金标准集与评测的接口。

在数据工作台的页面里，这一块与待对齐共用一套形状（Q47）：**左边给人看材料，
右边给人做判断**。区别只在判断的对象——那边判「这条提及指的是哪个景点」，
这边判「原文里到底有哪些真结论」。

一条与待对齐同样的原则：**接口只呈现，不代替人判断**。标注必须给出原文
片段，位置由服务端算；评测只报数字与样本量，不在样本不足时给出「很好」的
结论。`docs/M3-STATUS.md` 里那句「样本太小，不构成可信数字」是这套接口
要守住的态度。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from lushu.services import evaluation as ev
from lushu.services import gold_store as gs

router = APIRouter(prefix="/api/workbench/gold", tags=["金标准集"])
eval_router = APIRouter(prefix="/api/workbench", tags=["金标准集"])


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ─── 响应体 ──────────────────────────────────────────────────


class GoldDocumentOut(BaseModel):
    """一篇素材在金标准集里的状态。"""

    document_id: str
    title: str | None
    site: str
    chars: int
    prediction_count: int
    labeled: int
    in_gold_set: bool
    model_output_seen: bool
    annotated_at: str | None


class GoldLabelOut(BaseModel):
    """一条人工标注。"""

    label_id: str
    quote: str
    polarity: str
    subject_type: str
    subject_name: str | None
    expected_poi_id: str | None
    facet: str | None
    char_start: int | None
    char_end: int | None
    verdict: str
    note: str | None
    created_at: str


class GoldCandidateOut(BaseModel):
    """模型抽出的一条候选，标注时拿来对照。

    界面上它要显眼地标一句「这是模型说的，不是原文」：看过模型输出再标，
    人会不自觉地只修模型给的东西，而漏掉模型压根没抽到的那些。
    """

    subject_name: str
    polarity: str
    facet: str
    text: str
    quote: str
    char_start: int | None = None
    char_end: int | None = None


class GoldContextOut(BaseModel):
    """标注一篇素材需要的全部材料。"""

    document_id: str
    title: str | None
    body: str
    in_gold_set: bool
    model_output_seen: bool
    labels: list[GoldLabelOut]
    candidates: list[GoldCandidateOut]


class GoldLabelIn(BaseModel):
    """新增一条标注。

    `quote` 必须能在原文里原样找到——**这条限制是刻意的**，由服务端定位并
    记录偏移。没有它，标注会慢慢退化成「凭印象写结论」，
    而那种金标准测不出任何东西。
    """

    quote: str = Field(min_length=1)
    polarity: str = Field(pattern="^(avoid|highlight)$")
    subject_type: str = Field(default="poi", pattern="^(poi|route|city)$")
    subject_name: str | None = None
    expected_poi_id: str | None = None
    facet: str | None = None
    note: str | None = None


class GoldPatchIn(BaseModel):
    """补全一条已有标注。

    标注是反复来回的：先照着原文把事实写下来，再去查这个提及指哪个景点。
    没有这个口子，人只能删掉重加，而重加之后 `label_id` 变了。
    """

    expected_poi_id: str | None = None
    set_poi: bool = False
    subject_name: str | None = None
    facet: str | None = None
    note: str | None = None


class GoldDoneIn(BaseModel):
    """标记一篇素材的标注完成（或取消）。

    `model_output_seen` 记下标注时有没有看过模型输出，它是日后判断
    「某个指标偏乐观是不是标注方式造成的」的唯一依据。
    """

    done: bool = True
    model_output_seen: bool = False
    annotator: str | None = None
    note: str | None = None


class ScoreOut(BaseModel):
    """一篇素材的评分。"""

    document_id: str
    hits: int
    gold_total: int
    predicted_total: int
    recall: float | None
    precision: float | None
    alignment_accuracy: float | None
    unaligned: int
    missed: list[str]
    spurious: list[str]


class EvalOut(BaseModel):
    """一次评测的结果。

    `sample_size` 与 `ready` 不是装饰：样本不足时数字照样算得出来，
    但必须与样本量一起出现，否则 2 篇素材的 100% 会被当成结论。
    """

    sample_size: int
    ready: bool
    gold_labels: int
    gold_documents: int
    blind_documents: int
    hits: int
    gold_total: int
    predicted_total: int
    recall: float | None
    precision: float | None
    alignment_accuracy: float | None
    unaligned: int
    misaligned: int
    discard_ratio: float | None
    documents: list[ScoreOut]


# ─── 标注 ────────────────────────────────────────────────────


@router.get("", response_model=list[GoldDocumentOut])
def list_gold_documents() -> list[GoldDocumentOut]:
    """素材的标注状态。未标注的排前面——工作台要能直接看出下一篇标哪篇。"""
    return [
        GoldDocumentOut(
            document_id=item.document_id,
            title=item.title,
            site=item.site,
            chars=item.chars,
            prediction_count=item.prediction_count,
            labeled=item.labeled,
            in_gold_set=item.in_gold_set,
            model_output_seen=item.model_output_seen,
            annotated_at=item.annotated_at,
        )
        for item in gs.gold_documents()
    ]


@router.get("/{document_id}", response_model=GoldContextOut)
def gold_context(document_id: str) -> GoldContextOut:
    """标注一篇素材需要的材料：正文、模型候选、已有标注。

    正文整篇返回。这是本地自用工具，素材本来就存在本机库里；
    标注时必须能读到上下文，否则判不出「这句话说的是哪件事」。
    """
    from lushu.store import connect

    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, title, body_text FROM source_document WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="没有这篇素材")
        labels = gs.labels_for_document(document_id, conn=conn)
        predictions = ev.predictions_for_document(document_id, conn=conn)
        state = next(
            (item for item in gs.gold_documents(conn=conn) if item.document_id == document_id),
            None,
        )
    finally:
        conn.close()

    return GoldContextOut(
        document_id=document_id,
        title=row["title"],
        body=row["body_text"],
        in_gold_set=bool(state and state.in_gold_set),
        model_output_seen=bool(state and state.model_output_seen),
        labels=[_label_out(item) for item in labels],
        candidates=[
            GoldCandidateOut(
                subject_name=item.subject_name,
                polarity=item.polarity,
                facet=item.facet,
                text=item.text,
                quote=item.quote,
                char_start=item.char_start,
                char_end=item.char_end,
            )
            for item in predictions
        ],
    )


def _label_out(row: gs.GoldLabelRow) -> GoldLabelOut:
    return GoldLabelOut(
        label_id=row.label_id,
        quote=row.quote,
        polarity=row.polarity,
        subject_type=row.subject_type,
        subject_name=row.subject_name,
        expected_poi_id=row.expected_poi_id,
        facet=row.facet,
        char_start=row.char_start,
        char_end=row.char_end,
        verdict=row.verdict,
        note=row.note,
        created_at=row.created_at,
    )


@router.post("/{document_id}/labels", response_model=GoldLabelOut, status_code=201)
def add_label(document_id: str, payload: GoldLabelIn) -> GoldLabelOut:
    """加一条标注。

    引文找不到原文时返回 422，并说明原因——**不做「差不多就行」的模糊匹配**：
    位置是评测判「事实点是否相同」的主判据，位置错了评测就静默错判。
    """
    from lushu.store import transaction

    try:
        with transaction() as conn:
            row = gs.add_label(
                conn=conn,
                document_id=document_id,
                quote=payload.quote,
                polarity=payload.polarity,
                subject_name=payload.subject_name,
                subject_type=payload.subject_type,
                expected_poi_id=payload.expected_poi_id,
                facet=payload.facet,
                note=payload.note,
                created_at=_now(),
            )
    except gs.GoldError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return _label_out(row)


@router.delete("/labels/{label_id}", status_code=204)
def remove_label(label_id: str) -> None:
    """删掉一条标注。"""
    from lushu.store import transaction

    with transaction() as conn:
        if not gs.remove_label(conn=conn, label_id=label_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="没有这条标注")


@router.patch("/labels/{label_id}", response_model=GoldLabelOut)
def patch_label(label_id: str, payload: GoldPatchIn) -> GoldLabelOut:
    """补全一条标注：挂上 POI、改主体名或 facet。

    引文与位置改不了——它们是这条标注的锚点，要换引文就删掉重加。
    """
    from lushu.store import transaction

    with transaction() as conn:
        row = gs.update_label(
            conn=conn,
            label_id=label_id,
            expected_poi_id=payload.expected_poi_id,
            subject_name=payload.subject_name,
            facet=payload.facet,
            note=payload.note,
            set_poi=payload.set_poi,
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="没有这条标注")
    return _label_out(row)


@router.post("/{document_id}/done", response_model=GoldDocumentOut)
def mark_done(document_id: str, payload: GoldDoneIn) -> GoldDocumentOut:
    """把一篇素材记入金标准集，或撤出。

    「标完了但一条结论都没有」是合法且重要的结果——那种素材进召回率的分母，
    靠的就是这个标记而不是有没有标注行。
    """
    from lushu.store import connect, transaction

    with transaction() as conn:
        exists = conn.execute(
            "SELECT 1 FROM source_document WHERE id = ?", (document_id,)
        ).fetchone()
        if exists is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="没有这篇素材")
        if payload.done:
            gs.mark_annotated(
                conn=conn,
                document_id=document_id,
                model_output_seen=payload.model_output_seen,
                annotator=payload.annotator,
                note=payload.note,
                annotated_at=_now(),
            )
        else:
            gs.unmark_annotated(conn=conn, document_id=document_id)

    conn = connect()
    try:
        state = next(
            (item for item in gs.gold_documents(conn=conn) if item.document_id == document_id),
            None,
        )
    finally:
        conn.close()
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="没有这篇素材")
    return GoldDocumentOut(
        document_id=state.document_id,
        title=state.title,
        site=state.site,
        chars=state.chars,
        prediction_count=state.prediction_count,
        labeled=state.labeled,
        in_gold_set=state.in_gold_set,
        model_output_seen=state.model_output_seen,
        annotated_at=state.annotated_at,
    )


# ─── 评测 ────────────────────────────────────────────────────


@eval_router.get("/eval", response_model=EvalOut)
def run_eval(document: Annotated[list[str] | None, Query()] = None) -> EvalOut:
    """跑一遍评测，报出抽取精确率、召回率与对齐准确率。

    `ready=False` 时数字仍然返回——藏起来更糟，会让人以为「还没跑」而不是
    「样本不够」。界面上要与样本量并排显示。
    """
    bundle = ev.run_evaluation(document_ids=document)
    report = bundle.report
    tally = ev.alignment_tally()

    return EvalOut(
        sample_size=report.sample_size,
        ready=bundle.ready,
        gold_labels=bundle.stats.get("labels", 0),
        gold_documents=bundle.stats.get("documents", 0),
        blind_documents=bundle.stats.get("blind", 0),
        hits=report.hits,
        gold_total=report.gold_total,
        predicted_total=report.predicted_total,
        recall=report.recall,
        precision=report.precision,
        alignment_accuracy=report.alignment_accuracy,
        unaligned=report.unaligned,
        misaligned=report.misaligned,
        discard_ratio=tally.discard_ratio,
        documents=[
            ScoreOut(
                document_id=item.document_id,
                hits=item.hits,
                gold_total=item.gold_total,
                predicted_total=item.predicted_total,
                recall=item.recall,
                precision=item.precision,
                alignment_accuracy=item.alignment_accuracy,
                unaligned=item.unaligned,
                # 漏抽与多抽的原文都要给出来：只看一个百分比无从下手，
                # 而这两份清单直接就是调提示词的依据
                missed=[gold.quote for gold in item.pairing.missed],
                spurious=[f"{bad.prediction.subject_name}：{bad.reason}" for bad in item.pairing.spurious],
            )
            for item in report.documents
        ],
    )
