"""金标准评测：预测出来的结论，怎么算跟人工标注的是同一条。

**这里刻意不用文本相似度当判据。** 人工标注写的是「只有午门能进」，
模型可能写成「午门是唯一入口」——两者字面重合不到一半，说的却是同一件事；
反过来「门票 120」与「门票 100」字面只差一个字，却是**互相矛盾**的两条。
按相似度算，前者被算作漏抽，后者被算作命中，两个方向都错。

判据于是落在三件可以逐个查证的事情上（ADR-0010）：

1. **主体同一**——说的是不是同一个地方。复用 `lushu.domain.align.name_score`，
   与实体对齐用同一套名称判据：对齐认为「这两个名字指同一个地方」，
   评测就不该认为「这两条结论说的不是同一个地方」。
2. **极性相同**——`avoid`（避坑）与 `highlight`（打卡）是两类结论。
   「这家店好吃」和「这家店别去」抽自同一句话也不能算命中（Q26）。
3. **事实点相同**——按**原文位置**判，不按措辞判。标注与预测都存了
   `char_start/char_end`，区间重叠就是同一句原文里长出来的。位置对不上时
   才退化成文本比对，作为补救而不是主判据。

比对是**一对一**的：一条标注最多命中一条预测，一条预测最多命中一条标注。
模型把同一件事抽成两条时，一条算命中、另一条算多抽——否则精确率会因为
「多说几遍」而虚高。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .align import name_score
from .similarity import overlap

# 主体名判为「同一个地方」所需的分。取 0.85 是 align.name_score 的分档位置：
# 完全同名(1.0)、去掉「公园/景区」类通名后同名(0.95)、整体包含(0.90)、
# 分隔符之后的子点(0.90) 都过线；只靠字符重合度凑出来的分数过不了。
#
# 「陕历博」对「陕西历史博物馆」这种简称过不了线，这是**故意的**：
# 那种情况靠标注里的 `expected_poi_id` 认，不该靠字符串猜。
SUBJECT_NAME_SCORE = 0.85

# 位置对不上时，靠文字补救判为同一事实点所需的 LCS 覆盖率。
# 比正文判重的 DUPLICATE_COVERAGE(0.60) 松一些：引文是短句，
# 标注者可能圈了整段而模型只摘了半句，短的那一方仍被覆盖得很满。
FACT_COVERAGE = 0.50

# 文字补救时的最短公共片段。正文判重取 12 是为了压掉两位作者写同一座城市的
# 措辞偶合，而这里比的是「同一个地方的同一条结论」，偶合概率低得多；
# 况且「门票 120」这类事实点的骨架本来就只有几个字。
FACT_MIN_RUN = 5


@dataclass(frozen=True)
class GoldItem:
    """人工标注的一条真实结论。

    `quote` 必须能在原文里找到——强制回原文是这套标注唯一的质量保证：
    没有它，标注会慢慢退化成「凭印象写结论」，而那种金标准测不出任何东西。
    """

    label_id: str
    quote: str
    polarity: str
    subject_type: str = "poi"
    subject_name: str | None = None
    expected_poi_id: str | None = None
    facet: str | None = None
    char_start: int | None = None
    char_end: int | None = None


@dataclass(frozen=True)
class Prediction:
    """提纯（并对齐过）产出的一条候选结论。"""

    subject_name: str
    polarity: str
    text: str
    quote: str
    subject_type: str = "poi"
    facet: str = "other"
    char_start: int | None = None
    char_end: int | None = None
    poi_id: str | None = None

    @property
    def cited(self) -> bool:
        """是否带着可回查的原文位置。没有位置的预测只能靠文字比对。"""
        return self.char_start is not None and self.char_end is not None


# ─── 单项比对 ────────────────────────────────────────────────


def same_subject(prediction: Prediction, gold: GoldItem) -> bool:
    """两条结论说的是不是同一个地方。

    **主体相同不等于挂对了 POI。** 名字一致而 POI 不同时，这里判「同一主体」
    （它们确实是同一个提及），挂错由对齐准确率单独计一笔。这样处理是刻意的：
    一个错误只该出现在一个指标里。若让 POI 不一致反过来否决主体相同，
    一次挂错会同时扣掉召回率和精确率，指标之间就不再正交，
    「抽取变好了还是对齐变差了」也无从分辨。
    """
    if prediction.poi_id and gold.expected_poi_id and prediction.poi_id == gold.expected_poi_id:
        # 两边都指到了同一个 POI，这是最硬的证据，不必再看名字
        return True
    if not gold.subject_name:
        # 标注没写提及名，只剩 POI id 一条路；上面没命中就是判不出
        return False
    return name_score(prediction.subject_name, gold.subject_name) >= SUBJECT_NAME_SCORE


def same_fact(prediction: Prediction, gold: GoldItem) -> bool:
    """两条结论说的是不是同一件事。

    先看原文位置，位置对不上再退回文字比对。**位置不重叠不构成否决**：
    原文可能把同一件事说了两遍，标注圈了前一句、模型摘了后一句，
    那仍然是抽到了。
    """
    if _ranges_overlap(
        prediction.char_start, prediction.char_end, gold.char_start, gold.char_end
    ):
        return True

    threshold = FACT_COVERAGE
    for left in (prediction.quote, prediction.text):
        if not left:
            continue
        if overlap(left, gold.quote, min_run=FACT_MIN_RUN).coverage >= threshold:
            return True
    return False


def _ranges_overlap(
    a_start: int | None, a_end: int | None, b_start: int | None, b_end: int | None
) -> bool:
    if a_start is None or a_end is None or b_start is None or b_end is None:
        return False
    # 端点相接不算重叠：区间是半开的 [start, end)
    return a_start < b_end and b_start < a_end


@dataclass(frozen=True)
class Verdict:
    """一条预测对一条标注的判定结果。"""

    hit: bool
    reason: str = ""
    poi_agrees: bool | None = None


def compare(prediction: Prediction, gold: GoldItem) -> Verdict:
    """判定一条预测是否命中了这条标注，命中不了就说清是哪一关没过。"""
    if prediction.polarity != gold.polarity:
        return Verdict(False, f"极性不同（预测 {prediction.polarity}，标注 {gold.polarity}）")
    if not same_subject(prediction, gold):
        return Verdict(
            False, f"主体不同（预测「{prediction.subject_name}」，标注「{gold.subject_name or '未写'}」）"
        )
    if not same_fact(prediction, gold):
        return Verdict(False, "事实点不同（原文位置不重叠且文字不重合）")

    agrees: bool | None = None
    if gold.expected_poi_id:
        agrees = prediction.poi_id == gold.expected_poi_id
    return Verdict(True, "", agrees)


# ─── 整篇配对 ────────────────────────────────────────────────


@dataclass(frozen=True)
class Mismatch:
    """一条没配上的预测，附带它为什么没配上。"""

    prediction: Prediction
    reason: str


@dataclass(frozen=True)
class Pairing:
    """一篇素材里，预测与标注的一对一配对结果。"""

    matched: tuple[tuple[Prediction, GoldItem], ...]
    missed: tuple[GoldItem, ...]
    spurious: tuple[Mismatch, ...]
    misaligned: tuple[tuple[Prediction, GoldItem], ...]
    no_poi_in_gold: int  # 命中的条里，标注没给 POI、因而无法判对齐的条数

    @property
    def hits(self) -> int:
        return len(self.matched)


def pair(predictions: Sequence[Prediction], golds: Sequence[GoldItem]) -> Pairing:
    """把一篇素材的预测与标注配成一对一。

    贪心：按标注的顺序逐条去找还没被占用的预测，第一个判定命中的就配上。
    标注的顺序在服务层按 `created_at` 固定下来，同一份数据每次跑结果一致——
    评测结果必须是可复现的，否则「改了一版提示词，涨了两个点」无从判断真伪。
    """
    used: set[int] = set()
    matched: list[tuple[Prediction, GoldItem]] = []
    misaligned: list[tuple[Prediction, GoldItem]] = []
    missed: list[GoldItem] = []

    for gold in golds:
        found = None
        for index, prediction in enumerate(predictions):
            if index in used:
                continue
            verdict = compare(prediction, gold)
            if verdict.hit:
                found = (index, verdict)
                break
        if found is None:
            missed.append(gold)
            continue
        index, verdict = found
        used.add(index)
        matched.append((predictions[index], gold))
        if verdict.poi_agrees is False:
            misaligned.append((predictions[index], gold))

    spurious: list[Mismatch] = []
    for index, prediction in enumerate(predictions):
        if index in used:
            continue
        spurious.append(Mismatch(prediction, _why_unmatched(prediction, golds)))

    return Pairing(
        matched=tuple(matched),
        missed=tuple(missed),
        spurious=tuple(spurious),
        misaligned=tuple(misaligned),
        no_poi_in_gold=sum(1 for _, gold in matched if not gold.expected_poi_id),
    )


def _why_unmatched(prediction: Prediction, golds: Sequence[GoldItem]) -> str:
    """多抽的一条，最接近的标注是差在哪一关。

    这条信息是调提示词的直接依据：「多抽的全是主体判错」与「多抽的全是
    把感慨当成了结论」，要改的地方完全不同。
    """
    if not golds:
        return "这篇素材没有任何标注"
    best = min(
        (compare(prediction, gold) for gold in golds),
        key=lambda verdict: (not verdict.hit,),
    )
    if best.hit:
        # 判定命中却被别人占走了——模型把同一件事抽成了两条
        return "与另一条预测重复（同一件事抽了两遍）"
    return best.reason


# ─── 三个指标 ────────────────────────────────────────────────


@dataclass(frozen=True)
class DocumentScore:
    """一篇素材的评分。"""

    document_id: str
    pairing: Pairing
    predicted_total: int
    gold_total: int

    @property
    def hits(self) -> int:
        return self.pairing.hits

    @property
    def recall(self) -> float | None:
        return self.hits / self.gold_total if self.gold_total else None

    @property
    def precision(self) -> float | None:
        return self.hits / self.predicted_total if self.predicted_total else None

    @property
    def judged(self) -> int:
        """命中的条里，标注给了 POI、对齐判得出来的条数。"""
        return self.pairing.hits - self.pairing.no_poi_in_gold

    @property
    def unaligned(self) -> int:
        """预测里压根没挂上 POI 的条数。

        这些条目挂不到任何景点上，对排程等于不存在，所以它们在对齐准确率里
        算错；但错因是「高德没有这个 POI」还是「挂到了别的地方」是两回事，
        这个计数就是为了把两者分开看，而不是混成一个数字。
        """
        return sum(1 for prediction, _ in self.pairing.matched if not prediction.poi_id) + sum(
            1 for item in self.pairing.spurious if not item.prediction.poi_id
        )

    @property
    def alignment_accuracy(self) -> float | None:
        if not self.judged:
            return None
        return (self.judged - len(self.pairing.misaligned)) / self.judged


@dataclass(frozen=True)
class EvalReport:
    """整个金标准集上的评测。

    汇总用 micro（把各篇的计数直接相加再除），不用 macro（各篇比率求平均）。
    理由：一篇只标了 1 条结论的素材，在 macro 下与标了 30 条的有同等话语权，
    而前者的一条对错只是运气。micro 下每条结论等权，这才是「提纯质量」的意思。
    """

    documents: tuple[DocumentScore, ...]

    @property
    def gold_total(self) -> int:
        return sum(item.gold_total for item in self.documents)

    @property
    def predicted_total(self) -> int:
        return sum(item.predicted_total for item in self.documents)

    @property
    def hits(self) -> int:
        return sum(item.hits for item in self.documents)

    @property
    def judged(self) -> int:
        return sum(item.judged for item in self.documents)

    @property
    def misaligned(self) -> int:
        return sum(len(item.pairing.misaligned) for item in self.documents)

    @property
    def unaligned(self) -> int:
        return sum(item.unaligned for item in self.documents)

    @property
    def recall(self) -> float | None:
        return self.hits / self.gold_total if self.gold_total else None

    @property
    def precision(self) -> float | None:
        return self.hits / self.predicted_total if self.predicted_total else None

    @property
    def alignment_accuracy(self) -> float | None:
        if not self.judged:
            return None
        return (self.judged - self.misaligned) / self.judged

    @property
    def sample_size(self) -> int:
        return len(self.documents)


def evaluate(
    predictions: Mapping[str, Sequence[Prediction]],
    golds: Mapping[str, Sequence[GoldItem]],
) -> EvalReport:
    """跑评测。

    只统计**已被标注**的素材：没进金标准集的篇，既不知道它有几条真结论，
    也不知道模型抽的对不对，把它算进来只会让分母变成一个说不清的东西。
    """
    scores: list[DocumentScore] = []
    for document_id in sorted(golds):
        gold_items = list(golds[document_id])
        predicted = list(predictions.get(document_id, ()))
        scores.append(
            DocumentScore(
                document_id=document_id,
                pairing=pair(predicted, gold_items),
                predicted_total=len(predicted),
                gold_total=len(gold_items),
            )
        )
    return EvalReport(documents=tuple(scores))
