"""实体对齐：把攻略里的自然语言景点名对到高德 POI。

风险表把这一条列为最致命的（docs/DESIGN.md 第十一节）：

    对不上，攻略知识就挂不到行程上，产品价值归零

对齐的判据是**名称 + 城市 + 类型**三者，缺一样都会出静默错误。三条实测依据
（见 `docs/M3-PROBE.md` 第三节与 `docs/adr/0009`）：

1. **城市不能靠名称保。** 搜「袁家村」限定西安，前五候选全是西安市区的连锁
   中餐馆，真正的袁家村在咸阳。名称完全一致而城市完全错误，是这里最危险的
   失败模式，所以城市是硬门禁。
2. **类型要分两类。** 搜「洒金桥」「金鱼胡同」，首位返回的是
   `地名地址信息;交通地名` 类型的道路名与桥名。「值得去的地方」与
   「只是个地名」在行程里的地位不同，不能同分。
3. **层级靠 `parent` 认亲。** `故宫博物院-午门` 的 `parent` 指向
   `故宫博物院`，所以「本体」与「本体内部的一个点」能从字段上分开。
   结论一律挂在**本体**上（ADR-0009）——行程里的一天项就是故宫博物院，
   挂到午门会生成一个只有一条结论、无法参与路线排序的孤立 POI。

本模块是纯函数：不发起网络请求，不碰数据库。候选由调用方查好后传进来，
这样对齐规则可以被完全离线测试，而离线测试正是这一段最需要的。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

from lushu.domain.lineage import CandidateSet
from lushu.domain.poi import NOT_A_DESTINATION_MARKS, CandidatePoi, PoiKind

# 名称得分达到这个值才认为「就是它」。
MATCH_SCORE_THRESHOLD = 0.80

# 只剩一个实体可选时的放宽下限。
#
# 依据是实测：俗称与官方名的字面重合度可以低到 0.48（「陕历博」对
# 「陕西历史博物馆」）甚至 0.00（「兵马俑」对「秦始皇帝陵博物院」）——
# **别名能力在高德那边，不在字符串里**。只有一个实体可选时，
# 高德的排序就是唯一的信号，按 0.80 卡掉只会让这些提及白进待对齐队列。
#
# 实测的另一面：0.40 这个下限不能取消。搜「不倒翁小姐姐」，
# 高德返回的第一条是「唐韵不倒翁」（0.44），那是一家同名小店而不是景点；
# 完全不设下限就会把它挂到行程上。
SINGLE_ENTITY_FLOOR = 0.40

# 前两名得分差小于这个值就交人工。名称相近的候选之间，高德排序没有权威性，
# 我们也没有别的判据，硬选一个不如让数据工作台问一句。
#
# 取 0.08 而不是 0.10：实测搜「钟楼」，同名候选 1.20、
# 降权后的「大慈恩寺-钟楼」1.10，差值恰好 0.10。真正该交人工的情形
# （两个候选得分完全相同）差值是 0.00，留 0.08 的余量足够把两者分开。
AMBIGUITY_MARGIN = 0.08


class AlignOutcome(StrEnum):
    """对齐的结论。允许「对不上」是一个正式的结论，不是失败。"""

    ALIGNED = "aligned"  # 认准了
    AMBIGUOUS = "ambiguous"  # 有候选但选不出来，交人工
    NO_CANDIDATE = "no_candidate"  # 高德根本没返回像样的东西
    UNRESOLVED = "unresolved"  # 上下文不足，无法构造查询（例如城市未知）

    @property
    def needs_human(self) -> bool:
        """是否需要进数据工作台。

        `UNRESOLVED` 也算：缺城市线索不是「没问题」，而是**还没法判断**，
        它同样会生成一条待办，只是原因不同——要补的是上下文，不是选候选。
        """
        return self is not AlignOutcome.ALIGNED


@dataclass(frozen=True)
class Mention:
    """提纯阶段产出的一条景点提及，加上它自带的上文线索。

    `name` 是原文里的叫法，可能是简称或俗称（「陕历博」「兵马俑」）。
    """

    name: str
    city_name: str
    city_adcode: str | None = None
    context: str | None = None
    document_id: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("提及名不能为空")
        if not self.city_name.strip():
            raise ValueError("提及必须带城市线索，否则无法限定搜索范围")


@dataclass(frozen=True)
class ScoredCandidate:
    """一个候选及其得分。得分要能解释，否则人工复核时无从判断。

    `name_score` 只量名称像不像，`rank_score` 才是用来选谁的分。
    两者分开是因为**排序还要看名称是上位还是下位**：实测搜「故宫博物院」时，
    `故宫博物院` 与 `故宫博物院-午门` 的名称得分都是 1.0，
    只按名称选就会选中午门，然后挂错一层（ADR-0009）。
    """

    poi: CandidatePoi
    name_score: float
    rank_score: float
    usable: bool  # 是否通过城市与类型门禁
    reject_reason: str | None = None
    rank_note: str | None = None


# 名称**就是**答案（完全相同，或作为完整一段跟在分隔符后）时的加成。
#
# 加满到 1.0 之上，是因为实测里同名的候选会互相咬住：
# 搜「钟楼」，`钟楼` 与 `大慈恩寺-钟楼` 都会得到 1.0 与 0.90；
# 搜「大唐不夜城」，`大唐不夜城` 与 `穿越大唐不夜城` 只差 0.12。
# 不把「完全同名」这个信号拉开，它就会被包含关系淹没。
_EXACT_NAME_BONUS = 0.20

# 一方**包含**另一方（但不等同）时的加成。
# 实测「兵马俑」要靠它才能对上「秦始皇兵马俑博物馆」——两者的
# SequenceMatcher 比例只有 0.90，而答案是「秦始皇帝陵博物院」，
# 字面重合度为零，别名能力在高德那边而不在字符串里。
_CONTAINS_BONUS = 0.12


@dataclass(frozen=True)
class AlignResult:
    """一次对齐的完整结论。

    `resolved` 是最终认定的**本体** POI；`matched` 是名称真正命中的那个候选。
    搜「午门」时 `matched` 是 `故宫博物院-午门`，而 `resolved` 是
    `故宫博物院`——两者都留着，人工复核时能看懂为什么这么归（ADR-0009）。
    """

    mention: Mention
    outcome: AlignOutcome
    resolved: CandidatePoi | None = None
    matched: CandidatePoi | None = None
    candidates: tuple[ScoredCandidate, ...] = ()
    reason: str = ""

    @property
    def resolved_poi_id(self) -> str | None:
        return self.resolved.poi_id if self.resolved else None

    @property
    def collapsed_from_sub(self) -> bool:
        """是否发生了「命中子点、归并到本体」。界面要标注这件事。"""
        return (
            self.matched is not None
            and self.resolved is not None
            and self.matched.poi_id != self.resolved.poi_id
        )


def name_score(mention: str, candidate: str) -> float:
    """提及名与候选名的相近程度，0 到 1。

    规则按可靠性降序，先命中先返回：

    - 完全相同 → 1.0
    - 去掉「公园 / 景区」这类通名后缀后相同 → 0.95（搜「景山」命中「景山公园」）
    - 整体包含另一方 → 0.90（搜「故宫」命中「故宫博物院」）
    - 出现在分隔符之后 → 0.90（搜「午门」命中「故宫博物院-午门」）
    - 前缀命中且够长 → 0.85（搜「大唐不夜城」命中「大唐不夜城(地铁站)」）
    - 否则按字符最长公共子序列比例给分

    分隔符那一条是必需的，不能靠整体包含兜住：高德给景点内部子点命名的方式
    就是「本体-子点」，「午门」只有两个字，按整体包含判断的话短词阈值会把
    它挡在门外，而它恰恰是最需要正确归并的一类提及（ADR-0009）。
    """
    left = mention.strip()
    right = candidate.strip()
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    if _strip_generic_suffix(left) == _strip_generic_suffix(right):
        return 0.95

    if left in right or right in left:
        shorter = min(len(left), len(right))
        # 短词的整体包含没有意义：「西安」出现在几百个候选名里。
        # 除了分隔符那一种情况，下面单独处理。
        if shorter >= 3:
            return 0.90

    if _follows_separator(left, right) or _follows_separator(right, left):
        return 0.90

    if (right.startswith(left) or left.startswith(right)) and min(len(left), len(right)) >= 4:
        return 0.85

    ratio = SequenceMatcher(None, left, right, autojunk=False).ratio()
    return round(ratio * 0.8, 4)


# 高德给「本体内部的一个点」命名时用的连接符：`故宫博物院-午门`。
_SEPARATORS = ("-", "－", "—", "·", "(", "（", "_")


def _follows_separator(needle: str, haystack: str) -> bool:
    """`needle` 是否作为 `haystack` 里一个完整的段落在分隔符之后出现。"""
    if not needle or len(needle) > len(haystack):
        return False
    for separator in _SEPARATORS:
        index = haystack.find(separator + needle)
        if index < 0:
            continue
        end = index + len(separator) + len(needle)
        # 段末必须也是结尾或又一个分隔符，否则「午门」会命中「午门西卫生间」
        if end == len(haystack) or haystack[end] in _SEPARATORS:
            return True
    return False


_GENERIC_SUFFIXES = ("风景区", "博物院", "博物馆", "公园", "景区", "遗址", "故里", "古镇", "广场")


def _strip_generic_suffix(name: str) -> str:
    for suffix in _GENERIC_SUFFIXES:
        if len(name) > len(suffix) + 1 and name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _rank_bonus(mention: str, name: str) -> tuple[float, str | None]:
    """名称关系上的加成：这个候选与提及是同一个名字，还是只是被它包含。

    实测依据（docs/M3-PROBE.md 第 3.3 节）：真实候选里同一处地方会有
    好几个名称相近的条目，`故宫博物院` / `故宫博物院-午门` /
    `故宫博物院-午门西卫生间` 分别是 1.0、0.90、0.90。不把「完全相同」
    与「被包含」拉开，选出来的就会是其中一个子点。

    返回（加成, 说明）。说明会进人工复核的界面，让人看得懂为什么这么排。
    """
    left = mention.strip()
    right = name.strip()
    if not left or not right:
        return 0.0, None

    if left == right:
        return _EXACT_NAME_BONUS, "名称完全相同"
    if _follows_separator(left, right):
        return _EXACT_NAME_BONUS, "名称在分隔符后完整出现"

    # 包含关系：两边都要够长才有意义。
    # 「钟楼」出现在「临潼奥莱钟楼广场」里，但那是另一个地方；
    # 只有像「兵马俑」对「秦始皇兵马俑博物馆」这样、被包含的那一段
    # 本身就是一个完整名称时，包含关系才是正信号。
    if len(left) >= 3 and left in right:
        return _CONTAINS_BONUS, "名称完整出现在候选名里"
    if len(right) >= 3 and right in left:
        return _CONTAINS_BONUS, "候选名完整出现在名称里"
    return 0.0, None


def score_candidates(
    mention: Mention,
    candidates: list[CandidatePoi] | tuple[CandidatePoi, ...],
) -> list[ScoredCandidate]:
    """给候选打分并标记门禁结果。返回按排序分降序、同分保持原顺序的列表。

    打分只做两件与层级无关的事：名称像不像，以及名称关系是上位还是下位。
    层级（本体 / 子点）的处置在 `align()` 里，因为判断层级要先消除重复实体。
    """
    scored: list[ScoredCandidate] = []

    for poi in candidates:
        score = name_score(mention.name, poi.name)
        bonus, note = _rank_bonus(mention.name, poi.name)
        reason: str | None = None

        if not poi.in_city(mention.city_adcode):
            reason = f"不在{mention.city_name}（候选 adcode {poi.adcode or '缺失'}）"
        elif poi.kind is PoiKind.IRRELEVANT:
            # 名称判据优先展示：它比六位类型码好读，人工复核时一眼能看懂。
            # 但**类型码仍然要独立挡住**——只按名称判断的话，
            # 「故宫博物院检票处」这种会被放过，因为「检票处」不在名称标记里。
            if poi.name and any(mark in poi.name for mark in NOT_A_DESTINATION_MARKS):
                reason = f"不是游览对象（{poi.name}）"
            else:
                reason = f"类型不是目的地（{poi.type_name or poi.typecode or '未知'}）"

        scored.append(
            ScoredCandidate(
                poi=poi,
                name_score=score,
                # 刻意**不夹到 1.0**。加成的作用是把「完全相同」与「被包含」
                # 分开，一夹上界两者就都变成 1.0、差值恒为 0，
                # 于是「前两名咬得紧」这条规则会在每一处都触发。
                # 排序分超过 1 不代表名称相似度超过 1，它只是排序用的。
                rank_score=round(score + bonus, 4),
                usable=reason is None,
                reject_reason=reason,
                rank_note=note,
            )
        )

    # 同分时保持高德给的顺序：`sorted` 是稳定排序，所以不加第三键。
    # 换成别的排序方式（例如按 poi_id）等于用我们臆想的顺序覆盖高德的排序信号。
    scored.sort(key=lambda item: -item.rank_score)
    return _demote_containment(scored)


# 存在完全同名候选时，给「只是被包含」的候选降的权。
#
# 依据是实测：搜「钟楼」，第一名是 `钟楼`（西安钟楼，完全同名），
# 第二名 `大慈恩寺-钟楼` 却因为包含关系拿到 1.02，与同名候选只差 0.10；
# 搜「珍宝馆」，`故宫博物院-珍宝馆` 与 `国学艺术珍宝馆` 差 0.08。
# 两处都是同一个原因——**当答案就摆在眼前时，一个碰巧含有同样字眼的名字
# 不该跟它并列**。降权之后这两条都能干脆地选出正确答案。
_CONTAINMENT_DEMOTION = 0.12

_CONTAINS_NOTES = ("名称完整出现在候选名里", "候选名完整出现在名称里")
_EXACT_NOTES = ("名称完全相同", "名称在分隔符后完整出现")


def _demote_containment(scored: list[ScoredCandidate]) -> list[ScoredCandidate]:
    """有完全同名候选时，把只靠包含关系得分的候选降下来。

    没有完全同名候选时**什么都不做**——那时包含关系是唯一可用的信号，
    正是它让俗称「兵马俑」对上「秦始皇兵马俑博物馆」。
    """
    usable = [item for item in scored if item.usable]
    if not any(item.rank_note in _EXACT_NOTES for item in usable):
        return scored

    return [
        ScoredCandidate(
            poi=item.poi,
            name_score=item.name_score,
            rank_score=round(item.rank_score - _CONTAINMENT_DEMOTION, 4),
            usable=item.usable,
            reject_reason=item.reject_reason,
            rank_note=f"{item.rank_note}（已有同名候选，降权）",
        )
        if item.rank_note in _CONTAINS_NOTES
        else item
        for item in scored
    ]


@dataclass(frozen=True)
class _Entity:
    """一个**本体**以及指向它的那批候选。

    搜索结果是「一组实体」，不是一个候选列表：实测搜「兵马俑」，
    十条结果里六条最终属于同一个本体（秦始皇帝陵博物院）。
    把重复实体算成多个候选，会让「前两名咬得紧就交人工」这条规则
    在同一个地方反复触发。

    `root` 是**本体对应的那个候选对象**（可能是这批里的任何一个，
    不一定是最像的那个），不在候选集里时为 None——此时只能停在最像的候选上，
    并让调用方从 `resolved` 与本体的差异看出来链条断了。
    """

    root_id: str
    best: ScoredCandidate
    members: tuple[ScoredCandidate, ...]
    root: CandidatePoi | None = None


def _consolidate(
    scored: list[ScoredCandidate],
    root_of: Mapping[str, str],
) -> list[_Entity]:
    """按本体合并候选，返回排序后的实体列表。

    这一步同时解决两件事：

    1. **重复实体**：同一个本体的多个候选算一个，取名称最像的那个代表
    2. **本体与子点之间的选择**：靠 `rank_score` 里的名称关系加成，
       本体（名称更短、更上位）自然排在子点前面
    """
    groups: dict[str, list[ScoredCandidate]] = {}
    for item in scored:
        if not item.usable:
            continue
        groups.setdefault(root_of.get(item.poi.poi_id, item.poi.poi_id), []).append(item)

    entities: list[_Entity] = []
    for root_id, members in groups.items():
        members.sort(key=lambda item: -item.rank_score)
        best = members[0]
        # 本体自己可能**不是**这批里最像名称的那个：「午门」那一组里
        # 名称最像的是子点，而本体是 `故宫博物院`。所以要显式找出来，
        # 不能拿 best 当本体。
        root = next((item.poi for item in members if item.poi.poi_id == root_id), None)
        entities.append(
            _Entity(
                root_id=root_id,
                best=best,
                members=tuple(members),
                root=root,
            )
        )

    entities.sort(key=lambda entity: -entity.best.rank_score)
    return entities


def align(
    mention: Mention,
    candidates: list[CandidatePoi] | tuple[CandidatePoi, ...],
    *,
    lineage: CandidateSet | None = None,
) -> AlignResult:
    """给一条提及在候选里找答案。

    `lineage` 是补全过的祖先谱系（`lushu/domain/lineage.py`）。
    不传时退化为「只用候选自身带的 `parent`」——实测这样会让六个子点
    都被当成独立本体、互相咬住选不出来，所以正式路径一定要传。
    """
    if not mention.city_adcode:
        return AlignResult(
            mention=mention,
            outcome=AlignOutcome.UNRESOLVED,
            reason="缺少城市 adcode，无法校验候选是否跨城",
        )

    candidate_list = tuple(candidates)
    roots = lineage or CandidateSet.from_candidates(candidate_list)
    by_id = {poi.poi_id: poi for poi in candidate_list}

    scored = score_candidates(mention, candidate_list)
    root_of = {poi.poi_id: roots.root_of(poi.poi_id) for poi in candidate_list}
    entities = _consolidate(scored, root_of)

    if not entities:
        rejected = [item.reject_reason for item in scored[:3] if item.reject_reason]
        detail = "；".join(reason for reason in rejected if reason) or "高德没有返回候选"
        return AlignResult(
            mention=mention,
            outcome=AlignOutcome.NO_CANDIDATE,
            candidates=tuple(scored),
            reason=f"没有可用候选：{detail}",
        )

    best = entities[0]

    # ── 名称不像的时候怎么判 ────────────────────────────────────
    #
    # 俗称与官方名的字面重合度可以低到零（「兵马俑」对
    # 「秦始皇帝陵博物院」是 0.00），别名能力在高德那边而不在字符串里。
    #
    # 所以只有**一个实体**可选时相信高德的排序；有多个实体时交人工——
    # 实测搜「袁家村」限定西安，前五全是西安市区的连锁中餐馆，
    # 名称完全一致而城市完全错误，多个实体之间硬选就是这种错误。
    if best.best.rank_score < MATCH_SCORE_THRESHOLD:
        if len(entities) == 1 and best.best.rank_score >= SINGLE_ENTITY_FLOOR:
            return _accept(
                mention, best, scored, roots, by_id,
                note="唯一候选，按高德排序采信",
            )
        runner_up = entities[1] if len(entities) > 1 else None
        detail = (
            f"，且还有 {len(entities) - 1} 个可用实体"
            f"（如「{runner_up.best.poi.name}」）" if runner_up else ""
        )
        return AlignResult(
            mention=mention,
            outcome=AlignOutcome.NO_CANDIDATE,
            candidates=tuple(scored),
            reason=(
                f"最高候选「{best.best.poi.name}」名称得分 {best.best.name_score:.2f}，"
                f"低于 {MATCH_SCORE_THRESHOLD}{detail}，无法确定指的是哪一个"
            ),
        )

    # 前两名咬得太紧就不选。同一个本体的候选已经在上一步合并掉了，
    # 所以这里咬住的确实是两个**不同的地方**。
    if len(entities) > 1:
        runner_up = entities[1]
        gap = best.best.rank_score - runner_up.best.rank_score
        if gap < AMBIGUITY_MARGIN:
            return AlignResult(
                mention=mention,
                outcome=AlignOutcome.AMBIGUOUS,
                candidates=tuple(scored),
                reason=(
                    f"「{best.best.poi.name}」与「{runner_up.best.poi.name}」"
                    f"得分相差 {gap:.2f}，不足 {AMBIGUITY_MARGIN}，"
                    "无法判断指的是哪一个"
                ),
            )

    return _accept(mention, best, scored, roots, by_id)


def _accept(
    mention: Mention,
    entity: _Entity,
    scored: list[ScoredCandidate],
    roots: CandidateSet,
    by_id: dict[str, CandidatePoi],
    *,
    note: str = "",
) -> AlignResult:
    """认定一个实体，并落到它的本体上（ADR-0009）。"""
    # 本体优先从谱系里取，而不是只看候选集。实测这一步很关键：
    # 搜「午门」时 `故宫博物院` 根本不在搜索结果里，
    # 只看候选集就只能停在子点「故宫博物院-午门」上。
    resolved = roots.owner_of(entity.best.poi.poi_id)
    if resolved is None:
        resolved = by_id.get(entity.root_id) or entity.best.poi

    detail = f"名称得分 {entity.best.name_score:.2f}"
    if note:
        detail += f"（{note}）"
    if entity.best.rank_note:
        detail += f"（{entity.best.rank_note}）"
    if resolved.poi_id != entity.best.poi.poi_id:
        detail += f"，由子点「{entity.best.poi.name}」归并到本体「{resolved.name}」"
    if len(entity.members) > 1:
        detail += f"，另有 {len(entity.members) - 1} 条候选属于同一本体"

    return AlignResult(
        mention=mention,
        outcome=AlignOutcome.ALIGNED,
        resolved=resolved,
        matched=entity.best.poi,
        candidates=tuple(scored),
        reason=detail,
    )


