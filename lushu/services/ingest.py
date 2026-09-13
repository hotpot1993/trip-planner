"""攻略素材的导入。

两种入口（docs/DESIGN.md 4.1）：粘贴正文（单篇精读）、导入 HTML 或 Markdown
文件（批量囤稿），外加从公开页面抓取。**不碰任何需要登录的内容**（Q9、Q22）。

导入即算两样东西：

1. **正文指纹**（sha256）—— 精确去重。同一篇粘两次不该变成两篇。
2. **正文相似度**（LCS 覆盖，ADR-0008）—— 近似去重，用于识别转载与节选。
   实测同篇的覆盖率 [0.051, 0.852]、异篇 [0.000, 0.000]，
   所以阈值定在 0.60 能把异篇完全排除在外。

归组在这一步只做**第一层**（文字复制）。第二层「结论同源」要在提纯之后才能做，
所以 `ls group run` 可以反复执行（ADR-0008）。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

from lushu.domain.similarity import (
    DUPLICATE_COVERAGE,
    MIN_RUN_CHARS,
    normalize_body,
    overlap,
)
from lushu.store.connection import connect
from lushu.store.ids import SOURCE, SOURCE_GROUP, new_id

# 支持的站点标识。与 `source_document.site` 的注释保持一致。
KNOWN_SITES = ("mafengwo", "zhihu", "qiongyou", "ctrip", "xhs", "manual")

# 域名 → 站点标识。识别不出来的一律记 manual，
# 不猜——猜错站点会影响「独立来源」的域名判据。
_SITE_BY_DOMAIN: dict[str, str] = {
    "mafengwo.cn": "mafengwo",
    "mafengwo.com": "mafengwo",
    "zhihu.com": "zhihu",
    "zhuanlan.zhihu.com": "zhihu",
    "qyer.com": "qiongyou",
    "ctrip.com": "ctrip",
    "you.ctrip.com": "ctrip",
    "xiaohongshu.com": "xhs",
    "xhslink.com": "xhs",
}

# HTML 里要剥掉的块级元素。整块删掉而不是只删标签，
# 否则 `<script>` 里的代码会混进正文，污染指纹与引文偏移。
_HTML_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|svg|iframe)\b.*?</\1>", re.IGNORECASE | re.DOTALL
)
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_BLOCK_END = re.compile(
    r"</(p|div|li|tr|h[1-6]|section|article|blockquote|br)\s*>", re.IGNORECASE
)
_HTML_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HTML_ENTITY = re.compile(r"&(nbsp|amp|lt|gt|quot|#39|#x27);")
_ENTITIES = {
    "nbsp": " ",
    "amp": "&",
    "lt": "<",
    "gt": ">",
    "quot": '"',
    "#39": "'",
    "#x27": "'",
}


class ImportKind(StrEnum):
    PASTE = "paste"
    FILE = "file"
    FETCH = "fetch"


class DuplicateKind(StrEnum):
    """导入时的重复判定。"""

    NEW = "new"  # 库里没有
    EXACT = "exact"  # 正文指纹相同，就是同一篇
    REPOST = "repost"  # 正文高度重合，是转载或节选


@dataclass(frozen=True)
class ImportRequest:
    """一次导入请求。"""

    body: str
    title: str | None = None
    url: str | None = None
    site: str | None = None
    author: str | None = None
    published_at: str | None = None
    kind: ImportKind = ImportKind.PASTE


@dataclass(frozen=True)
class ImportResult:
    """导入结果。`duplicate` 与 `group_id` 要如实回报给用户。"""

    document_id: str
    duplicate: DuplicateKind
    site: str
    chars: int
    # 判为转载时：与哪一篇重合、重合度多少
    duplicate_of: str | None = None
    coverage: float | None = None
    group_id: str | None = None

    @property
    def stored(self) -> bool:
        """是否真的新入库了。"""
        return self.duplicate is not DuplicateKind.EXACT


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint_text(text: str) -> str:
    """正文指纹：**抹掉所有空白**之后再做 sha256。

    为什么不直接对正文取哈希：同一段话从两个站点复制下来，换行位置几乎一定
    不同，而 `normalize_body` 只压空行、不动行内空白。不抹空白的话，
    「同一篇导两次」会被判成两篇，独立来源计数随之虚高——而置信度就是靠它算的。

    抹空白只用于**判重**，不用于存储与偏移：入库的正文与引文偏移仍然基于
    `normalize_body` 的结果（`lushu/domain/extraction.py` 的引文校验要求
    偏移对准那份正文）。
    """
    return sha256_text("".join(char for char in text if not char.isspace()))


def detect_site(url: str | None) -> str:
    """从网址认站点。认不出来记 manual。"""
    if not url:
        return "manual"
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return "manual"
    for domain, site in _SITE_BY_DOMAIN.items():
        if host == domain or host.endswith("." + domain):
            return site
    return "manual"


def html_to_text(raw: str) -> str:
    """把网页 HTML 剥成正文。

    刻意做得很粗：只删块级标签与实体，不做正文抽取算法。
    理由是**引文校验要求偏移对准入库的那份正文**——正文抽取器一旦改版，
    历史引文的偏移就全错位了。宁可多留一点噪声，也不要一个会变的正文。
    噪声的影响可以在提纯阶段看到（候选变少、loose 变多），是可观测的。
    """
    text = _HTML_DROP_BLOCKS.sub("", raw)
    text = _HTML_BR.sub("\n", text)
    text = _HTML_BLOCK_END.sub("\n", text)
    text = _HTML_TAG.sub("", text)
    text = _HTML_ENTITY.sub(lambda match: _ENTITIES.get(match.group(1), ""), text)
    return normalize_body(text)


def read_source_file(path: Path) -> ImportRequest:
    """从本地文件读一篇素材。按扩展名决定要不要剥 HTML。"""
    raw = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
    if suffix in (".html", ".htm"):
        body = html_to_text(raw)
    else:
        body = normalize_body(raw)

    if not body.strip():
        raise ValueError(f"{path.name} 里没有可用正文")

    return ImportRequest(
        body=body,
        title=path.stem,
        site="manual",
        kind=ImportKind.FILE,
    )


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def import_document(
    request: ImportRequest,
    *,
    conn: sqlite3.Connection | None = None,
    now: str | None = None,
) -> ImportResult:
    """导入一篇素材，顺带做重复判定与第一层归组。

    精确重复（指纹相同）**不入库**：同一篇粘两次变成两篇，
    会让独立来源计数虚高，而置信度就是靠它算的。
    近似重复（转载）**照常入库**——它有自己的域名与作者，
    但归到同一个来源组，这样「计数按组、不按篇」才成立（ADR-0008）。
    """
    if not isinstance(request, ImportRequest):
        raise TypeError("import_document 需要 ImportRequest")

    body = normalize_body(request.body)
    if not body.strip():
        raise ValueError("正文为空，没有可导入的内容")

    site = (request.site or "").strip() or detect_site(request.url)
    if site not in KNOWN_SITES:
        site = "manual"

    digest = fingerprint_text(body)
    timestamp = now or _now()

    owned = conn is None
    active = conn or connect()
    try:
        exact = _find_exact(active, digest)
        if exact is not None:
            return ImportResult(
                document_id=exact["id"],
                duplicate=DuplicateKind.EXACT,
                site=exact["site"],
                chars=len(body),
                group_id=exact["source_group_id"],
                coverage=1.0,
            )

        repost_of, repost_coverage = _find_repost(active, body)

        group_id = None
        if repost_of is not None:
            group_id = repost_of["source_group_id"] or _ensure_group(
                active, repost_of, basis="similarity", now=timestamp
            )
            bound_repost = repost_of
        else:
            bound_repost = None

        document_id = new_id(SOURCE)
        active.execute(
            "INSERT INTO source_document (id, site, url, author, title, body_text, "
            "body_sha256, content_sha256, source_group_id, published_at, imported_at, "
            "fetched_at, import_kind, group_score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                document_id,
                site,
                request.url,
                request.author,
                request.title,
                body,
                digest,
                digest,
                group_id,
                request.published_at,
                timestamp,
                timestamp if request.kind is ImportKind.FETCH else None,
                str(request.kind),
                repost_coverage,
            ),
        )
        active.commit()

        duplicate = DuplicateKind.REPOST if bound_repost is not None else DuplicateKind.NEW
        return ImportResult(
            document_id=document_id,
            duplicate=duplicate,
            site=site,
            chars=len(body),
            duplicate_of=bound_repost["id"] if bound_repost is not None else None,
            coverage=repost_coverage,
            group_id=group_id,
        )
    finally:
        if owned:
            active.close()


def _find_exact(conn: sqlite3.Connection, digest: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, site, source_group_id FROM source_document "
        "WHERE content_sha256 = ? OR body_sha256 = ? LIMIT 1",
        (digest, digest),
    ).fetchone()


def _maybe_repost(left: str, right: str, *, threshold: float = DUPLICATE_COVERAGE) -> bool:
    """粗筛：返回 False 时，保证两篇的 LCS 覆盖**一定**低于阈值。

    **这里原先的写法是错的，代价是信任模型的闸门失效。** 旧版是
    「较短的一篇 × 3 < 较长的一篇就跳过」，理由是「长度差三倍以上时，
    较短的那篇被覆盖满也达不到阈值」。这句话本身就不成立：覆盖率的分母
    就是**较短的那一篇**（`similarity.overlap` 特意换过序），所以短篇被
    整篇照搬时覆盖率是 1.0，与两篇的长度差无关。

    真实代价：库里 99 字的《【转】西安博物馆预约提示》对 459 字的原文，
    真覆盖率 **0.722**（阈值 0.60），却因为 `99 × 3 < 459` 从没进过比对。
    两篇都没归组，于是同一条「陕历博周一闭馆」按**两篇素材**计成了
    「2 个独立来源」——正是设计第十一节列为致命的「置信度退化成转发量」，
    而那两篇的正文重合度连肉眼都看得出来。

    现在的上界取自**字符多重集合的交集**：公共片段里的每个字符都必须
    同时出现在两篇里，所以「交集字符数 ÷ 较短篇长度」一定不小于真覆盖率。
    它是 O(n) 的，比 `SequenceMatcher` 便宜得多，而筛掉的都是真的达不到
    阈值的那些对。空白没有先抹掉——那只会让上界更松，不会漏。
    """
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if len(shorter) < MIN_RUN_CHARS:
        # 公共片段至少要 MIN_RUN_CHARS 才计数，短于此则 covered 恒为 0
        return False
    shared = sum((Counter(shorter) & Counter(longer)).values())
    return shared >= threshold * len(shorter)


def best_repost_candidate(
    rows: list[sqlite3.Row] | tuple[sqlite3.Row, ...],
    body: str,
    *,
    threshold: float = DUPLICATE_COVERAGE,
) -> tuple[sqlite3.Row | None, float]:
    """在这一批素材里找 `body` 的「原件」，返回（原件，覆盖率）。

    「什么算转载」只在这里判一次：导入新素材（`_find_repost`）与重跑历史
    （`relink_reposts`）走的是同一个函数。两处各写一遍的话，重跑出来的分组
    迟早与导入时的分组长得不一样——而两个分组都自称是第一层。
    """
    best: sqlite3.Row | None = None
    best_coverage = 0.0
    for row in rows:
        existing = row["body_text"] or ""
        if not _maybe_repost(body, existing, threshold=threshold):
            continue
        result = overlap(body, existing)
        if result.coverage >= threshold and result.coverage > best_coverage:
            best = row
            best_coverage = result.coverage
    return best, best_coverage


def _find_repost(
    conn: sqlite3.Connection,
    body: str,
    *,
    threshold: float = DUPLICATE_COVERAGE,
) -> tuple[sqlite3.Row | None, float | None]:
    """在已有素材里找这一篇的「原件」。

    先用 `_maybe_repost` 粗筛，再算真正的 LCS 覆盖。这是自用规模下让 O(n)
    次比对跑得动的最简单的办法，真正的索引要等素材量上来再说（设计里说得
    很清楚，现在不上向量库）。
    """
    rows = conn.execute(
        "SELECT id, site, author, body_text, source_group_id FROM source_document "
        "ORDER BY imported_at DESC"
    ).fetchall()

    best, coverage = best_repost_candidate(rows, body, threshold=threshold)
    if best is None:
        return None, None
    return best, round(coverage, 4)


@dataclass
class RelinkReport:
    """第一层归组重跑的结果。"""

    compared: int = 0  # 本次真正做了候选比对的篇数（已经归过组的不再重算）
    merged: int = 0  # 本次新并进已有组的篇数
    groups: int = 0  # 现在有多少个来源组


def relink_reposts(
    *, conn: sqlite3.Connection | None = None, threshold: float = DUPLICATE_COVERAGE
) -> RelinkReport:
    """把第一层（文字复制）在**已经入库的素材**上重跑一遍。

    为什么必须有这个命令：第一层原先只在导入那一刻跑，也就是说判据改过、
    或者导入时判错了，历史数据永远修不回来。这不是假想——库里那对 459 字
    的原文与 99 字的《【转】西安博物馆预约提示》因为一道错的长度粗筛从没
    进过比对，长期计成两个独立来源。ADR-0008 说归组是「可以对同一批素材
    反复执行的命令」，第二层做到了，第一层当时没有。

    **只在更早导入的素材里找原件。** 原件总是先来的，而且这样不会转出环。
    同一秒导入的素材按 id 定序——不按「谁更像原件」猜，那种猜法在真实数据
    上分不出对错，只会让重跑的结果依赖于一段不可复现的顺序。

    **已经归过组的不再重算。** 它的位置是上一次判断的结果，重跑只负责补上
    漏掉的；每次重算一遍还会让「并了几篇」这个数字每次都报一遍，看不出
    到底有没有变化。组号在本轮里可能刚被建出来，所以走一个随循环更新的
    字典而不是开头取的那份快照。
    """
    owned = conn is None
    active = conn or connect()
    report = RelinkReport()
    try:
        rows = active.execute(
            "SELECT id, site, author, body_text, source_group_id FROM source_document "
            "ORDER BY imported_at, id"
        ).fetchall()
        group_of: dict[str, str | None] = {row["id"]: row["source_group_id"] for row in rows}

        earlier: list[sqlite3.Row] = []
        for row in rows:
            document_id = row["id"]
            if group_of[document_id] is None:
                report.compared += 1
                original, coverage = best_repost_candidate(
                    earlier, row["body_text"] or "", threshold=threshold
                )
                if original is not None:
                    group_id = group_of[original["id"]] or _ensure_group(
                        active, original, basis="similarity", now=_now()
                    )
                    group_of[original["id"]] = group_id
                    group_of[document_id] = group_id
                    active.execute(
                        "UPDATE source_document SET source_group_id = ?, group_score = ? "
                        "WHERE id = ?",
                        (group_id, round(coverage, 4), document_id),
                    )
                    report.merged += 1
            earlier.append(row)

        active.commit()
        report.groups = len({group for group in group_of.values() if group})
    finally:
        if owned:
            active.close()
    return report


def _ensure_group(
    conn: sqlite3.Connection,
    document: sqlite3.Row,
    *,
    basis: str,
    now: str,
) -> str:
    """给一篇还没有组的素材建组，并把已有成员拉进来。"""
    group_id = new_id(SOURCE_GROUP)
    conn.execute(
        "INSERT INTO source_group (id, site, author, basis, created_at) VALUES (?, ?, ?, ?, ?)",
        (group_id, document["site"], document["author"], basis, now),
    )
    conn.execute(
        "UPDATE source_document SET source_group_id = ? WHERE id = ?",
        (group_id, document["id"]),
    )
    return group_id
