"""文本近重复检测：中文攻略的转载与洗稿要靠什么认出来。

**这个模块的形状是被实测定下来的，不是设计出来的**，结论见
`docs/M3-PROBE.md` 第二节与 `docs/adr/0008`。三条要点：

1. **SimHash 不能用。** 实测同篇的 64 位海明距离落在 [27, 34]，
   异篇落在 [26, 34]，区间完全重叠。它的降维前提是长文本里随机差异会互相
   抵消，而中文攻略常见只有几百字，前提不成立。
2. **n-gram Jaccard 不能单独用。** 最优的 4-gram 下同篇最低只有 0.045，
   任何 ≥0.2 的阈值都会漏掉「逐句改写」那一档。
3. **LCS 覆盖可用。** 找出所有长度 ≥ `MIN_RUN_CHARS` 的公共连续片段，
   看较短的一篇被覆盖了多少。实测同篇 [0.051, 0.852]，异篇 [0.000, 0.000]，
   是唯一能把异篇压到零的指标。

同时必须记住它有一个**已知盲区**：逐句改写的洗稿（每句话都换说法）
LCS 覆盖只有 0.051，和异篇一样低，本模块认不出来。那需要「结论同源」这一层
（在两篇的提纯结果上比对结论重合度），不在本模块职责内。
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

# 视为「同一段文字」的最短连续片段长度。
# 实测取 12 时能把异篇压到 0：两位不同作者写同一座城市，措辞偶合一般不超过
# 十来个字，而转发与节选会留下几十字的连续片段。
MIN_RUN_CHARS = 12

# 短篇被覆盖多少比例即判为同一来源。
# 实测同一篇的三个变体里，「全文照搬」与「节选转载」都在 0.85 上下，
# 而所有异篇组合都是 0.000，阈值取在中间留足余量。
DUPLICATE_COVERAGE = 0.60

# 单段极长公共片段即足以判定，不要求整体覆盖率。
# 「转载时在前面加了三百字编者按」这类情况下，覆盖率会被自己的加料稀释，
# 但那一大段照搬的正文是硬证据。
DECISIVE_RUN_CHARS = 40


@dataclass(frozen=True)
class Overlap:
    """两篇正文的重合度量。"""

    coverage: float  # 较短一篇被公共片段覆盖的字符比例
    longest_run: int  # 最长的公共连续片段长度
    shorter_chars: int  # 较短一篇去掉空白后的字符数

    @property
    def is_duplicate(self) -> bool:
        """是否判为同一来源的转载。

        两个判据取或：整体覆盖率够高，或者存在一段足够长的照搬。
        """
        if self.shorter_chars == 0:
            return False
        return self.coverage >= DUPLICATE_COVERAGE or self.longest_run >= DECISIVE_RUN_CHARS


def _squash(text: str) -> str:
    """去掉所有空白。

    中文攻略在网页上会被硬折行，同一段话从两个站点复制下来换行位置不同。
    空白参与比对会让本该命中的片段错开，所以先统一抹掉。
    """
    return "".join(char for char in text if not char.isspace())


def overlap(a: str, b: str, *, min_run: int = MIN_RUN_CHARS) -> Overlap:
    """量两篇正文的重合程度。

    覆盖率的分母是**较短的那一篇**，不看顺序。这样「长文里摘了一段」
    与「把一段扩写成一篇」得到同样的判定——两种情况都是同一个来源。
    """
    left, right = _squash(a), _squash(b)
    if len(left) > len(right):
        left, right = right, left
    if not left:
        return Overlap(coverage=0.0, longest_run=0, shorter_chars=0)

    matcher = SequenceMatcher(None, left, right, autojunk=False)
    covered = 0
    longest = 0
    for block in matcher.get_matching_blocks():
        if block.size < min_run:
            continue
        covered += block.size
        longest = max(longest, block.size)

    return Overlap(
        coverage=covered / len(left),
        longest_run=longest,
        shorter_chars=len(left),
    )


def is_duplicate(a: str, b: str, *, min_run: int = MIN_RUN_CHARS) -> bool:
    """两篇正文是否属于同一来源。"""
    return overlap(a, b, min_run=min_run).is_duplicate


def normalize_body(text: str) -> str:
    """导入前的正文归一。

    只做不会改变语义的清理：统一换行、去掉行尾空白、压掉连续空行。
    刻意**不做**全角半角转换与错别字纠正——引文校验要求模型给出的片段是
    原文的连续子串（见 `lushu/domain/extraction.py`），正文被改过就会失配。
    """
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    squashed: list[str] = []
    for line in lines:
        # 开头与中间的空行都要压缩：开头多出来的空行会让引文的字符偏移
        # 整体后移，偏移一旦用于回查原文就会指错位置。
        if not line and (not squashed or not squashed[-1]):
            continue
        squashed.append(line)
    while squashed and not squashed[-1]:
        squashed.pop()
    return "\n".join(squashed)
