"""高德 POI 的领域模型。

单独一个模块而不是塞进 `align.py`，是因为 `lineage.py` 要用 `CandidatePoi`，
而 `align.py` 要用 `CandidateSet`——放在一起就成环了。
这里的类型也确实是「实体」而不是「对齐」的一部分：POI 在整个项目里
的身份、层级与类型分类，与怎么把它对齐到一条提及是两件事。

分类规则全部来自实测（docs/M3-PROBE.md 第三节）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class PoiKind(StrEnum):
    """POI 在行程里的地位。"""

    DESTINATION = "destination"  # 值得去的地方：可以占一个天项
    PLACE_NAME = "place_name"  # 只是个地名：街区、道路、桥
    IRRELEVANT = "irrelevant"  # 停车场、公交站、公厕、商店：不该进候选


class PoiLevel(StrEnum):
    """POI 在本体—子点层级里的位置。"""

    ROOT = "root"  # 本体，parent 为空
    SUB = "sub"  # 子点，parent 非空


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

# 明确「不是目的地」的类型前缀。
#   15 交通设施服务（停车场、公交站、地铁站）
#   07 生活服务（售票处、咨询中心、检票处）
#   20 公共设施（公厕）
#   12 商务住宅   13 政府机构   16 金融保险   17 公司企业   18 道路附属
_EXCLUDED_PREFIXES = ("15", "07", "20", "12", "13", "16", "17", "18")

# 购物服务(06)整体不算目的地——商场、专卖店、超市都在里面——但有一个例外：
# 实测回民街的 typecode 是 061001（购物服务;特色商业街;步行街），
# 而它确实值得去。按「购物服务一律排除」会把它丢掉。
_DESTINATION_SHOPPING_TYPECODES = ("061001",)
_DESTINATION_SHOPPING_MARK = "特色商业街"

# 名称里带这些字样的，即使 typecode 归在风景名胜里也不是游览对象。
# 实测：「兵马俑直通车乘车点」的 typecode 是 110000（风景名胜），
# 名称里也有「兵马俑」，只按码判断会把它当候选——而它是个上车点。
#
# 公开（不带下划线）是因为 `align.py` 要用同一份清单**解释**拒绝理由：
# 「故宫博物院检票处」按类型码该拒，但把它的名字读给人工听更直观。
NOT_A_DESTINATION_MARKS = ("乘车点", "上车点", "售票处", "检票处", "停车场", "卫生间", "厕所")


def _starts_with(name: str, prefixes: tuple[str, ...]) -> bool:
    return name.startswith(prefixes)


def classify_poi(
    typecode: str | None,
    type_name: str | None = None,
    name: str | None = None,
) -> PoiKind:
    """按高德类型判 POI 在行程里的地位。

    先看 typecode（六位数字，稳定），typecode 缺失时退化到中文类型串。
    实测现有 poi 表的 typecode 全是空的（见 docs/M3-PROBE.md 第四节），
    所以退化路径不是摆设。

    `name` 参与判断是因为实测发现**类型码本身不够**：「兵马俑直通车乘车点」
    的 typecode 是 `110000`（风景名胜），名称里也有「兵马俑」，
    只按码判断会把它当成候选，而它是个上车点。
    """
    if name:
        for mark in NOT_A_DESTINATION_MARKS:
            if mark in name:
                return PoiKind.IRRELEVANT

    code = (typecode or "").strip()
    if code:
        if _starts_with(code, _PLACE_NAME_PREFIXES):
            return PoiKind.PLACE_NAME
        if code in _DESTINATION_SHOPPING_TYPECODES:
            return PoiKind.DESTINATION
        if _starts_with(code, _EXCLUDED_PREFIXES) or code.startswith("06"):
            return PoiKind.IRRELEVANT
        if _starts_with(code, _DESTINATION_PREFIXES):
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
        return classify_poi(self.typecode, self.type_name, self.name)

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


# ─── 同一处地方的两个实体 ────────────────────────────────────────
#
# ADR-0002 说高德是本项目实体真源，一个地方只有一个 POI id。但 M1/M2 落库时
# 用的是引擎从规划结果里回填的 id，M3 有了自己的适配器之后又按高德搜了一遍，
# 于是同一个地方在库里有了两行——实测真实库里有五处（西安城墙、兵马俑、
# 陕历博、南京博物院、夫子庙）。
#
# 这不是洁癖问题：预约规则挂在新实体上，而旧行程的天项指向旧实体，
# 于是**规则永远匹配不到那些行程**；M5 的候选池也一样会落空。
# 所以需要能认出「这两行是同一个地方」，并决定留哪一个。


@dataclass(frozen=True)
class PoiIdentity:
    """`poi` 表里的一行，只取判断「谁是实体真源」用得着的字段。"""

    poi_id: str
    name: str
    typecode: str | None = None
    type_name: str | None = None
    adcode: str | None = None
    address: str | None = None
    parent_id: str | None = None
    rating: float | None = None
    open_time: str | None = None
    photo_url: str | None = None
    raw_json: str | None = None
    fetched_at: str | None = None
    references: int = 0  # 有多少行指着它（天项、结论、规则……）

    @property
    def looks_like_amap(self) -> bool:
        """这一行的 id 像不像高德给的。

        高德的 POI id 以 `B` 开头（`B000A8UIN8`）。引擎早期回填的是 `X` 开头的
        自造 id（`X000A8UIN2`）——那正是需要被合并掉的那种。
        """
        return self.poi_id.startswith("B")

    @property
    def has_amap_detail(self) -> bool:
        """有没有高德那边的细节。`typecode` 是自有适配器写进去的（M3），
        它非空说明这一行是**真的查过高德**，而不只是引擎回填的一个名字。"""
        return bool((self.typecode or "").strip())


def pick_survivor(rows: Sequence[PoiIdentity]) -> PoiIdentity | None:
    """同一处地方的两行里，留哪一个。

    判据按可靠性降序，先满足的先赢：

    1. **有高德细节的**（`typecode` 非空）。它证明这一行是真查过高德的，
       而不是引擎从规划结果里回填的一个名字。
    2. **id 像高德的**（`B` 开头）。ADR-0002 要求实体主键是高德 id。
    3. **被引用得多的**。合并要改引用，改得少的那次风险小。
    4. id 字典序，只为让结果稳定可复现——同样的输入每次必须选出同一个赢家，
       否则合并「跑两次结果不一样」这种事根本没法查。
    """
    if not rows:
        return None
    return sorted(
        rows,
        key=lambda row: (
            not row.has_amap_detail,
            not row.looks_like_amap,
            -row.references,
            row.poi_id,
        ),
    )[0]
