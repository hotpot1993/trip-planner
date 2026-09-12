"""攻略原文检索。

刻意不做全文索引，理由见 `schema.py` 里「关于全文检索」那一节：
中文没有词边界，SQLite 的两个内置分词器都对两字词无效
（unicode61 把整串汉字当一个词元，trigram 要求至少三个字符）。

这里用 LIKE 子串扫描。它在语义上恰好等于我们真正要问的问题——
「这段原文里有没有这句话」——而且对任意长度的中文查询都准确。
自用规模下（数百到数千篇素材）性能完全够用。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .connection import connect

DEFAULT_LIMIT = 50

# LIKE 里的通配符必须转义，否则搜「100%」这类词会退化成匹配所有素材
_LIKE_ESCAPE = "\\"


@dataclass(frozen=True)
class SourceMatch:
    """一条命中的攻略素材。"""

    document_id: str
    site: str
    title: str | None
    url: str | None
    snippet: str
    offset: int


def _escape_like(phrase: str) -> str:
    return (
        phrase.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )


def _snippet(body: str, offset: int, width: int = 80) -> str:
    """截取命中位置周围的一段原文，两端加省略号。"""
    half = width // 2
    start = max(0, offset - half)
    end = min(len(body), offset + half)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return f"{prefix}{body[start:end]}{suffix}"


def find_documents_containing(
    phrase: str,
    *,
    conn: sqlite3.Connection | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[SourceMatch]:
    """找出正文或标题里包含该片段的素材。

    这是「回查这条结论的原文到底怎么说的」的入口，也是人工复核时最常用的动作。
    """
    phrase = phrase.strip()
    if not phrase:
        return []

    owned = conn is None
    active = conn or connect()
    try:
        pattern = f"%{_escape_like(phrase)}%"
        rows = active.execute(
            "SELECT id, site, title, url, body_text FROM source_document "
            f"WHERE body_text LIKE ? ESCAPE '{_LIKE_ESCAPE}' "
            f"   OR title LIKE ? ESCAPE '{_LIKE_ESCAPE}' "
            "ORDER BY imported_at DESC LIMIT ?",
            (pattern, pattern, limit),
        ).fetchall()
    finally:
        if owned:
            active.close()

    matches: list[SourceMatch] = []
    for row in rows:
        body = row["body_text"] or ""
        offset = body.find(phrase)
        if offset < 0:
            # 命中的是标题而不是正文
            offset = 0
        matches.append(
            SourceMatch(
                document_id=row["id"],
                site=row["site"],
                title=row["title"],
                url=row["url"],
                snippet=_snippet(body, offset) if body else "",
                offset=offset,
            )
        )
    return matches


def document_contains(
    phrase: str,
    document_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """某篇素材是否包含该片段。

    证据的原文片段在入库前要用它校验一次——引用了原文里根本没有的话，
    等于没有溯源。
    """
    phrase = phrase.strip()
    if not phrase:
        return False

    owned = conn is None
    active = conn or connect()
    try:
        row = active.execute(
            "SELECT 1 FROM source_document "
            f"WHERE id = ? AND body_text LIKE ? ESCAPE '{_LIKE_ESCAPE}' LIMIT 1",
            (document_id, f"%{_escape_like(phrase)}%"),
        ).fetchone()
    finally:
        if owned:
            active.close()
    return row is not None
