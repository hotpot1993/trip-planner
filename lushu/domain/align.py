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

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

# 高德 POI 的 typecode 是六位分层编码，前两位是门类。
# 取值依据：`风景名胜`(11)、`科教文化服务`(14)、`体育休闲服务`(08)。
# 停车场是 1509xx、公交站 1507xx、地铁站 1505xx、公厕是 20xxxx，
# 它们会大量挤进结果里（搜「故宫」第二位就是「故宫博物院检票处」），
# 必须显式排除，而不是靠名称过滤。
_DESTINATION_PREFIXES = ("11", "14", "08")

# 「只是个地名」：道路名、桥梁、热点地名、行政地名。
# 它们可能确实值得去（回民街、洒金桥），但没有门票、没有开放时间，
# 也不该占行程里的一个天项，所以单列一类由调用方决定怎么用。
_PLACE_NAME_PREFIXES = ("19",)

# 明确「不是目的地」的类型前缀。停车场与公交站属交通设施服务(15)，
# 售票处与咨询中心属生活服务(07)，公厕属公共设施(20)，商场属商务住宅(12)。
_EXCLUDED_PREFIXES = ("15", "07", "20", "12", "13", "16", "17", "18")

# 购物服务(06)整体不算目的地——商场、专卖店、超市都在里面——但
# **特色商业街**是例外：实测回民街的 typecode 是 060101，而它确实值得去。
# 只按「购物服务一律排除」会把它丢掉，所以这一条要单独判。
_DESTINATION_SHOPPING_TYPECODE = "060101"
_DESTINATION_SHOPPING_MARK = "特色商业街"

# 名称得分达到这个值才认为「就是它」。
MATCH_SCORE_THRESHOLD = 0.80

# 前两名得分差小于这个值就交人工。名称相近的候选之间，高德排序没有权威性，
# 我们也没有别的判据，硬选一个不如让数据工作台问一句。
AMBIGUITY_MARGIN = 0.10


class PoiKind(StrEnum):
    """POI 在行程里的地位。"""

    DESTINATION = "destination"  # 值得去的地方：可以占一个天项
    PLACE_NAME = "place_name"  # 只是个地名：街区、道路、桥
    IRRELEVANT = "irrelevant"  # 停车场、公交站、公厕、商店：不该进候选


class PoiLevel(StrEnum):
    """POI 在本体—子点层级里的位置。"""

    ROOT = "root"  # 本体，parent 为空
    SUB = "sub"  # 子点，parent 非空


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


def classify_poi(typecode: str | None, type_name: str | None = None) -> PoiKind:
    """按高德类型判 POI 在行程里的地位。

    先看 typecode（六位数字，稳定），typecode 缺失时退化到中文类型串。
    实测现有 poi 表的 typecode 全是空的（见 docs/M3-PROBE.md 第四节），
    所以退化路径不是摆设。
    """
    code = (typecode or "").strip()
    if code:
        if code.startswith(_PLACE_NAME_PREFIXES):
            return PoiKind.PLACE_NAME
        # 特色商业街要先于通用前缀判断，否则会被 06 的排除规则吃掉
        if code == _DESTINATION_SHOPPING_TYPECODE and _DESTINATION_SHOPPING_MARK in (
            type_name or ""
        ):
            return PoiKind.DESTINATION
        if code.startswith(_EXCLUDED_PREFIXES) or code.startswith("06"):
            return PoiKind.IRRELEVANT
        if code.startswith(_DESTINATION_PREFIXES):
            return PoiKind.DESTINATION
        return PoiKind.IRRELEVANT

    text = (type_name or "").strip()
    if not text:
        return PoiKind.IRRELEVANT
    if text.startswith("地名地址信息"):
        return PoiKind.PLACE_NAME
    if _DESTINATION_SHOPPING_MARK in text:
        return PoiKind.DESTINATION
    if text.startswith(("风景名胜", "科教文化服务", "体育休闲服务")):
        return PoiKind.DESTINATION
    return PoiKind.IRRELEVANT


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
class CandidatePoi:
    """高德返回的一个候选 POI。字段名对齐 `/v3/place/text` 的原始键名。"""

    poi_id: str
    name: str
    typecode: str | None = None
    type_name: str | None = None
    adcode: str | None = None
    city_name: str | None = None
    citycode: str | None = None
    address: str | None = None
    tel: str | None = None
    lng_gcj02: float | None = None
    lat_gcj02: float | None = None
    parent_id: str | None = None
    rating: float | None = None
    open_time: str | None = None
    photo_url: str | None = None
    raw_json: str | None = None

    def __post_init__(self) -> None:
        if not self.poi_id.strip():
            raise ValueError("候选 POI 必须有高德 id，它是本项目的实体主键（ADR-0002）")

    @property
    def kind(self) -> PoiKind:
        return classify_poi(self.typecode, self.type_name)

    @property
    def level(self) -> PoiLevel:
        return PoiLevel.SUB if (self.parent_id or "").strip() else PoiLevel.ROOT

    def in_city(self, city_adcode: str | None) -> bool:
        """是否落在目标城市内。

        只比前四位（市一级）。区县不同是正常的——「兵马俑」在临潼区，
        「陕西历史博物馆」在雁塔区，都属西安。
        """
        if not city_adcode:
            return True
        mine = (self.adcode or "").strip()
        if not mine:
            # 没有 adcode 的候选无法校验城市，按不通过处理。
            # 放过它就等于承认「袁家村在西安」这类跨城错误。
            return False
        return mine[:4] == city_adcode.strip()[:4]


