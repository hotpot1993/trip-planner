"""跑评测：把库里的预测与金标准取出来，交给 `lushu.domain.evaluation` 比对。

匹配规则本身是纯函数，在 domain 层；这里只管**取数**与**算账的边界**。
取数有两处必须说清楚，否则指标会被悄悄扭曲：

**预测池取自 `extraction_run.accepted_json`，不取自 `claim` 表。**
落库的 claim 只是「抽到且对齐上了」的那些，拿它当预测池会把对齐失败
算成抽取错误——那正是三个指标要分开的原因。提纯的输出留在提纯自己的
运行记录上，与人工看到的待对齐队列是同一批东西。

**分母只含已标注的素材。** 没进金标准集的篇，既不知道它有几条真结论，
也不知道模型抽的对不对，算进来只会让分母变成一个说不清的东西。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from lushu.domain.evaluation import EvalReport, GoldItem, Prediction, evaluate
from lushu.services import gold_store as gs
from lushu.store.connection import connect

# 一次提纯抽出的候选上限：`accepted_json` 是老版本行里没有的列，
# 也是「这篇提纯过但没留下候选」时的兜底说明
_NO_PREDICTIONS = "这篇素材还没有成功提纯过，跑 ls extract run 之后再来评测"


@dataclass(frozen=True)
class EvalBundle:
    """一次评测的完整输入。报告之外的东西也留着，界面要用。"""

    report: EvalReport
    stats: dict[str, int]
    documents: tuple[gs.GoldDocument, ...]

    @property
    def ready(self) -> bool:
        """金标准集够不够撑起一个可信的数字。

        30 篇是设计里定的目标（第十一节风险表：需要 30 篇素材的人工标注集）。
        不到这个量照样出数字，但报告必须如实标注样本量——一个 2 篇素材
        算出来的 100% 精确率不是结论，是噪声。
        """
        return self.stats.get("documents", 0) >= 30


def predictions_for_document(
    document_id: str, *, conn: sqlite3.Connection | None = None
) -> list[Prediction]:
    """一篇素材最近一次成功提纯抽出的全部候选。

    同一主体名、同一极性的候选可能在 `claim` 表里已经合并成了一条，
    但评测比的是**提纯这一步的输出**，所以这里按原样返回，不做合并。

    对齐结果（这条候选最后挂到了哪个 POI）不在提纯输出里，得另外接上来，
    见 `_alignment_of`。
    """
    owned = conn is None
    active = conn or connect()
    try:
        row = active.execute(
            "SELECT accepted_json FROM extraction_run "
            "WHERE source_document_id = ? AND status = 'ok' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (document_id,),
        ).fetchone()
        payload = _load_accepted(row["accepted_json"]) if row and row["accepted_json"] else []
        if not payload:
            payload = _legacy_payload(active, document_id)
        if not payload:
            return []
        aligned = _alignment_of(active, document_id)
    finally:
        if owned:
            active.close()

    predictions: list[Prediction] = []
    for item in payload:
        predictions.append(
            Prediction(
                subject_name=str(item.get("subject_name") or ""),
                polarity=str(item.get("polarity") or ""),
                text=str(item.get("text") or ""),
                quote=str(item.get("quote") or ""),
                subject_type=str(item.get("subject_type") or "poi"),
                facet=str(item.get("facet") or "other"),
                char_start=_as_int(item.get("char_start")),
                char_end=_as_int(item.get("char_end")),
                poi_id=aligned.get(
                    (item.get("char_start"), item.get("char_end")),
                    aligned.get(("quote", str(item.get("quote") or ""))),
                ),
            )
        )
    return predictions


def _load_accepted(raw: str) -> list[dict]:
    """解析提纯运行留下的候选载荷。库里的 JSON 不能盲信。"""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _legacy_payload(conn: sqlite3.Connection, document_id: str) -> list[dict]:
    """迁移 9 之前提纯过的篇，候选散在两个地方，都要捡回来。

    `extraction_run.accepted_json` 是迁移 9 才加的列，早先跑过的篇在那一列上
    是空的。好在同一批载荷当时分头落到了两处，合起来是完整的：

    - `alignment_task.extracted_claims_json`：提纯时按主体名归堆写下的候选。
      对不上的、被判非地点的都在这里。
    - `claim_evidence` ⋈ `claim`：对齐成功之后落库的那些。**这一路不能省**——
      主体名已经人工处置过的候选不会再开待办（`_already_handled`），
      只看待办会漏掉它们。

    两边会有重叠，按「主体名 + 结论文本 + 原文位置」去重。这条路径只对老数据
    有意义，新数据一律走 `accepted_json`。
    """
    payload: list[dict] = []
    seen: set[tuple] = set()

    def add(item: dict) -> None:
        key = (
            item.get("subject_name"),
            item.get("text"),
            item.get("char_start"),
            item.get("char_end"),
        )
        if key in seen:
            return
        seen.add(key)
        payload.append(item)

    rows = conn.execute(
        "SELECT extracted_claims_json FROM alignment_task WHERE source_document_id = ?",
        (document_id,),
    ).fetchall()
    for row in rows:
        for item in _load_accepted(row["extracted_claims_json"] or ""):
            add(item)

    rows = conn.execute(
        "SELECT c.subject_name, c.subject_type, c.polarity, c.facet, c.text, "
        "  e.quote, e.char_start, e.char_end "
        "FROM claim_evidence e JOIN claim c ON c.id = e.claim_id "
        "WHERE e.source_document_id = ?",
        (document_id,),
    ).fetchall()
    for row in rows:
        add(
            {
                "subject_name": row["subject_name"],
                "subject_type": row["subject_type"],
                "polarity": row["polarity"],
                "facet": row["facet"],
                "text": row["text"],
                "quote": row["quote"],
                "char_start": row["char_start"],
                "char_end": row["char_end"],
            }
        )
    return payload


def _alignment_of(conn: sqlite3.Connection, document_id: str) -> dict[tuple, str]:
    """这篇素材的候选落库之后挂到了哪个 POI。

    预测池取自提纯输出，而提纯的输出里没有对齐结果——对齐发生在后面，
    结果记在 `claim.poi_id` 上。两边的连接点是证据行：候选落库时，
    引文与偏移是**原样**写进 `claim_evidence` 的（`_materialize_claims`），
    所以 `(char_start, char_end)` 就是这条候选落库后的身份。
    偏移算不出来时退回按引文全文比对。
    """
    rows = conn.execute(
        "SELECT e.quote, e.char_start, e.char_end, c.poi_id "
        "FROM claim_evidence e JOIN claim c ON c.id = e.claim_id "
        "WHERE e.source_document_id = ? AND c.poi_id IS NOT NULL",
        (document_id,),
    ).fetchall()
    table: dict[tuple, str] = {}
    for row in rows:
        table[(row["char_start"], row["char_end"])] = row["poi_id"]
        table.setdefault(("quote", row["quote"]), row["poi_id"])
    return table


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def gold_items_for_document(
    document_id: str, *, conn: sqlite3.Connection | None = None
) -> list[GoldItem]:
    """一篇素材的全部人工标注。"""
    return [
        GoldItem(
            label_id=row.label_id,
            quote=row.quote,
            polarity=row.polarity,
            subject_type=row.subject_type,
            subject_name=row.subject_name,
            expected_poi_id=row.expected_poi_id,
            facet=row.facet,
            char_start=row.char_start,
            char_end=row.char_end,
        )
        for row in gs.labels_for_document(document_id, conn=conn)
    ]


def run_evaluation(
    *, conn: sqlite3.Connection | None = None, document_ids: list[str] | None = None
) -> EvalBundle:
    """跑一遍评测。只统计已记入金标准集的素材。"""
    owned = conn is None
    active = conn or connect()
    try:
        documents = gs.gold_documents(conn=active)
        if document_ids is not None:
            wanted = set(document_ids)
            documents = [item for item in documents if item.document_id in wanted]

        golds: dict[str, list[GoldItem]] = {}
        predictions: dict[str, list[Prediction]] = {}
        for document in documents:
            if not document.in_gold_set:
                continue
            golds[document.document_id] = gold_items_for_document(
                document.document_id, conn=active
            )
            predictions[document.document_id] = predictions_for_document(
                document.document_id, conn=active
            )

        return EvalBundle(
            report=evaluate(predictions, golds),
            stats=gs.gold_stats(conn=active),
            documents=tuple(documents),
        )
    finally:
        if owned:
            active.close()


# ─── 对齐准确率的另一半：人工处置的回流 ─────────────────────────
#
# 金标准集只能测「标注过的那几篇」。对齐在别的素材上也发生了——
# 人工在待对齐队列里处置过的每一条，都是一次由人给出的对齐判断。
# 这类判断的样本量比标注大得多，而且是免费的：处置待办本来就要做。
#
# 它与金标准集算出来的对齐准确率**必须分开报**，因为偏差方向不同：
# 金标准集里的素材是人挑的，人工队列里的待办是模型挑的（对不上的才会进队列）。
# 把两者平均成一个数字，等于把两种抽样方式混在一起。


@dataclass(frozen=True)
class AlignmentTally:
    """人工处置过的对齐结果统计。"""

    resolved: int  # 人选了某个 POI
    discarded: int  # 人判定「这不是一个地点」

    @property
    def total(self) -> int:
        return self.resolved + self.discarded

    @property
    def discard_ratio(self) -> float | None:
        """被判定为非地点的比例。

        这个数字衡量的是**上游提纯的主体识别质量**：比例高说明模型把
        「排队」「门票」这类词也当成了景点名去搜，浪费的是人的时间。
        """
        return self.discarded / self.total if self.total else None


def alignment_tally(*, conn: sqlite3.Connection | None = None) -> AlignmentTally:
    """人工处置过的待对齐里，选择与丢弃各占多少。"""
    owned = conn is None
    active = conn or connect()
    try:
        row = active.execute(
            "SELECT SUM(status = 'resolved') AS resolved, "
            "  SUM(status = 'discarded') AS discarded "
            "FROM alignment_task WHERE status IN ('resolved', 'discarded')"
        ).fetchone()
    finally:
        if owned:
            active.close()
    return AlignmentTally(resolved=row["resolved"] or 0, discarded=row["discarded"] or 0)


# ─── ADR-0009 体检 ──────────────────────────────────────────────
#
# 结论一律挂**本体**，子点只作为「午门属于故宫博物院」这层关系留在库里。
# 这条规则是金标准集第一次跑起来时抓出来的：评测报出「太和殿 挂错」，
# 追下去发现它挂在 `故宫博物院-太和殿` 上，而不是 `故宫博物院`。
#
# 根因不是归并逻辑，是**归并逻辑修好之前的旧数据**：`align_pending` 只处理
# pending 的待办，一条待办被解析之后就再也不会被重新检查，于是对齐算法的
# 每一次改进都只对新数据生效。这个体检就是给这种「陈旧结论」用的。


@dataclass(frozen=True)
class Violation:
    """一条挂到子点上的结论或待办。"""

    kind: str  # claim | task
    ref_id: str
    subject: str
    poi_id: str
    poi_name: str
    root_id: str | None
    root_name: str | None


def sub_poi_violations(*, conn: sqlite3.Connection | None = None) -> list[Violation]:
    """列出所有挂到子点上的结论与待办（违反 ADR-0009）。

    只报告，不改写。要不要把它挪到本体上，得看那条结论说的是子点本身的事
    （「太和殿要另外买票」）还是整个本体的事；这需要人判断，
    自动挪会静默改掉结论的含义。
    """
    owned = conn is None
    active = conn or connect()
    try:
        rows = active.execute(
            "SELECT 'claim' AS kind, c.id AS ref_id, COALESCE(c.subject_name, '') AS subject, "
            "  p.amap_poi_id AS poi_id, p.name AS poi_name, "
            "  r.amap_poi_id AS root_id, r.name AS root_name "
            "FROM claim c JOIN poi p ON p.amap_poi_id = c.poi_id "
            "LEFT JOIN poi r ON r.amap_poi_id = p.parent_poi_id "
            "WHERE p.parent_poi_id IS NOT NULL "
            "UNION ALL "
            "SELECT 'task', t.id, t.mention_name, p.amap_poi_id, p.name, "
            "  r.amap_poi_id, r.name "
            "FROM alignment_task t JOIN poi p ON p.amap_poi_id = t.resolved_poi_id "
            "LEFT JOIN poi r ON r.amap_poi_id = p.parent_poi_id "
            "WHERE p.parent_poi_id IS NOT NULL "
            "ORDER BY kind, subject"
        ).fetchall()
    finally:
        if owned:
            active.close()

    return [
        Violation(
            kind=row["kind"],
            ref_id=row["ref_id"],
            subject=row["subject"],
            poi_id=row["poi_id"],
            poi_name=row["poi_name"],
            root_id=row["root_id"],
            root_name=row["root_name"],
        )
        for row in rows
    ]
