"""提纯领域模型：从攻略原文里抽出来的「待验证的经验」。

设计里写死了一条规则（docs/DESIGN.md 4.2）：

    **提纯只做搬运与归一，不产生新事实**——凡是在原文里找不到对应片段的
    候选一律丢弃。

这条规则的执行点就是本模块的 `verify_quote()`。它成立的前提实测过
（docs/M3-PROBE.md 第一节）：两篇素材抽出 28 条候选，引文
**28/28 都是原文的精确连续子串**，连换行、空格、直角引号都原样保留。

宽松匹配仍然要实现，但定位是**诊断**而不是常态化兜底：真实网文里有全角
半角混排、错别字、平台插入的表情符号，一旦某篇素材出现大量 `loose` 命中，
说明该篇的正文清洗有问题，那是要看的信号，不该静默接受。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from lushu.domain.knowledge import Facet, Polarity, SubjectType
from lushu.domain.similarity import normalize_body

# 引文太短就不能作为证据。
# 「提前预约」四个字可以出现在任何一篇攻略里，它证明不了什么；
# 而设计要求的「引用原文中根本没有的话等于没有溯源」需要一个最小长度才站得住。
MIN_QUOTE_CHARS = 6


class QuoteVerdict(StrEnum):
    """引文在原文里的可定位性。"""

    EXACT = "exact"  # 一字不差的连续子串，唯一可接受为常态的判定
    LOOSE = "loose"  # 去掉空白与常见标点后能找到，属于要看的信号
    TOO_SHORT = "too_short"  # 短到无法证明任何事
    MISSING = "missing"  # 找不到——按设计规则必须丢弃


@dataclass(frozen=True)
class QuoteLocation:
    """引文在原文里的位置。`start`/`end` 是归一化正文里的字符偏移。"""

    verdict: QuoteVerdict
    start: int
    end: int

    @property
    def usable(self) -> bool:
        """这条引文能不能作为证据入库。

        `LOOSE` 也算可用：模型改了标点时，事实仍然在原文里。
        但它是可以被质问的，所以判定要存下来。
        """
        return self.verdict in (QuoteVerdict.EXACT, QuoteVerdict.LOOSE)


@dataclass(frozen=True)
class ExtractedClaim:
    """一条待验证的经验。产出不是结论，只是候选。"""

    subject_name: str
    subject_type: SubjectType
    polarity: Polarity
    facet: Facet
    text: str
    quote: str

    def __post_init__(self) -> None:
        if not self.subject_name.strip():
            raise ValueError("候选必须给出主体名，否则无从对齐")
        if not self.text.strip():
            raise ValueError("候选结论不能为空")
        if not self.quote.strip():
            raise ValueError("候选必须带原文片段，无引文的候选不得入库（DESIGN 4.2）")


@dataclass(frozen=True)
class VerifiedClaim:
    """通过了引文校验的候选，带上它在原文里的位置。"""

    claim: ExtractedClaim
    location: QuoteLocation


@dataclass(frozen=True)
class ExtractionOutcome:
    """一次提纯的完整结果。

    `dropped` 与 `accepted` 都要留着：只报「抽出了几条」而不报「丢了几条、
    为什么丢」，就区分不出「这一篇本来就没内容」与「模型编了引文」——
    而这正是设计里担心的「无法区分提纯很准与只看到了准的那几条」。
    """

    document_id: str
    accepted: tuple[VerifiedClaim, ...]
    dropped: tuple[tuple[ExtractedClaim, QuoteVerdict], ...]
    model: str
    prompt_version: str
    duration_ms: int = 0

    @property
    def candidate_count(self) -> int:
        return len(self.accepted) + len(self.dropped)

    @property
    def loose_count(self) -> int:
        """需要看一眼的引文条数。数量多了说明该篇正文清洗有问题。"""
        return sum(
            1 for item in self.accepted if item.location.verdict is QuoteVerdict.LOOSE
        )


# 宽松匹配时要抹掉的字符：空白加上中文标点，以及拉丁标点里常见的那几个。
#
# 刻意**不**做全角半角转换：那是内容层面的改动，会掩盖真实的差异
# （「２０分钟」与「20分钟」在原文里就是两种写法，抹平了就查不出问题）。
_LOOSE_IGNORE = set(
    " \t\r\n\u3000，。、；：！？\u201c\u201d\u2018\u2019（）《》〈〉【】…—～·,.;:!?\"'()[]<>-"
)


def _squash(text: str, ignore: set[str]) -> str:
    return "".join(char for char in text if char not in ignore)


def locate_quote(quote: str, body: str) -> QuoteLocation:
    """在原文里定位引文。

    两档判定，先精确后宽松。返回的偏移指向**归一化后的正文**
    （`normalize_body` 的结果），与入库时保存的正文保持一致——
    偏移对不上正文就等于没有溯源。
    """
    text = quote.strip()
    if len(text) < MIN_QUOTE_CHARS:
        return QuoteLocation(QuoteVerdict.TOO_SHORT, -1, -1)

    start = body.find(text)
    if start >= 0:
        return QuoteLocation(QuoteVerdict.EXACT, start, start + len(text))

    # 宽松定位。映射回原文偏移的做法是「数一数被抹掉的字符」：
    # 逐字扫描归一化后的正文，跳过被忽略的字符，直到匹配上抹平后的引文。
    squashed_body = _squash(body, _LOOSE_IGNORE)
    squashed_quote = _squash(text, _LOOSE_IGNORE)
    if not squashed_quote:
        return QuoteLocation(QuoteVerdict.MISSING, -1, -1)

    flat_index = squashed_body.find(squashed_quote)
    if flat_index < 0:
        return QuoteLocation(QuoteVerdict.MISSING, -1, -1)

    # 把「抹平后的下标」换算回「原文下标」
    seen = 0
    start = -1
    end = -1
    for index, char in enumerate(body):
        if char in _LOOSE_IGNORE:
            continue
        if seen == flat_index:
            start = index
        seen += 1
        if seen == flat_index + len(squashed_quote):
            end = index + 1
            break

    if start < 0 or end < 0:
        return QuoteLocation(QuoteVerdict.MISSING, -1, -1)
    return QuoteLocation(QuoteVerdict.LOOSE, start, end)


def verify_extraction(
    document_id: str,
    raw_body: str,
    candidates: list[ExtractedClaim] | tuple[ExtractedClaim, ...],
    *,
    model: str,
    prompt_version: str,
    duration_ms: int = 0,
) -> ExtractionOutcome:
    """把模型吐出的候选用原文校验一遍，丢掉引文对不上的那些。

    **这是「提纯不产生新事实」这条设计的执行点。** 引文在原文里找不到，
    这条候选就不进管线——哪怕它的结论读起来完全合理。
    """
    body = normalize_body(raw_body)
    accepted: list[VerifiedClaim] = []
    dropped: list[tuple[ExtractedClaim, QuoteVerdict]] = []

    for candidate in candidates:
        location = locate_quote(candidate.quote, body)
        if location.usable:
            accepted.append(VerifiedClaim(claim=candidate, location=location))
        else:
            dropped.append((candidate, location.verdict))

    return ExtractionOutcome(
        document_id=document_id,
        accepted=tuple(accepted),
        dropped=tuple(dropped),
        model=model,
        prompt_version=prompt_version,
        duration_ms=duration_ms,
    )