@dataclass(frozen=True)
class ScoredCandidate:
    """一个候选及其得分。得分要能解释，否则人工复核时无从判断。"""

    poi: CandidatePoi
    name_score: float
    usable: bool  # 是否通过城市与类型门禁
    reject_reason: str | None = None


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


def score_candidates(
    mention: Mention,
    candidates: list[CandidatePoi] | tuple[CandidatePoi, ...],
) -> list[ScoredCandidate]:
    """给候选打分并标记门禁结果。返回按得分降序、同分按原名顺序的列表。"""
    scored: list[ScoredCandidate] = []

    for poi in candidates:
        score = name_score(mention.name, poi.name)
        reason: str | None = None

        if not poi.in_city(mention.city_adcode):
            reason = f"不在{mention.city_name}（候选 adcode {poi.adcode or '缺失'}）"
        elif poi.kind is PoiKind.IRRELEVANT:
            reason = f"类型不是目的地（{poi.typecode or poi.type_name or '未知'}）"

        scored.append(
            ScoredCandidate(
                poi=poi,
                name_score=score,
                usable=reason is None,
                reject_reason=reason,
            )
        )

    scored.sort(key=lambda item: -item.name_score)
    return scored


def _collapse_to_root(poi: CandidatePoi, by_id: dict[str, CandidatePoi]) -> CandidatePoi:
    """沿 parent 链走到本体。

    层级可以不止一层，实测的链是三跳：
    `秦始皇兵马俑博物馆第1停车场` → `秦始皇兵马俑博物馆` → `秦始皇帝陵博物院`，
    只有最后那个的 `parent` 是空的。所以归并要一直走到空为止，
    走一步不算归并。最多走 `_MAX_PARENT_HOPS` 步，防止数据里出现环时死循环。
    """
    seen: set[str] = set()
    current = poi
    for _ in range(_MAX_PARENT_HOPS):
        parent_id = (current.parent_id or "").strip()
        if not parent_id or parent_id in seen:
            break
        seen.add(parent_id)
        parent = by_id.get(parent_id)
        if parent is None:
            # 父节点不在本次候选里。这是一个真实的缺口：高德把本体排在
            # 结果之外时我们只知道「它属于某个东西」，却不知道那个东西是谁。
            # 此时不归并，留在子点上并让调用方看见 collapsed=False。
            break
        current = parent
    return current


_MAX_PARENT_HOPS = 8


def align(
    mention: Mention,
    candidates: list[CandidatePoi] | tuple[CandidatePoi, ...],
) -> AlignResult:
    """给一条提及在候选里找答案。"""
    if not mention.city_adcode:
        return AlignResult(
            mention=mention,
            outcome=AlignOutcome.UNRESOLVED,
            reason="缺少城市 adcode，无法校验候选是否跨城",
        )

    scored = score_candidates(mention, candidates)
    usable = [item for item in scored if item.usable]

    if not usable:
        rejected = [item.reject_reason for item in scored[:3] if item.reject_reason]
        detail = "；".join(reason for reason in rejected if reason) or "高德没有返回候选"
        return AlignResult(
            mention=mention,
            outcome=AlignOutcome.NO_CANDIDATE,
            candidates=tuple(scored),
            reason=f"没有可用候选：{detail}",
        )

    best = usable[0]
    if best.name_score < MATCH_SCORE_THRESHOLD:
        return AlignResult(
            mention=mention,
            outcome=AlignOutcome.NO_CANDIDATE,
            candidates=tuple(scored),
            reason=(
                f"最高候选「{best.poi.name}」名称得分 {best.name_score:.2f}，"
                f"低于 {MATCH_SCORE_THRESHOLD}"
            ),
        )

    # 前两名咬得太紧就不选。名称相近的候选之间高德排序没有权威性。
    if len(usable) > 1:
        runner_up = usable[1]
        if best.name_score - runner_up.name_score < AMBIGUITY_MARGIN:
            return AlignResult(
                mention=mention,
                outcome=AlignOutcome.AMBIGUOUS,
                candidates=tuple(scored),
                reason=(
                    f"「{best.poi.name}」与「{runner_up.poi.name}」得分相差不足 "
                    f"{AMBIGUITY_MARGIN}，无法判断指的是哪一个"
                ),
            )

    by_id = {poi.poi_id: poi for poi in candidates}
    root = _collapse_to_root(best.poi, by_id)

    return AlignResult(
        mention=mention,
        outcome=AlignOutcome.ALIGNED,
        resolved=root,
        matched=best.poi,
        candidates=tuple(scored),
        reason=(
            f"名称得分 {best.name_score:.2f}"
            + (f"，由子点「{best.poi.name}」归并到本体「{root.name}」"
               if root.poi_id != best.poi.poi_id else "")
        ),
    )
