"""金标准集的读写。

金标准集是「人工读原文写下的全部真实结论」，它是**唯一**能回答
「提纯质量到底如何」的东西（设计第十一节的风险表点名了这一条）。
这个模块只管标注本身，评测在 `lushu/services/evaluation.py`。

两条贯穿本模块的规矩：

1. **标注必须落回原文。** 加一条标注时，引文要能在正文里定位到，
   偏移由程序算，人不用填。这不是为难标注者——没有这条，标注会慢慢
   退化成「凭印象写结论」，而那种金标准测不出任何东西。给模型定的门槛
   与给人的门槛是同一道（同一套 `locate_quote`、同一个最短长度）。
2. **「标注完了但一条结论都没有」必须能表达。** 一篇读完发现全是废话的
   素材，零标注行恰恰是最重要的标注结果——它进召回率的分母。所以
   「标完了」记在 `gold_set` 表上，而不是靠 `gold_label` 有没有行反推。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from lushu.domain.extraction import MIN_QUOTE_CHARS, QuoteVerdict, locate_quote
from lushu.store.connection import connect
from lushu.store.ids import GOLD_LABEL, new_id

# 标注的引文允许「宽松命中」：全角半角、空白差异不拦。
# 人手抄原文时这些差异是常态，而位置照样算得出来，卡它没有意义。
_ACCEPTED_VERDICTS = (QuoteVerdict.EXACT, QuoteVerdict.LOOSE)


class GoldError(ValueError):
    """标注不合法。消息是给标注者看的中文。"""


@dataclass(frozen=True)
class GoldLabelRow:
    label_id: str
    source_document_id: str
    quote: str
    polarity: str
    subject_type: str
    subject_name: str | None
    expected_poi_id: str | None
    facet: str | None
    char_start: int | None
    char_end: int | None
    note: str | None
    created_at: str
    verdict: str


@dataclass(frozen=True)
class GoldDocument:
    """一篇素材在金标准集里的状态。"""

    document_id: str
    title: str | None
    site: str
    chars: int
    prediction_count: int  # 模型抽出的候选条数，也就是这篇的预测池大小
    labeled: int  # 已标注条数
    in_gold_set: bool  # 是否已标记「标注完成」
    model_output_seen: bool
    annotated_at: str | None


def add_label(
    *,
    conn: sqlite3.Connection,
    document_id: str,
    quote: str,
    polarity: str,
    subject_name: str | None = None,
    subject_type: str = "poi",
    expected_poi_id: str | None = None,
    facet: str | None = None,
    note: str | None = None,
    created_at: str,
) -> GoldLabelRow:
    """往金标准集里加一条。

    引文的位置在这里算出来，不让人填。找不到就报错——**这条限制是刻意的**：
    允许「找不到引文的标注」等于允许金标准里混进凭印象写的条目，
    而金标准一旦不可信，后面所有指标都白算。
    """
    row = conn.execute(
        "SELECT body_text FROM source_document WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise GoldError(f"没有这篇素材：{document_id}")

    text = quote.strip()
    if len(text) < MIN_QUOTE_CHARS:
        raise GoldError(f"引文太短（至少 {MIN_QUOTE_CHARS} 字），短于它的片段定位不可靠")

    location = locate_quote(text, row["body_text"])
    if location.verdict not in _ACCEPTED_VERDICTS:
        raise GoldError("引文在原文里找不到。请从正文里原样复制一段，不要改写")

    if polarity not in ("avoid", "highlight"):
        raise GoldError("极性只能是 avoid（避坑）或 highlight（打卡）")
    if subject_type not in ("poi", "route", "city"):
        raise GoldError("主体类型只能是 poi / route / city")

    label_id = new_id(GOLD_LABEL)
    conn.execute(
        "INSERT INTO gold_label (id, source_document_id, quote, char_start, char_end, "
        "subject_type, subject_name, expected_poi_id, polarity, facet, note, quote_verdict, "
        "created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            label_id,
            document_id,
            text,
            location.start,
            location.end,
            subject_type,
            subject_name,
            expected_poi_id,
            polarity,
            facet,
            note,
            location.verdict.value,
            created_at,
        ),
    )
    return GoldLabelRow(
        label_id=label_id,
        source_document_id=document_id,
        quote=text,
        polarity=polarity,
        subject_type=subject_type,
        subject_name=subject_name,
        expected_poi_id=expected_poi_id,
        facet=facet,
        char_start=location.start,
        char_end=location.end,
        note=note,
        created_at=created_at,
        verdict=location.verdict.value,
    )


def remove_label(*, conn: sqlite3.Connection, label_id: str) -> bool:
    """删掉一条标注。返回是否真的删掉了。"""
    cursor = conn.execute("DELETE FROM gold_label WHERE id = ?", (label_id,))
    return cursor.rowcount > 0


def update_label(
    *,
    conn: sqlite3.Connection,
    label_id: str,
    expected_poi_id: str | None = None,
    subject_name: str | None = None,
    facet: str | None = None,
    note: str | None = None,
    set_poi: bool = False,
) -> GoldLabelRow | None:
    """补全一条已有标注。

    标注是**反复来回**的：先照着原文把事实写下来，再去查这个提及指的是哪个
    景点。没有补全的口子，人就只能删掉重加，而重加之后的 `label_id` 变了，
    导出与比对都会错位。

    `set_poi=True` 才写 `expected_poi_id`——它要能表达「把 POI 清掉」，
    也要能表达「这次不改 POI」，两者不能都用 `None` 表示。
    """
    fields: list[str] = []
    values: list[object] = []
    if set_poi:
        fields.append("expected_poi_id = ?")
        values.append(expected_poi_id)
    if subject_name is not None:
        fields.append("subject_name = ?")
        values.append(subject_name)
    if facet is not None:
        fields.append("facet = ?")
        values.append(facet)
    if note is not None:
        fields.append("note = ?")
        values.append(note)

    if fields:
        values.append(label_id)
        cursor = conn.execute(
            f"UPDATE gold_label SET {', '.join(fields)} WHERE id = ?", values
        )
        if cursor.rowcount == 0:
            return None

    row = conn.execute("SELECT * FROM gold_label WHERE id = ?", (label_id,)).fetchone()
    return _row(row) if row is not None else None


def labels_for_document(
    document_id: str, *, conn: sqlite3.Connection | None = None
) -> list[GoldLabelRow]:
    owned = conn is None
    active = conn or connect()
    try:
        rows = active.execute(
            "SELECT * FROM gold_label WHERE source_document_id = ? "
            "ORDER BY COALESCE(char_start, 1 << 30), created_at, rowid",
            (document_id,),
        ).fetchall()
    finally:
        if owned:
            active.close()
    return [_row(item) for item in rows]


def _row(row: sqlite3.Row) -> GoldLabelRow:
    return GoldLabelRow(
        label_id=row["id"],
        source_document_id=row["source_document_id"],
        quote=row["quote"],
        polarity=row["polarity"],
        subject_type=row["subject_type"],
        subject_name=row["subject_name"],
        expected_poi_id=row["expected_poi_id"],
        facet=row["facet"],
        char_start=row["char_start"],
        char_end=row["char_end"],
        note=row["note"],
        created_at=row["created_at"],
        # 迁移 9 之前的行没记判定结果，它们的位置是当初按精确算出来的
        verdict=row["quote_verdict"] or QuoteVerdict.EXACT.value,
    )


def mark_annotated(
    *,
    conn: sqlite3.Connection,
    document_id: str,
    model_output_seen: bool = False,
    annotator: str | None = None,
    note: str | None = None,
    annotated_at: str,
) -> None:
    """把一篇素材记入金标准集。

    `model_output_seen` 记下标注时**有没有看过模型输出**。看过后再标，
    人会不自觉地只修模型给的东西而漏掉模型没抽到的——这是金标准最常见的
    偏差来源。把它留在数据里，日后才判断得了一个偏乐观的指标
    是提纯真的变好了，还是标注方式造成的。
    """
    conn.execute(
        "INSERT INTO gold_set (source_document_id, annotator, model_output_seen, note, annotated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(source_document_id) DO UPDATE SET "
        "annotator = excluded.annotator, "
        "model_output_seen = excluded.model_output_seen, "
        "note = excluded.note, "
        "annotated_at = excluded.annotated_at",
        (document_id, annotator, 1 if model_output_seen else 0, note, annotated_at),
    )


def unmark_annotated(*, conn: sqlite3.Connection, document_id: str) -> bool:
    """把一篇素材移出金标准集。已有的标注行留着不动。"""
    cursor = conn.execute("DELETE FROM gold_set WHERE source_document_id = ?", (document_id,))
    return cursor.rowcount > 0


def gold_documents(*, conn: sqlite3.Connection | None = None) -> list[GoldDocument]:
    """全部素材及其标注状态。工作台用它决定「下一篇标哪篇」。"""
    owned = conn is None
    active = conn or connect()
    try:
        rows = active.execute(
            "SELECT d.id, d.title, d.site, LENGTH(d.body_text) AS chars, "
            "  (SELECT COUNT(*) FROM gold_label g WHERE g.source_document_id = d.id) AS labeled, "
            "  (SELECT COUNT(*) FROM extraction_run r "
            "   WHERE r.source_document_id = d.id AND r.status = 'ok') AS runs, "
            "  (SELECT r.accepted_count FROM extraction_run r "
            "   WHERE r.source_document_id = d.id AND r.status = 'ok' "
            "   ORDER BY r.created_at DESC, r.rowid DESC LIMIT 1) AS prediction_count, "
            "  s.source_document_id IS NOT NULL AS in_gold_set, "
            "  COALESCE(s.model_output_seen, 0) AS model_output_seen, "
            "  s.annotated_at "
            "FROM source_document d LEFT JOIN gold_set s ON s.source_document_id = d.id "
            "ORDER BY in_gold_set, labeled DESC, d.imported_at"
        ).fetchall()
    finally:
        if owned:
            active.close()

    return [
        GoldDocument(
            document_id=row["id"],
            title=row["title"],
            site=row["site"],
            chars=row["chars"] or 0,
            prediction_count=row["prediction_count"] or 0,
            labeled=row["labeled"],
            in_gold_set=bool(row["in_gold_set"]),
            model_output_seen=bool(row["model_output_seen"]),
            annotated_at=row["annotated_at"],
        )
        for row in rows
    ]


def gold_stats(*, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """金标准集的规模。评测报告要带上它——样本量决定指标能信几分。"""
    owned = conn is None
    active = conn or connect()
    try:
        row = active.execute(
            "SELECT (SELECT COUNT(*) FROM gold_set) AS documents, "
            "  (SELECT COUNT(*) FROM gold_label) AS labels, "
            "  (SELECT COUNT(*) FROM gold_label WHERE expected_poi_id IS NOT NULL) AS with_poi, "
            "  (SELECT COUNT(*) FROM gold_set WHERE model_output_seen = 0) AS blind"
        ).fetchone()
    finally:
        if owned:
            active.close()
    return {key: row[key] for key in row.keys()}
