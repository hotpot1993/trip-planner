"""数据链路的编排：提纯 → 对齐 → 归组 → 合并入库。

四步的次序与边界都是有理由的，改次序会破坏设计里的某条约束：

| 步 | 做什么 | 为什么在这一步 |
|---|---|---|
| 提纯 | 调模型抽出候选 + 引文校验 | 引文对不上原文的直接丢弃（DESIGN 4.2），后面几步只见得到通过校验的候选 |
| 对齐 | 提及名 → 高德 POI 本体 | ADR-0009：挂错一层会生成孤立 POI；对不上的进待对齐队列（Q19） |
| 归组 | 提纯结果高度重合的两篇算同一来源 | **必须在提纯之后**——第一层的文字复制判据认不出逐句改写（ADR-0008） |
| 合并 | 同主体的同义结论合成一条，重算独立来源数 | 置信度按组计数（Q34），这一步是唯一的重算点 |

归组放在提纯之后这件事决定了整个管线的形状：`ls group run` 可以在
`ls extract run` 之后再跑，也可以反复跑。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from lushu.adapters.poi import PoiSearchError, fetch_poi, search_pois
from lushu.adapters.poi_lineage import complete_lineage
from lushu.domain.align import AlignOutcome, align
from lushu.domain.extraction import VerifiedClaim, verify_extraction
from lushu.domain.knowledge import (
    ClaimEvidence,
    Facet,
    Polarity,
    SubjectType,
    refresh_days_for,
)
from lushu.domain.lineage import CandidateSet
from lushu.domain.poi import CandidatePoi
from lushu.store.connection import connect
from lushu.store.ids import SOURCE_GROUP, new_id
from lushu.store.search import document_contains

from . import knowledge_store as ks

# 一批里最多处理多少篇。防止一次误操作把整个素材库交给模型。
DEFAULT_BATCH_LIMIT = 50


class PipelineError(RuntimeError):
    """管线无法继续。"""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class ExtractionReport:
    """提纯一轮的结果。丢了几条、为什么丢都要能看见。"""

    documents: int = 0
    cached: int = 0
    candidates: int = 0
    accepted: int = 0
    dropped: int = 0
    loose: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def drop_ratio(self) -> float:
        return self.dropped / self.candidates if self.candidates else 0.0


@dataclass
class AlignmentReport:
    """对齐一轮的结果。"""

    mentions: int = 0
    aligned: int = 0
    pending: int = 0
    collapsed: int = 0  # 命中子点、归并到本体的条数
    unresolved_subjects: int = 0  # 连城市都定不下来的提及
    failed: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class GroupReport:
    """第二层归组的结果。"""

    compared: int = 0
    merged: int = 0  # 因结论重合而被并进已有组的篇数
    groups: int = 0  # 现在有多少个来源组


@dataclass
class MergeReport:
    """合并入库的结果。"""

    created: int = 0
    extended: int = 0  # 给已有结论补了证据
    high_confidence: int = 0
    single_source: int = 0


@dataclass(frozen=True)
class Progress:
    """管线跑到哪了。

    **给人看的一行**放在 `label` 里，而不是让界面拿 `step` 与计数自己拼文案——
    拼文案的地方一多，同一件事在 CLI、SSE、日志里就会有三种说法。

    提纯一篇要 2.5–4.5 秒、对齐一条提及要一次高德请求，几十篇的批量能跑上
    几分钟。没有进度的话，界面只能对着空屏，人分不清「在跑」与「卡住」。
    """

    step: str  # extract | align | group | merge
    label: str
    index: int = 0
    total: int = 0


# 进度回调是同步的、可选的，默认什么都不做——服务层不该知道有没有人在听
ProgressHook = Callable[["Progress"], None]


def _noop(_progress: Progress) -> None:
    return None


# ─── 提纯 ────────────────────────────────────────────────────────


def extract_documents(
    *,
    conn: sqlite3.Connection | None = None,
    document_ids: list[str] | None = None,
    limit: int = DEFAULT_BATCH_LIMIT,
    force: bool = False,
    on_progress: ProgressHook | None = None,
) -> ExtractionReport:
    """对素材跑提纯，把通过引文校验的候选挂到待对齐队列上。

    为什么不在这一步直接写入 claim：对齐是另一步（可能要人工），
    而对不上的候选**不该进知识库**。所以提纯的产出先落在
    `alignment_task` 的载荷里，对齐成功后才由 `align_pending` 落库。

    `force=True` 连已经成功提纯过的篇也重跑一遍。它**不额外花钱**：
    `llm_cache` 按「提示词版本 + 模型 + 标题 + 正文」缓存，同样的输入直接命中。
    用途是补齐 `extraction_run.accepted_json`——迁移 9 之前跑的篇没有这一列，
    而它是评测的预测池（评测那边有从待办与证据行捡回来的兜底，
    但兜底终究会漏掉个别候选，重跑一遍才是完整的）。
    """
    from lushu.adapters.extract import ExtractionError, extract

    owned = conn is None
    active = conn or connect()
    report = ExtractionReport()
    notify = on_progress or _noop
    try:
        rows = _documents_to_extract(
            active, document_ids=document_ids, limit=limit, force=force
        )
        for index, row in enumerate(rows, 1):
            document_id = row["id"]
            report.documents += 1
            title = row["title"] or document_id
            notify(Progress("extract", f"提纯《{title}》", index, len(rows)))
            try:
                raw = extract(title=row["title"], body=row["body_text"], conn=active)
            except ExtractionError as exc:
                report.failed.append((document_id, str(exc)))
                ks.record_extraction_run(
                    conn=active,
                    source_document_id=document_id,
                    prompt_version="unknown",
                    model="unknown",
                    candidate_count=0,
                    accepted_count=0,
                    dropped_count=0,
                    status="failed",
                    error=str(exc),
                    created_at=_now(),
                )
                continue

            outcome = verify_extraction(
                document_id,
                row["body_text"],
                raw.claims,
                model=raw.model,
                prompt_version=raw.prompt_version,
                duration_ms=raw.duration_ms,
            )
            ks.record_extraction_run(
                conn=active,
                source_document_id=document_id,
                prompt_version=raw.prompt_version,
                model=raw.model,
                candidate_count=outcome.candidate_count,
                accepted_count=len(outcome.accepted),
                dropped_count=len(outcome.dropped),
                input_chars=len(row["body_text"]),
                output_chars=len(raw.raw_json or ""),
                duration_ms=raw.duration_ms,
                accepted=_accepted_payload(outcome),
                created_at=_now(),
            )

            _park_verified_claims(active, document_id=document_id, outcome=outcome, now=_now())

            report.cached += 1 if raw.from_cache else 0
            report.candidates += outcome.candidate_count
            report.accepted += len(outcome.accepted)
            report.dropped += len(outcome.dropped)
            report.loose += outcome.loose_count

        active.commit()
    finally:
        if owned:
            active.close()
    return report


def _documents_to_extract(
    conn: sqlite3.Connection,
    *,
    document_ids: list[str] | None,
    limit: int,
    force: bool = False,
) -> list[sqlite3.Row]:
    """挑出要提纯的素材。

    已经成功提纯过的不重复跑——重跑要花钱，而 `llm_cache` 只挡得住
    完全相同的输入。真正的去重靠这里：`extraction_run` 里有成功记录就跳过。
    `force` 例外，见 `extract_documents`。
    """
    if document_ids:
        placeholders = ",".join("?" for _ in document_ids)
        return conn.execute(
            f"SELECT id, title, body_text FROM source_document WHERE id IN ({placeholders})",
            document_ids,
        ).fetchall()

    if force:
        return conn.execute(
            "SELECT id, title, body_text FROM source_document ORDER BY imported_at LIMIT ?",
            (limit,),
        ).fetchall()

    return conn.execute(
        "SELECT d.id, d.title, d.body_text FROM source_document d "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM extraction_run r "
        "  WHERE r.source_document_id = d.id AND r.status = 'ok'"
        ") ORDER BY d.imported_at LIMIT ?",
        (limit,),
    ).fetchall()


def _claim_payload(verified: VerifiedClaim) -> dict:
    """一条通过校验的候选在库里的形状。

    待对齐队列与提纯运行记录用的是同一个形状——评测要拿后者当预测池，
    两处形状一旦分叉，「模型抽到的」与「人工看到的」就不是同一批东西了。
    """
    claim = verified.claim
    return {
        "subject_name": claim.subject_name,
        "subject_type": claim.subject_type.value,
        "polarity": claim.polarity.value,
        "facet": claim.facet.value,
        "text": claim.text,
        "quote": claim.quote,
        "char_start": verified.location.start,
        "char_end": verified.location.end,
        "quote_verdict": verified.location.verdict.value,
    }


def _accepted_payload(outcome) -> list[dict]:
    """一次提纯里全部通过校验的候选。评测的预测池就是它。"""
    return [_claim_payload(item) for item in outcome.accepted]


def _park_verified_claims(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    outcome,
    now: str,
) -> None:
    """把通过校验的候选按主体名归堆，挂到待对齐队列上。

    同一个主体名的一堆候选合成一条待办：人工处置「故宫」一次就够了，
    不应该为「故宫」的十三条结论开十三张待办。
    """
    by_subject: dict[tuple[str, str], list[VerifiedClaim]] = {}
    for verified in outcome.accepted:
        claim = verified.claim
        key = (claim.subject_name, claim.subject_type.value)
        by_subject.setdefault(key, []).append(verified)

    for (subject_name, _subject_type), items in by_subject.items():
        payload = [_claim_payload(item) for item in items]
        # 对齐已经做过的（人工处置过的）主体名不再重复开待办
        if _already_handled(conn, document_id, subject_name):
            continue

        conn.execute(
            "INSERT INTO alignment_task (id, mention_name, city_adcode, context_snippet, "
            "source_document_id, candidate_pois_json, extracted_claims_json, status, created_at) "
            "VALUES (?, ?, NULL, ?, ?, '[]', ?, 'pending', ?)",
            (
                new_id("at"),
                subject_name,
                f"来自《{document_id}》的 {len(payload)} 条结论",
                document_id,
                _json(payload),
                now,
            ),
        )


def _already_handled(conn: sqlite3.Connection, document_id: str, subject_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM alignment_task WHERE source_document_id = ? AND mention_name = ? LIMIT 1",
        (document_id, subject_name),
    ).fetchone()
    return row is not None


def _json(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


# ─── 对齐 ────────────────────────────────────────────────────────


def align_pending(
    *,
    conn: sqlite3.Connection | None = None,
    limit: int = DEFAULT_BATCH_LIMIT,
    search=None,
    fetch=None,
    on_progress: ProgressHook | None = None,
) -> AlignmentReport:
    """把待对齐队列里的提及逐条去高德找主体，找得到的落库成结论。

    一条待办里的候选结论全在同一个主体上（`_park_verified_claims` 归过堆），
    所以主体定下来之后整批一起落库——不会出现「同一批里一半入库一半没入」。

    `search` / `fetch` 可注入，便于离线测试；默认是真实高德接口。
    """
    owned = conn is None
    active = conn or connect()
    report = AlignmentReport()

    searcher = search or (lambda keywords, city: search_pois(keywords, city=city))
    fetcher = fetch or (lambda poi_id: fetch_poi(poi_id))
    notify = on_progress or _noop

    try:
        tasks = ks.pending_alignments(conn=active, limit=limit)
        for index, task in enumerate(tasks, 1):
            report.mentions += 1
            notify(
                Progress(
                    "align", f"对齐「{task.mention_name}」", index, len(tasks)
                )
            )
            city_adcode = task.city_adcode or _guess_city_adcode(active, task)
            if not city_adcode:
                report.unresolved_subjects += 1
                continue

            city_name = _city_name(active, city_adcode)
            if not city_name:
                report.unresolved_subjects += 1
                continue

            try:
                found = searcher(task.mention_name, city_name)
                roots = complete_lineage(found.candidates, fetch_ancestor=fetcher)
            except (PoiSearchError, RuntimeError) as exc:
                report.failed.append((task.mention_name, str(exc)))
                continue

            verdict = align(
                _mention(task.mention_name, city_name, city_adcode),
                found.candidates,
                lineage=roots,
            )
            _apply_alignment(
                active,
                task=task,
                verdict=verdict,
                roots=roots,
                city_adcode=city_adcode,
                now=_now(),
            )

            if verdict.outcome is AlignOutcome.ALIGNED:
                report.aligned += 1
                report.collapsed += 1 if verdict.collapsed_from_sub else 0
            else:
                report.pending += 1

        active.commit()
    finally:
        if owned:
            active.close()
    return report


def _mention(name: str, city_name: str, city_adcode: str):
    from lushu.domain.align import Mention

    return Mention(name=name, city_name=city_name, city_adcode=city_adcode)


def _guess_city_adcode(conn: sqlite3.Connection, task: ks.PendingAlignment) -> str | None:
    """找出这篇素材讲的是哪座城市。

    先在**素材标题与正文**里找，再退回结论正文：城市名通常出现在标题里
    （「北京旅行篇章」「西安三天」），而结论正文里可能一次都没提到城市——
    实测里「故宫现在只有午门能进」这句话里没有「北京」。

    做法很笨但可控：拿已经入库的城市名去正文里做子串匹配，取出现次数最多的
    那个。**猜不出来就不猜**——返回 None，那条提及进待人工，而不是挂到一座
    瞎猜的城市上（跨城错误是最危险的失败模式）。
    """
    pieces: list[str] = []
    if task.source_document_id:
        row = conn.execute(
            "SELECT title, body_text FROM source_document WHERE id = ?",
            (task.source_document_id,),
        ).fetchone()
        if row is not None:
            pieces.append(row["title"] or "")
            pieces.append(row["body_text"] or "")
    pieces.extend(str(item.get("text") or "") for item in task.claims)
    pieces.append(task.context_snippet or "")

    haystack = " ".join(pieces)
    if not haystack.strip():
        return None

    best: tuple[int, str] | None = None
    for row in conn.execute("SELECT adcode, name FROM city ORDER BY name").fetchall():
        hits = haystack.count(row["name"])
        if hits and (best is None or hits > best[0]):
            best = (hits, row["adcode"])
    return best[1] if best else None


def _city_name(conn: sqlite3.Connection, adcode: str) -> str | None:
    row = conn.execute("SELECT name FROM city WHERE adcode = ?", (adcode,)).fetchone()
    return row["name"] if row else None


def _apply_alignment(
    conn: sqlite3.Connection,
    *,
    task: ks.PendingAlignment,
    verdict,
    roots: CandidateSet,
    city_adcode: str,
    now: str,
) -> None:
    """按对齐结果处置一条待办：成功就落库，失败就更新候选供人工挑。"""
    if verdict.outcome is not AlignOutcome.ALIGNED or verdict.resolved is None:
        candidates = [
            {
                "poi_id": item.poi.poi_id,
                "name": item.poi.name,
                "typecode": item.poi.typecode,
                "address": item.poi.address,
                "adcode": item.poi.adcode,
                "name_score": item.name_score,
                "usable": item.usable,
                "reject_reason": item.reject_reason,
            }
            for item in verdict.candidates[:10]
        ]
        conn.execute(
            "UPDATE alignment_task SET candidate_pois_json = ?, context_snippet = ? WHERE id = ?",
            (_json(candidates), verdict.reason[:500], task.task_id),
        )
        return

    _materialize_claims(
        conn,
        task=task,
        poi=verdict.resolved,
        city_adcode=city_adcode,
        roots=roots,
        now=now,
    )
    conn.execute(
        "UPDATE alignment_task SET status = 'resolved', resolved_poi_id = ?, resolved_at = ?, "
        "candidate_pois_json = ? WHERE id = ?",
        (verdict.resolved.poi_id, now, _json([_candidate_dict(verdict.resolved)]), task.task_id),
    )


def _candidate_dict(poi) -> dict:
    return {
        "poi_id": poi.poi_id,
        "name": poi.name,
        "typecode": poi.typecode,
        "address": poi.address,
        "adcode": poi.adcode,
    }


def _materialize_claims(
    conn: sqlite3.Connection,
    *,
    task: ks.PendingAlignment,
    poi,
    city_adcode: str,
    roots: CandidateSet,
    now: str,
) -> list[str]:
    """把一条待办里的候选结论写成 claim + claim_evidence。

    第一件事是把 POI 写进 `poi` 表：`claim.poi_id` 有外键指向它，
    没有这一步落库时会直接撞约束——实测踩过。

    证据的原文片段要再校验一次（`document_contains`）：从提纯到落库之间
    可能隔了很久，素材正文如果被改过，引文就不能再声称「原文里有这句话」。
    """
    ks.save_candidate_poi(conn=conn, poi=poi, city_adcode=city_adcode, now=now)
    # 命中的子点也一并记住：它虽然不挂结论，但「午门属于故宫博物院」
    # 这层关系要留在库里，M5 排路线时会用到
    for member in roots.sub_pois_of(poi.poi_id):
        ks.save_candidate_poi(conn=conn, poi=member, city_adcode=city_adcode, now=now)

    created: list[str] = []
    group_id = _document_group(conn, task.source_document_id)

    for payload in task.claims:
        quote = str(payload.get("quote") or "")
        if task.source_document_id and not document_contains(
            quote, task.source_document_id, conn=conn
        ):
            continue

        claim_id = ks.find_claim_by_text(
            str(payload.get("text") or ""),
            conn=conn,
            subject_type=str(payload.get("subject_type") or SubjectType.POI.value),
            polarity=str(payload.get("polarity") or Polarity.AVOID.value),
            poi_id=poi.poi_id,
            city_adcode=None,
        )
        evidence = ClaimEvidence(
            source_document_id=task.source_document_id or "",
            quote=quote,
            source_group_id=group_id,
            char_start=_as_int(payload.get("char_start")),
            char_end=_as_int(payload.get("char_end")),
        )

        if claim_id is None:
            facet = _facet(payload.get("facet"))
            first_seen = date.today()
            claim_id = ks.save_claim(
                conn=conn,
                subject_type=str(payload.get("subject_type") or SubjectType.POI.value),
                subject_name=task.mention_name,
                poi_id=poi.poi_id,
                polarity=str(payload.get("polarity") or Polarity.AVOID.value),
                facet=facet.value,
                text=str(payload.get("text") or ""),
                evidence=[evidence],
                first_seen_at=now,
                verify_due_at=_verify_due(facet, first_seen),
            )
            created.append(claim_id)
        else:
            ks.append_evidence(conn=conn, claim_id=claim_id, evidence=evidence, created_at=now)

        ks.recount_independent_sources(claim_id, conn=conn)

    return created


def _as_int(value) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _facet(value) -> Facet:
    try:
        return Facet(str(value or ""))
    except ValueError:
        return Facet.OTHER


def _verify_due(facet: Facet, anchor: date) -> str | None:
    days = refresh_days_for(facet)
    if days is None:
        return None
    return (anchor + timedelta(days=days)).isoformat()


def _document_group(conn: sqlite3.Connection, document_id: str | None) -> str | None:
    if not document_id:
        return None
    row = conn.execute(
        "SELECT source_group_id FROM source_document WHERE id = ?", (document_id,)
    ).fetchone()
    return row["source_group_id"] if row else None


# ─── 第二层归组：结论同源 ────────────────────────────────────────


# 两篇的结论重合到这个比例才算同一来源。
#
# 必须比第一层的文字复制阈值严得多，理由在 ADR-0008 里：两位真的各写各的
# 游客写同一座城市的同一批景点，结论天然会重合（「故宫要提前预约」人人都写），
# 阈值放宽会把独立来源错并成一个，把高置信结论降级。
DEFAULT_CONCLUSION_OVERLAP = 0.80


def group_by_conclusions(
    *,
    conn: sqlite3.Connection | None = None,
    threshold: float = DEFAULT_CONCLUSION_OVERLAP,
) -> GroupReport:
    """按提纯结果把「逐句改写」的洗稿归到一起（ADR-0008 第二层）。

    第一层只看文字复制，实测认不出逐句改写的洗稿（LCS 覆盖 0.051，
    与异篇的 0.000 一样低）。所以补这一层：两篇提纯出的结论高度重合时，
    它们是同一个来源。

    只在**结论足够多**的篇之间比：一篇只抽出两条结论的素材，
    与另一篇偶然重合两条就会达到 100%，那不是同源，是样本太小。
    """
    owned = conn is None
    active = conn or connect()
    report = GroupReport()
    try:
        profiles = _conclusion_profiles(active)
        report.groups = len({group for _, group in _group_of(active) if group})

        for document_id, group_id, fingerprints in profiles:
            if group_id is not None or len(fingerprints) < MIN_CONCLUSIONS_TO_COMPARE:
                continue
            for other_id, other_group, other_fingerprints in profiles:
                if other_id == document_id or len(other_fingerprints) < MIN_CONCLUSIONS_TO_COMPARE:
                    continue
                report.compared += 1
                shared = len(fingerprints & other_fingerprints)
                ratio = shared / len(fingerprints)
                if ratio < threshold:
                    continue
                target = other_group or _ensure_group_for(active, other_id, now=_now())
                active.execute(
                    "UPDATE source_document SET source_group_id = ?, group_score = ? WHERE id = ?",
                    (target, round(ratio, 4), document_id),
                )
                report.merged += 1
                break
        active.commit()
        report.groups = len({row["source_group_id"] for row in _group_of(active) if row["source_group_id"]})
    finally:
        if owned:
            active.close()
    return report


# 少于这个结论数就不参与第二层比较：样本太小，偶然重合会变成 100%
MIN_CONCLUSIONS_TO_COMPARE = 4


def _conclusion_fingerprints(conn: sqlite3.Connection, document_id: str) -> set[str]:
    """一篇素材提纯出的结论指纹集合。

    指纹用「主体名 + 归一化正文」而不是正文本身：洗稿会把「故宫」写成
    「故宫博物院」、把「晚上8点」写成「晚上八点」，而主体与事实点是它们的
    共同骨架。归一化只抹空白与标点——**不做同义改写识别**，
    那需要语义模型，而当前这一步的阈值已经定得很严了。
    """
    rows = conn.execute(
        "SELECT subject_name, text FROM claim c "
        "JOIN claim_evidence e ON e.claim_id = c.id "
        "WHERE e.source_document_id = ?",
        (document_id,),
    ).fetchall()

    return {
        _conclusion_key(row["subject_name"] or "", row["text"] or "")
        for row in rows
        if (row["text"] or "").strip()
    }


_PUNCT = set(" \t\r\n\u3000，。、；：！？\u201c\u201d\u2018\u2019（）《》〈〉【】…—～·,.;:!?\"'()[]<>-")


def _conclusion_key(subject: str, text: str) -> str:
    return "".join(char for char in f"{subject}|{text}" if char not in _PUNCT)


def _conclusion_profiles(conn: sqlite3.Connection) -> list[tuple[str, str | None, set[str]]]:
    rows = conn.execute(
        "SELECT id, source_group_id FROM source_document ORDER BY imported_at"
    ).fetchall()
    return [
        (row["id"], row["source_group_id"], _conclusion_fingerprints(conn, row["id"]))
        for row in rows
    ]


def _group_of(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, source_group_id FROM source_document").fetchall()


def _ensure_group_for(conn: sqlite3.Connection, document_id: str, *, now: str) -> str:
    """给一篇还没有组的素材建组，并把它拉进去。"""
    row = conn.execute(
        "SELECT source_group_id, site, author FROM source_document WHERE id = ?", (document_id,)
    ).fetchone()
    if row is not None and row["source_group_id"]:
        return row["source_group_id"]

    group_id = new_id(SOURCE_GROUP)
    conn.execute(
        "INSERT INTO source_group (id, site, author, basis, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            group_id,
            row["site"] if row else None,
            row["author"] if row else None,
            "conclusion_overlap",
            now,
        ),
    )
    conn.execute(
        "UPDATE source_document SET source_group_id = ? WHERE id = ?", (group_id, document_id)
    )
    return group_id


# ─── 合并与重算 ──────────────────────────────────────────────────


def merge_claims(*, conn: sqlite3.Connection | None = None) -> MergeReport:
    """把同一个主体上的同义结论合成一条，并重算全部置信度。

    只处理**已经落库**的结论。「同义」的判据与 `find_claim_by_text` 一致：
    抹掉空白后正文相同，且极性相同。不做语义合并——那需要更严的判据，
    放宽会把不同人的不同经验错误地算成一条（ADR-0008 的同一个理由）。

    极性参与合并键：避坑与打卡分开建模（Q26），一句话换个态度就是另一条结论。
    """
    owned = conn is None
    active = conn or connect()
    report = MergeReport()
    try:
        rows = active.execute(
            "SELECT id, subject_type, subject_name, poi_id, city_adcode, polarity, text "
            "FROM claim ORDER BY first_seen_at"
        ).fetchall()

        seen: dict[tuple, str] = {}
        for row in rows:
            key = (
                row["subject_type"],
                row["polarity"],
                row["poi_id"] or "",
                row["city_adcode"] or "",
                _conclusion_key(row["subject_name"] or "", row["text"] or ""),
            )
            keeper = seen.get(key)
            if keeper is None:
                seen[key] = row["id"]
                continue
            if _move_evidence(active, from_claim=row["id"], to_claim=keeper):
                active.execute("DELETE FROM claim WHERE id = ?", (row["id"],))
                report.extended += 1
            else:
                # 证据全是同一篇素材的重复引用，合并后没有信息量增益，
                # 但也不该留两条一模一样的结论
                active.execute("DELETE FROM claim WHERE id = ?", (row["id"],))
                report.extended += 1

        report.created = len(seen)

        for claim_id in seen.values():
            count, confidence = ks.recount_independent_sources(claim_id, conn=active)
            if confidence.value == "high":
                report.high_confidence += 1
            else:
                report.single_source += 1

        active.commit()
    finally:
        if owned:
            active.close()
    return report


def _move_evidence(conn: sqlite3.Connection, *, from_claim: str, to_claim: str) -> bool:
    """把一条结论的证据搬给另一条。已经有同一篇素材的证据时跳过。

    `claim_evidence.claim_id` 上有 `ON DELETE CASCADE`，所以必须先搬再删，
    否则证据会跟着被删掉——那等于把溯源弄丢了。
    """
    moved = False
    rows = conn.execute(
        "SELECT id, source_document_id FROM claim_evidence WHERE claim_id = ?", (from_claim,)
    ).fetchall()
    for row in rows:
        exists = conn.execute(
            "SELECT 1 FROM claim_evidence WHERE claim_id = ? AND source_document_id = ? LIMIT 1",
            (to_claim, row["source_document_id"]),
        ).fetchone()
        if exists is not None:
            conn.execute("DELETE FROM claim_evidence WHERE id = ?", (row["id"],))
            continue
        conn.execute(
            "UPDATE claim_evidence SET claim_id = ? WHERE id = ?", (to_claim, row["id"])
        )
        moved = True
    return moved


def resolve_alignment_task(
    *,
    conn: sqlite3.Connection,
    task_id: str,
    poi_id: str | None,
    discard: bool,
    fetch=None,
    now: str | None = None,
) -> list[str]:
    """人工处置一条待对齐：挂到指定 POI，或判定「这不是一个地点」。

    人工确认与自动对齐走的是同一段落库代码（`_materialize_claims`），
    差别只在主体从哪来：自动那一步来自高德搜索，这一步来自人。

    `fetch` 可注入，便于离线测试。返回新建的结论 id（可能为空——
    候选的引文若已回不到原文，会被如实丢掉而不是谎报成功）。
    """

    timestamp = now or _now()
    row = conn.execute("SELECT * FROM alignment_task WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise PipelineError(f"没有这条待办：{task_id}")

    if discard:
        ks.resolve_alignment(
            conn=conn,
            task_id=task_id,
            resolved_poi_id=None,
            status=ks.ALIGN_STATUS_DISCARDED,
            resolved_at=timestamp,
        )
        return []

    if not poi_id:
        raise PipelineError("必须给出 poi_id，或显式 discard 表示这不是一个地点")

    poi = _poi_for(conn, poi_id, fetch=fetch)
    if poi is None:
        raise PipelineError(f"这个 POI 取不到：{poi_id}")

    city_adcode = ks.resolve_city_adcode(row["city_adcode"], conn=conn) or ks.resolve_city_adcode(
        poi.adcode, conn=conn
    )
    if not city_adcode:
        raise PipelineError(
            f"「{poi.name}」所在的城市（adcode {poi.adcode or '未知'}）还没入库，"
            "无法确定该挂到哪座城市"
        )

    task = ks.PendingAlignment(
        task_id=task_id,
        mention_name=row["mention_name"],
        city_adcode=city_adcode,
        context_snippet=row["context_snippet"],
        source_document_id=row["source_document_id"],
        candidates=(),
        claims=ks.dict_items(row["extracted_claims_json"]),
        status=row["status"],
        created_at=row["created_at"],
    )

    created = _materialize_claims(
        conn,
        task=task,
        poi=poi,
        city_adcode=city_adcode,
        roots=_lineage_from_db(conn, poi),
        now=timestamp,
    )
    ks.resolve_alignment(
        conn=conn,
        task_id=task_id,
        resolved_poi_id=poi.poi_id,
        resolved_at=timestamp,
    )
    return created


def _poi_for(conn: sqlite3.Connection, poi_id: str, *, fetch=None):
    """先看库里有没有，没有就去高德取一次。

    人对齐时指名的那个 POI 可能还没落过库——它在候选列表里展示过，
    但只有被选中才会写进来。
    """
    row = conn.execute(
        "SELECT amap_poi_id, name, typecode, type, adcode, address, tel, "
        "       parent_poi_id, lat_gcj02, lng_gcj02, rating, open_time, photo_url, raw_json "
        "FROM poi WHERE amap_poi_id = ?",
        (poi_id,),
    ).fetchone()
    if row is not None:
        return CandidatePoi(
            poi_id=row["amap_poi_id"],
            name=row["name"],
            typecode=row["typecode"],
            type_name=row["type"],
            adcode=row["adcode"],
            address=row["address"],
            tel=row["tel"],
            parent_id=row["parent_poi_id"],
            lng_gcj02=row["lng_gcj02"],
            lat_gcj02=row["lat_gcj02"],
            rating=row["rating"],
            open_time=row["open_time"],
            photo_url=row["photo_url"],
            raw_json=row["raw_json"],
        )

    fetcher = fetch or (lambda target: fetch_poi(target))
    try:
        return fetcher(poi_id)
    except PoiSearchError:
        return None


def _lineage_from_db(conn: sqlite3.Connection, poi) -> CandidateSet:
    """从库里把本体关系拼出来，供 `_materialize_claims` 记住子点。"""
    lineage: dict[str, str | None] = {poi.poi_id: (poi.parent_id or "").strip() or None}
    for row in conn.execute("SELECT amap_poi_id, parent_poi_id FROM poi").fetchall():
        lineage.setdefault(row["amap_poi_id"], (row["parent_poi_id"] or "").strip() or None)
    return CandidateSet(candidates=(poi,), lineage=lineage)


def pipeline_stats(*, conn: sqlite3.Connection | None = None) -> dict[str, object]:
    """链路各环节的规模，供 `ls pipeline stats` 与数据工作台概览使用。"""
    owned = conn is None
    active = conn or connect()
    try:
        documents = active.execute("SELECT COUNT(*) AS n FROM source_document").fetchone()["n"]
        groups = active.execute("SELECT COUNT(*) AS n FROM source_group").fetchone()["n"]
        claims = active.execute("SELECT COUNT(*) AS n FROM claim").fetchone()["n"]
        high = active.execute(
            "SELECT COUNT(*) AS n FROM claim WHERE confidence = 'high'"
        ).fetchone()["n"]
        evidence = active.execute("SELECT COUNT(*) AS n FROM claim_evidence").fetchone()["n"]
        run_stats = ks.extraction_stats(conn=active)
        align_stats = ks.alignment_stats(conn=active)
    finally:
        if owned:
            active.close()

    return {
        "documents": documents,
        "groups": groups,
        "claims": claims,
        "high_confidence": high,
        "evidence": evidence,
        **{f"extract_{key}": value for key, value in run_stats.items()},
        **{f"align_{key}": value for key, value in align_stats.items()},
    }
