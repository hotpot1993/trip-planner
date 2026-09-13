"""知识域的读写：结论、证据、待对齐、提纯运行的落库。

读写的形状由三条设计约束定死：

1. **无溯源的结论不得入库**（DESIGN 4.5）。所以 `save_claim` 要求至少一条证据，
   证据要带原文片段，而片段的可定位性由调用方先用
   `lushu/domain/extraction.py` 校验过。
2. **置信度按独立来源组计数，绝不按素材篇数**（Q34）。转载在同一组里，
   计一次。`recount_independent_sources` 是这件事的唯一执行点。
3. **对不上的提及进待对齐队列**，不硬塞一个近似结果（Q19）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date

from lushu.domain.align import AlignResult
from lushu.domain.knowledge import ClaimEvidence, Confidence, confidence_for
from lushu.store.connection import connect
from lushu.store.ids import CLAIM, EVIDENCE, new_id

# 待对齐的三类触发原因，与 domain.align.AlignOutcome 的取值对应
ALIGN_STATUS_PENDING = "pending"
ALIGN_STATUS_RESOLVED = "resolved"
ALIGN_STATUS_DISCARDED = "discarded"


@dataclass(frozen=True)
class ClaimRow:
    """库里的一条结论。"""

    claim_id: str
    subject_type: str
    subject_name: str | None
    poi_id: str | None
    city_adcode: str | None
    polarity: str
    facet: str
    text: str
    confidence: Confidence
    independent_source_count: int
    status: str
    first_seen_at: str
    verify_due_at: str | None
    evidence_count: int


@dataclass(frozen=True)
class PendingAlignment:
    """待对齐队列里的一条。

    `claims` 是**尚未落库的候选结论全文**。提纯与对齐分两步跑，
    中间那个状态里结论只存在于内存，人工处置待办时看不到「这条说的是什么」，
    所以记录时把载荷一起存了下来。
    """

    task_id: str
    mention_name: str
    city_adcode: str | None
    context_snippet: str | None
    source_document_id: str | None
    candidates: tuple[dict, ...]
    claims: tuple[dict, ...]
    status: str
    created_at: str


@dataclass(frozen=True)
class ExtractionRunRow:
    """一次提纯运行。`dropped_count` 是「模型编了引文」的直接证据。"""

    run_id: str
    source_document_id: str
    prompt_version: str
    model: str
    candidate_count: int
    accepted_count: int
    dropped_count: int
    duration_ms: int | None
    status: str
    error: str | None
    created_at: str

    @property
    def drop_ratio(self) -> float:
        if not self.candidate_count:
            return 0.0
        return self.dropped_count / self.candidate_count


# ─── 提纯运行 ────────────────────────────────────────────────────


def record_extraction_run(
    *,
    conn: sqlite3.Connection,
    source_document_id: str,
    prompt_version: str,
    model: str,
    candidate_count: int,
    accepted_count: int,
    dropped_count: int,
    input_chars: int | None = None,
    output_chars: int | None = None,
    duration_ms: int | None = None,
    accepted: list[dict] | None = None,
    status: str = "ok",
    error: str | None = None,
    created_at: str,
) -> str:
    """记下一次提纯运行。

    这个表的用处是回答「抽了几条、丢了几条、为什么丢」。只报「抽出了几条」
    区分不出「这一篇本来就没内容」与「模型编了引文」——而后者正是设计里
    担心的「无法区分提纯很准与只看到了准的那几条」（第十一节）。

    `accepted` 是引文校验通过的候选全文。留着它是为了让**评测有一个稳定的
    预测池**：落库的 claim 只是「抽到且对齐上了」的那些，拿它当预测池会把
    对齐失败算成抽取错误（迁移 9 的注释里写了为什么另外两个地方都不能用）。
    """
    run_id = new_id("er")
    conn.execute(
        "INSERT INTO extraction_run (id, source_document_id, prompt_version, model, "
        "candidate_count, accepted_count, dropped_count, input_chars, output_chars, "
        "duration_ms, accepted_json, status, error, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            source_document_id,
            prompt_version,
            model,
            candidate_count,
            accepted_count,
            dropped_count,
            input_chars,
            output_chars,
            duration_ms,
            json.dumps(accepted, ensure_ascii=False) if accepted is not None else None,
            status,
            error,
            created_at,
        ),
    )
    return run_id


def latest_extraction_run(
    document_id: str, *, conn: sqlite3.Connection | None = None
) -> ExtractionRunRow | None:
    owned = conn is None
    active = conn or connect()
    try:
        row = active.execute(
            "SELECT * FROM extraction_run WHERE source_document_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (document_id,),
        ).fetchone()
    finally:
        if owned:
            active.close()

    if row is None:
        return None
    return ExtractionRunRow(
        run_id=row["id"],
        source_document_id=row["source_document_id"],
        prompt_version=row["prompt_version"],
        model=row["model"],
        candidate_count=row["candidate_count"],
        accepted_count=row["accepted_count"],
        dropped_count=row["dropped_count"],
        duration_ms=row["duration_ms"],
        status=row["status"],
        error=row["error"],
        created_at=row["created_at"],
    )


def extraction_stats(*, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """整体提纯统计，用于 `ls extract stats` 与数据工作台的概览。"""
    owned = conn is None
    active = conn or connect()
    try:
        row = active.execute(
            "SELECT COUNT(*) AS runs, "
            "       COALESCE(SUM(candidate_count), 0) AS candidates, "
            "       COALESCE(SUM(accepted_count), 0) AS accepted, "
            "       COALESCE(SUM(dropped_count), 0) AS dropped, "
            "       COUNT(DISTINCT source_document_id) AS documents "
            "FROM extraction_run WHERE status = 'ok'"
        ).fetchone()
    finally:
        if owned:
            active.close()
    return {
        "runs": row["runs"],
        "documents": row["documents"],
        "candidates": row["candidates"],
        "accepted": row["accepted"],
        "dropped": row["dropped"],
    }


# ─── 实体落库 ────────────────────────────────────────────────────


def resolve_city_adcode(
    adcode: str | None, *, conn: sqlite3.Connection
) -> str | None:
    """把任意层级的行政区划代码归到库里已有的那座城市。

    高德给 POI 的是**区县**级 adcode（故宫博物院是 `110101` 东城区），
    而 `city` 表存的是**市**一级（北京是 `110100`）。直接拿 POI 的 adcode
    去写 `poi.city_adcode` 会撞外键——实测踩过。

    先精确匹配，再按前四位匹配市一级。都找不到就返回 None，
    让调用方去处理（宁可不写，也不要编一个不存在的城市）。
    """
    code = (adcode or "").strip()
    if not code:
        return None

    row = conn.execute("SELECT adcode FROM city WHERE adcode = ?", (code,)).fetchone()
    if row is not None:
        return row["adcode"]

    row = conn.execute(
        "SELECT adcode FROM city WHERE substr(adcode, 1, 4) = ? ORDER BY adcode LIMIT 1",
        (code[:4],),
    ).fetchone()
    return row["adcode"] if row else None


def save_candidate_poi(
    *,
    conn: sqlite3.Connection,
    poi,  # CandidatePoi，避免与 domain 循环导入，注解留在文档里
    city_adcode: str,
    now: str,
) -> None:
    """把对齐认定的 POI 写进 `poi` 表。

    **这一步不能省**：`claim.poi_id`、`city_stay` 与 `day_item` 都有外键指向
    `poi(amap_poi_id)`，没写进去的话落库时会直接撞外键约束。
    实测过——对齐逻辑本身跑通了，结论却写不进去。

    写全字段是因为 `typecode` 与 `parent_poi_id` 正是 M3 用来判类型与层级的
    那两列（`lushu/domain/poi.py`）。M1/M2 落库时它们全是空的，
    所以这里用 UPSERT 顺手补齐。
    """
    conn.execute(
        "INSERT INTO poi (amap_poi_id, name, city_adcode, adcode, address, tel, type, typecode, "
        "parent_poi_id, lat_gcj02, lng_gcj02, rating, open_time, photo_url, raw_json, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(amap_poi_id) DO UPDATE SET "
        "  name = excluded.name,"
        "  city_adcode = excluded.city_adcode,"
        "  adcode = COALESCE(excluded.adcode, poi.adcode),"
        "  address = COALESCE(excluded.address, poi.address),"
        "  tel = COALESCE(excluded.tel, poi.tel),"
        "  type = COALESCE(excluded.type, poi.type),"
        "  typecode = COALESCE(excluded.typecode, poi.typecode),"
        "  parent_poi_id = COALESCE(excluded.parent_poi_id, poi.parent_poi_id),"
        "  lat_gcj02 = excluded.lat_gcj02,"
        "  lng_gcj02 = excluded.lng_gcj02,"
        "  rating = COALESCE(excluded.rating, poi.rating),"
        "  open_time = COALESCE(excluded.open_time, poi.open_time),"
        "  photo_url = COALESCE(excluded.photo_url, poi.photo_url),"
        "  raw_json = COALESCE(excluded.raw_json, poi.raw_json),"
        "  fetched_at = excluded.fetched_at",
        (
            poi.poi_id,
            poi.name,
            city_adcode,
            poi.adcode,
            poi.address,
            poi.tel,
            poi.type_name,
            poi.typecode,
            poi.parent_id,
            poi.lat_gcj02,
            poi.lng_gcj02,
            poi.rating,
            poi.open_time,
            poi.photo_url,
            poi.raw_json,
            now,
        ),
    )


# ─── 结论与证据 ──────────────────────────────────────────────────


def save_claim(
    *,
    conn: sqlite3.Connection,
    subject_type: str,
    subject_name: str,
    polarity: str,
    facet: str,
    text: str,
    evidence: list[ClaimEvidence],
    first_seen_at: str,
    verify_due_at: str | None,
    poi_id: str | None = None,
    poi_a_id: str | None = None,
    poi_b_id: str | None = None,
    city_adcode: str | None = None,
) -> str:
    """写一条结论及其全部证据。

    独立来源数在这里只按**去重后的来源组**算初值；后续别的素材补充证据时
    由 `recount_independent_sources` 重算。
    """
    if not evidence:
        raise ValueError("无溯源的结论不得入库（DESIGN 4.5）")

    claim_id = new_id(CLAIM)
    groups = {item.source_group_id or item.source_document_id for item in evidence}
    count = len(groups)

    conn.execute(
        "INSERT INTO claim (id, subject_type, subject_name, poi_id, poi_a_id, poi_b_id, "
        "city_adcode, polarity, facet, text, confidence, independent_source_count, "
        "status, first_seen_at, last_verified_at, verify_due_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, ?)",
        (
            claim_id,
            subject_type,
            subject_name,
            poi_id,
            poi_a_id,
            poi_b_id,
            city_adcode,
            polarity,
            facet,
            text,
            str(confidence_for(count)),
            count,
            first_seen_at,
            verify_due_at,
        ),
    )

    for item in evidence:
        conn.execute(
            "INSERT INTO claim_evidence (id, claim_id, source_document_id, source_group_id, "
            "quote, char_start, char_end, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id(EVIDENCE),
                claim_id,
                item.source_document_id,
                item.source_group_id,
                item.quote,
                item.char_start,
                item.char_end,
                first_seen_at,
            ),
        )
    return claim_id


def append_evidence(
    *,
    conn: sqlite3.Connection,
    claim_id: str,
    evidence: ClaimEvidence,
    created_at: str,
) -> bool:
    """给已有结论补一条证据。已经引用过同一篇素材时返回 False。

    同一篇素材对同一条结论只算一条证据——重复追加会让证据列表长得像
    有很多人支持，而实际上只有一个人说过。
    """
    exists = conn.execute(
        "SELECT 1 FROM claim_evidence WHERE claim_id = ? AND source_document_id = ? LIMIT 1",
        (claim_id, evidence.source_document_id),
    ).fetchone()
    if exists is not None:
        return False

    conn.execute(
        "INSERT INTO claim_evidence (id, claim_id, source_document_id, source_group_id, "
        "quote, char_start, char_end, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id(EVIDENCE),
            claim_id,
            evidence.source_document_id,
            evidence.source_group_id,
            evidence.quote,
            evidence.char_start,
            evidence.char_end,
            created_at,
        ),
    )
    return True


def recount_independent_sources(
    claim_id: str, *, conn: sqlite3.Connection
) -> tuple[int, Confidence]:
    """按**独立来源组**重算置信度。这是 Q34 的唯一执行点。

    没有来源组的素材按自身 id 计——那是「本来就是独立的一篇」。
    转载在同一组里，无论被同步到多少个站点都只计一次。
    """
    row = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(source_group_id, source_document_id)) AS n "
        "FROM claim_evidence WHERE claim_id = ?",
        (claim_id,),
    ).fetchone()
    count = int(row["n"])
    confidence = confidence_for(count)

    conn.execute(
        "UPDATE claim SET independent_source_count = ?, confidence = ? WHERE id = ?",
        (count, str(confidence), claim_id),
    )
    return count, confidence


def find_claim_by_text(
    text: str,
    *,
    conn: sqlite3.Connection,
    subject_type: str,
    polarity: str,
    poi_id: str | None,
    city_adcode: str | None,
) -> str | None:
    """在同一个主体、同一个极性下找正文相同的已有结论。

    极性要参与匹配：避坑与打卡是分开建模、分开呈现的两类（Q26），
    「故宫值得去」与「故宫不必去」是两条不同的结论，不能因为都提到故宫
    就被合成一条。

    归一化只做「抹掉所有空白」，与导入的指纹同一个思路：中文攻略里同一句话
    的断行位置与空格用法不固定，而这不该让同一条结论变成两条。
    刻意**不做**同义改写识别——那属于「结论同源」的范畴，需要更严的判据
    （ADR-0008 说得很清楚：放宽会把独立来源错并，把高置信结论降级）。
    """
    squashed = _squash(text)
    if not squashed:
        return None

    rows = conn.execute(
        "SELECT id, text FROM claim WHERE subject_type = ? AND polarity = ? "
        "AND COALESCE(poi_id, '') = COALESCE(?, '') "
        "AND COALESCE(city_adcode, '') = COALESCE(?, '')",
        (subject_type, polarity, poi_id, city_adcode),
    ).fetchall()

    for row in rows:
        if _squash(row["text"] or "") == squashed:
            return row["id"]
    return None


def _squash(text: str) -> str:
    return "".join(char for char in text if not char.isspace())


def list_claims(
    *,
    conn: sqlite3.Connection | None = None,
    poi_id: str | None = None,
    city_adcode: str | None = None,
    confidence: str | None = None,
    limit: int = 200,
) -> list[ClaimRow]:
    """列出结论。供数据工作台与 M5 的候选池使用。"""
    owned = conn is None
    active = conn or connect()
    try:
        clauses: list[str] = []
        params: list[object] = []
        if poi_id:
            clauses.append("c.poi_id = ?")
            params.append(poi_id)
        if city_adcode:
            clauses.append("c.city_adcode = ?")
            params.append(city_adcode)
        if confidence:
            clauses.append("c.confidence = ?")
            params.append(confidence)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        rows = active.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM claim_evidence e WHERE e.claim_id = c.id) "
            "       AS evidence_count "
            f"FROM claim c {where} "
            "ORDER BY c.independent_source_count DESC, c.first_seen_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    finally:
        if owned:
            active.close()

    return [
        ClaimRow(
            claim_id=row["id"],
            subject_type=row["subject_type"],
            subject_name=row["subject_name"],
            poi_id=row["poi_id"],
            city_adcode=row["city_adcode"],
            polarity=row["polarity"],
            facet=row["facet"],
            text=row["text"],
            confidence=Confidence(row["confidence"]),
            independent_source_count=row["independent_source_count"],
            status=row["status"],
            first_seen_at=row["first_seen_at"],
            verify_due_at=row["verify_due_at"],
            evidence_count=row["evidence_count"],
        )
        for row in rows
    ]


def claims_for_poi(poi_id: str, *, conn: sqlite3.Connection | None = None) -> list[ClaimRow]:
    """某个景点上的全部结论。M5 的候选池与卡片标注都从这里取。"""
    return list_claims(poi_id=poi_id, conn=conn)


def claims_for_city(
    city_adcode: str, *, conn: sqlite3.Connection | None = None
) -> list[ClaimRow]:
    """某座城市的全部结论，指向该城 POI 的那些也算。"""
    owned = conn is None
    active = conn or connect()
    try:
        rows = active.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM claim_evidence e WHERE e.claim_id = c.id) "
            "       AS evidence_count "
            "FROM claim c LEFT JOIN poi p ON p.amap_poi_id = c.poi_id "
            "WHERE c.city_adcode = ? OR p.city_adcode = ? "
            "ORDER BY c.independent_source_count DESC",
            (city_adcode, city_adcode),
        ).fetchall()
    finally:
        if owned:
            active.close()

    return [
        ClaimRow(
            claim_id=row["id"],
            subject_type=row["subject_type"],
            subject_name=row["subject_name"],
            poi_id=row["poi_id"],
            city_adcode=row["city_adcode"],
            polarity=row["polarity"],
            facet=row["facet"],
            text=row["text"],
            confidence=Confidence(row["confidence"]),
            independent_source_count=row["independent_source_count"],
            status=row["status"],
            first_seen_at=row["first_seen_at"],
            verify_due_at=row["verify_due_at"],
            evidence_count=row["evidence_count"],
        )
        for row in rows
    ]


def evidence_for_claim(
    claim_id: str, *, conn: sqlite3.Connection | None = None
) -> list[dict]:
    """一条结论的全部证据。进路书的就是 quote 这一段。"""
    owned = conn is None
    active = conn or connect()
    try:
        rows = active.execute(
            "SELECT e.id, e.source_document_id, e.source_group_id, e.quote, e.char_start, "
            "       e.char_end, d.site, d.url, d.title "
            "FROM claim_evidence e JOIN source_document d ON d.id = e.source_document_id "
            "WHERE e.claim_id = ? ORDER BY e.created_at",
            (claim_id,),
        ).fetchall()
    finally:
        if owned:
            active.close()
    return [dict(row) for row in rows]


# ─── 待对齐 ──────────────────────────────────────────────────────


def record_alignment(
    *,
    conn: sqlite3.Connection,
    mention_name: str,
    city_adcode: str | None,
    context_snippet: str | None,
    source_document_id: str | None,
    result: AlignResult,
    created_at: str,
    extracted_claims: list[dict] | None = None,
) -> str | None:
    """记下一次对齐的结果。

    对齐成功时不写待办，只返回 None 让调用方把结论挂上去；
    对不上时写一条 `alignment_task`，带上候选供人工挑（Q19）。

    `extracted_claims` 是**尚未落库的候选结论全文**。提纯与对齐分两步跑，
    中间那个状态里结论只存在于内存，人工去处置待办时看不到「这条说的是什么」，
    所以要把载荷一起存下来（`lushu/services/pipeline.py` 靠它续上第二步）。
    """
    if result.outcome.value == "aligned":
        return None

    # 只留前十个候选：一条噪声查询可能返回几十个不相关的 POI，
    # 全塞进一行的 JSON 里既没用又会让工作台页面变慢
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
        for item in result.candidates[:10]
    ]

    task_id = new_id("at")
    conn.execute(
        "INSERT INTO alignment_task (id, mention_name, city_adcode, context_snippet, "
        "source_document_id, candidate_pois_json, extracted_claims_json, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            mention_name,
            city_adcode,
            (context_snippet or result.reason)[:500],
            source_document_id,
            json.dumps(candidates, ensure_ascii=False),
            json.dumps(extracted_claims or [], ensure_ascii=False),
            ALIGN_STATUS_PENDING,
            created_at,
        ),
    )
    return task_id


def pending_alignments(
    *, conn: sqlite3.Connection | None = None, limit: int = 200
) -> list[PendingAlignment]:
    """待对齐队列。数据工作台的主要工作面。"""
    owned = conn is None
    active = conn or connect()
    try:
        rows = active.execute(
            "SELECT * FROM alignment_task WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (ALIGN_STATUS_PENDING, limit),
        ).fetchall()
    finally:
        if owned:
            active.close()

    return [
        PendingAlignment(
            task_id=row["id"],
            mention_name=row["mention_name"],
            city_adcode=row["city_adcode"],
            context_snippet=row["context_snippet"],
            source_document_id=row["source_document_id"],
            candidates=dict_items(row["candidate_pois_json"]),
            claims=dict_items(row["extracted_claims_json"]),
            status=row["status"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


def dict_items(raw: str | None) -> tuple[dict, ...]:
    """把一列存着 JSON 数组的文本读成字典元组。

    对库里的 JSON **不能盲信**：早先版本的载荷形状与现在不同
    （候选曾是字符串列表），坏数据不该让整个工作台页面打不开。
    读不出来或形状不对的条目直接跳过，页面继续能显示其余内容。

    公开（不带下划线）是因为接口层与管线都要读这两列。
    """
    if not raw:
        return ()
    try:
        loaded = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(loaded, list):
        return ()
    return tuple(item for item in loaded if isinstance(item, dict))


def resolve_alignment(
    *,
    conn: sqlite3.Connection,
    task_id: str,
    resolved_poi_id: str | None,
    status: str = ALIGN_STATUS_RESOLVED,
    resolved_at: str | None = None,
) -> None:
    """人工处置一条待对齐。

    `status='discarded'` 表示「这条提及本来就不是一个地点」——
    那个结论按设计确实挂不上，丢掉它比挂到一个近似 POI 上诚实。
    """
    if status not in (ALIGN_STATUS_RESOLVED, ALIGN_STATUS_DISCARDED):
        raise ValueError(f"处置结果只能是 resolved 或 discarded，收到 {status}")

    conn.execute(
        "UPDATE alignment_task SET status = ?, resolved_poi_id = ?, resolved_at = ? WHERE id = ?",
        (
            status,
            resolved_poi_id if status == ALIGN_STATUS_RESOLVED else None,
            resolved_at or date.today().isoformat(),
            task_id,
        ),
    )


def alignment_stats(*, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    owned = conn is None
    active = conn or connect()
    try:
        rows = active.execute(
            "SELECT status, COUNT(*) AS n FROM alignment_task GROUP BY status"
        ).fetchall()
    finally:
        if owned:
            active.close()
    counts = {row["status"]: row["n"] for row in rows}
    return {
        "pending": counts.get(ALIGN_STATUS_PENDING, 0),
        "resolved": counts.get(ALIGN_STATUS_RESOLVED, 0),
        "discarded": counts.get(ALIGN_STATUS_DISCARDED, 0),
    }


@dataclass(frozen=True)
class PoiRow:
    """库里已有的一个 POI。"""

    poi_id: str
    name: str
    city_name: str | None
    parent_id: str | None
    parent_name: str | None
    typecode: str | None
    address: str | None

    @property
    def is_root(self) -> bool:
        """本体（`parent` 为空）还是子点。标注时该选本体（ADR-0009）。"""
        return not self.parent_id

    @property
    def label(self) -> str:
        """给人看的一行。子点要带上它的本体，否则选不出该选哪个。"""
        if self.parent_name:
            return f"{self.name}（属于 {self.parent_name}）"
        return self.name


def search_local_pois(
    query: str = "", *, city_adcode: str | None = None, limit: int = 20,
    conn: sqlite3.Connection | None = None,
) -> list[PoiRow]:
    """在**库里已有的** POI 里按名称找。

    标注金标准时要填「这条提及指的是哪个景点」（`expected_poi_id`），
    而那个字段应当填**管线能真正产出的实体**——也就是 `poi` 表里有的。
    去问一次高德会给出更多候选，但也会给出管线当下对不上的候选，
    这样的金标准会把「对齐准确率」变成一个够不着的指标。

    只做子串匹配，不排序打分：这里是人眼在挑，不是在机器对齐。
    本体排在子点前面——ADR-0009 要求结论挂本体。
    """
    owned = conn is None
    active = conn or connect()
    try:
        clauses = []
        params: list[object] = []
        if query.strip():
            clauses.append("p.name LIKE ?")
            params.append(f"%{query.strip()}%")
        if city_adcode:
            clauses.append("p.city_adcode = ?")
            params.append(city_adcode)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = active.execute(
            "SELECT p.amap_poi_id, p.name, c.name AS city_name, p.parent_poi_id, "
            "  r.name AS parent_name, p.typecode, p.address "
            "FROM poi p LEFT JOIN city c ON c.adcode = p.city_adcode "
            "LEFT JOIN poi r ON r.amap_poi_id = p.parent_poi_id "
            f"{where} "
            "ORDER BY (p.parent_poi_id IS NOT NULL), p.name LIMIT ?",
            params,
        ).fetchall()
    finally:
        if owned:
            active.close()

    return [
        PoiRow(
            poi_id=row["amap_poi_id"],
            name=row["name"],
            city_name=row["city_name"],
            parent_id=row["parent_poi_id"],
            parent_name=row["parent_name"],
            typecode=row["typecode"],
            address=row["address"],
        )
        for row in rows
    ]
