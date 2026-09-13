"""给餐饮天项补坐标：路书里「这两处之间没有路段说明」的根因就在这里。

M6 交付时路书上有 7 处 `leg_missing`，**全部落在餐饮项上，而这 7 个餐饮项的
坐标恰好全是空的**。转换层其实已经修好了（`_convert_meal` 保留 facts、
迁移 10 让天项自己记坐标），但那只对**之后**生成的行程生效：迁移不回填历史数据。

这正是本项目反复踩到的那个坑：**修好算法不等于修好数据。**
`ls align recheck` 是为同一类问题写的，这一条是它的第二个实例。

判据是保守的：**只在能确定的时候补。**

- 路书的坐标不只是用来画点，它是**唯一的空间线索**（ADR-0006 没有地图）：
  「步行 800 米」这句话就是拿两个坐标算出来的。坐标错了那句话就是错的，
  而且错得看不出来——比缺一句话更糟。
- 餐饮名有大量是**通用菜名**（「鸭血粉丝汤」「临潼石榴汁与 biangbiang 面」），
  高德搜出来的可能是另一家店。补一个错的比重空着更坏。

所以判据的主力是**「有没有并列第一」**：同名同分的店有两家，就是不唯一，不猜。
其余一律留空，并把**为什么没补上**带出来——留空的原因也是结论，
否则下次还得从头查一遍。

默认只算不写（`--apply` 才落库），与 `ls verify scan` 同一个规矩。

**补充（真机跑过之后）**：这条路只补回来一个坐标，路书的 7 处空洞一个都没少。
原因不在判据，在**问题的形状**：一段路要两个端点都有坐标才算得出来，
而 6 个餐饮名在城市范围的按名搜索里根本定不下来——它们是通用菜名
（「老孙家泡馍」搜出 4 家同分）或没有分店信息的品牌名（「绿柳居」8 家同分）。

真正能定下来的是**附近**那一层：这些店名本来就是引擎按当天景点「周边搜索」
拿到的（`meal_search_node` 用 `search_around_pois_async`），所以正确做法是
拿当天景点的坐标做周边搜索、再在结果里按名字挑——8 家同分的绿柳居，
在夫子庙附近只有一家。这一步还没有做（我们的 `adapters/poi.py` 目前只有
文本搜索与详情，没有周边搜索）。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from lushu.store.connection import connect

# 高德 QPS 限制实测会撞（`CUQPS_HAS_EXCEEDED_THE_LIMIT`），
# 每次搜索之间歇一下。与 `realign.survey` 同一个数。
PAUSE_SECONDS = 0.4

# 高德的一级类型码：05 是餐饮服务。typecode 缺失时退化到中文类型串
# （实测库里 typecode 常为空，见 docs/M3-PROBE.md 第四节）。
_FOOD_TYPECODE_PREFIX = "05"
_FOOD_TYPE_NAME_PREFIX = "餐饮"


@dataclass(frozen=True)
class MealSlot:
    """一个缺坐标的餐饮天项。"""

    item_id: str
    trip_id: str
    day_date: str
    title: str
    city_name: str
    city_adcode: str | None = None


@dataclass(frozen=True)
class MealFix:
    """一个餐饮天项的处置结果。

    `resolved` 为假时 `reason` 就是**没补上的原因**，同样是结论——
    「查过了，搜不到」与「还没查」必须分得开。
    """

    slot: MealSlot
    matched_name: str = ""
    lat_gcj02: float | None = None
    lng_gcj02: float | None = None
    address: str | None = None
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.lat_gcj02 is not None and self.lng_gcj02 is not None


@dataclass
class FillReport:
    fixes: list[MealFix] = field(default_factory=list)

    @property
    def resolved(self) -> list[MealFix]:
        return [fix for fix in self.fixes if fix.resolved]

    @property
    def unresolved(self) -> list[MealFix]:
        return [fix for fix in self.fixes if not fix.resolved]


def pending_meals(
    *, conn: sqlite3.Connection | None = None, trip_id: str | None = None
) -> list[MealSlot]:
    """缺坐标的餐饮天项。已经填过的不再出现——重跑是幂等的。"""
    owned = conn is None
    active = conn or connect()
    try:
        rows = active.execute(
            "SELECT i.id, d.trip_id, d.date, i.title, cs.city_name, cs.city_adcode "
            "FROM day_item i "
            "JOIN day d ON d.id = i.day_id "
            "JOIN city_stay cs ON cs.id = d.city_stay_id "
            "WHERE i.kind = 'meal' AND i.lat_gcj02 IS NULL "
            "  AND (? IS NULL OR d.trip_id = ?) "
            "ORDER BY d.date, i.seq",
            (trip_id, trip_id),
        ).fetchall()
    finally:
        if owned:
            active.close()
    return [
        MealSlot(
            item_id=row["id"],
            trip_id=row["trip_id"],
            day_date=row["date"],
            title=row["title"],
            city_name=row["city_name"] or "",
            city_adcode=row["city_adcode"],
        )
        for row in rows
    ]


def resolve_meal(slot: MealSlot, *, search) -> MealFix:
    """把一个餐饮名对到高德的店上，只取**分得清**的那一种。

    **不能用 `domain/align.py` 那套。** 它回答的是「这是哪个景点」，为此有一道
    类型门禁：`poi.kind is IRRELEVANT` 直接拒绝——而餐厅按 `classify_poi`
    恰好就是 IRRELEVANT（这是对的：候选池里出现饭馆是噪声）。拿它来对餐厅，
    每一个都会被判「类型不是目的地」，一句都补不上。

    这两个问题的要求本来就不同：景点要的是**唯一实体**（ADR-0002，一个地方
    一个 id），餐厅不进实体表，只需要「这家店在哪」。所以这里另立判据，
    但**沿用它的名称分**（`name_score`）与同一个及格线
    （`MATCH_SCORE_THRESHOLD`）：名字像不像是一回事，没必要有两套。

    四道关，缺一不可：

    1. **同城**（adcode 前四位）——搜到别的城市的同名店是最危险的一种错，
       坐标差几百公里，而名字一模一样。
    2. **是吃饭的地方**（高德一级类型码 05）。
    3. **名字够像**（`name_score` ≥ 及格线）。
    4. **没有并列第一**——并列就是不唯一，不猜。通用菜名（「鸭血粉丝汤」）
       会搜出一串同名小店，全部并列，于是留空。**这条是这套判据的主力。**
    """
    from lushu.domain.align import MATCH_SCORE_THRESHOLD, name_score

    if not slot.title.strip():
        return MealFix(slot, reason="这一项没有名字，无从查起")
    if not slot.city_name.strip():
        return MealFix(slot, reason="这一项没有城市线索，无法限定搜索范围")
    if not slot.city_adcode:
        return MealFix(slot, reason=f"「{slot.city_name}」还没有 adcode，先让这座城市入库")
    if not _looks_like_a_shop(slot.title):
        return MealFix(
            slot,
            reason=f"「{slot.title}」不像店名（是一串菜名），不去猜——猜错就是导航到别处",
        )

    try:
        found = search(slot.title, slot.city_name)
    except Exception as exc:  # 网络、额度、限流：都不是「查不到」
        return MealFix(slot, reason=f"高德搜索失败：{exc}")

    if not found.candidates:
        return MealFix(slot, reason="高德没有搜到这个名字（通用菜名常常如此）")

    in_city = [poi for poi in found.candidates if poi.in_city(slot.city_adcode)]
    if not in_city:
        return MealFix(slot, reason=f"搜到的都不在{slot.city_name}，不能拿别的城市的同名店顶上")

    scored = sorted(
        ((name_score(slot.title, poi.name), poi) for poi in in_city if _is_food(poi)),
        key=lambda pair: -pair[0],
    )
    if not scored:
        return MealFix(slot, reason="搜到的都不是吃饭的地方")

    best_score, best = scored[0]
    if best_score < MATCH_SCORE_THRESHOLD:
        return MealFix(
            slot,
            matched_name=best.name,
            reason=f"搜到的最像的是「{best.name}」，但名字差得远（{best_score}）",
        )
    ties = [poi.name for score, poi in scored if score == best_score]
    if len(ties) > 1:
        return MealFix(
            slot,
            matched_name=ties[0],
            reason=f"有 {len(ties)} 家同分的店（「{ties[0]}」等），分不出是哪一家——"
            "通用菜名基本都会卡在这里",
        )

    if best.lat_gcj02 is None or best.lng_gcj02 is None:
        return MealFix(slot, matched_name=best.name, reason=f"对上了「{best.name}」，但高德没给坐标")
    return MealFix(
        slot,
        matched_name=best.name,
        lat_gcj02=best.lat_gcj02,
        lng_gcj02=best.lng_gcj02,
        address=best.address,
        reason=f"名字命中（{best_score}）",
    )


def _is_food(poi) -> bool:
    """这家店是不是吃饭的地方。

    刻意**不复用** `classify_poi`：那个函数回答的是「该不该进景点候选池」，
    对它来说餐厅一律 IRRELEVANT。把它的结论反过来用（「不是景点就是饭馆」）
    会把停车场、公厕、商店一起放进来。这里问的是另一个问题，就得另写判据。
    """
    code = (poi.typecode or "").strip()
    if code:
        return code.startswith(_FOOD_TYPECODE_PREFIX)
    return (poi.type_name or "").strip().startswith(_FOOD_TYPE_NAME_PREFIX)


# 出现在名字里就说明这多半**不是店名**，而是一串菜名或一句描述。
#
# 这条是实测撞出来的：真实行程里有一项叫「临潼石榴汁与 biangbiang 面」——
# 那是兵马俑门口吃的东西，不是一家店。它按包含关系命中了「biangbiang面」
# 这家店（`right in left` → 0.90，过了及格线），而**那家店在城北未央路，
# 离兵马俑 35 公里**。坐标写进去，路书会让人从兵马俑「走 35 公里」去吃面。
#
# 「和」刻意不在这一列：店名里带「和」的很常见（永和豆浆、和记），
# 宁可漏掉几个，也不能把「永和豆浆」判成菜名。
_NOT_A_SHOP_MARKS = ("与", "、", "＋", "+", "/")


def _looks_like_a_shop(name: str) -> bool:
    """这串字像不像店名。"""
    return not any(mark in name for mark in _NOT_A_SHOP_MARKS)


def plan_fill(
    slots: Sequence[MealSlot],
    *,
    search,
    pause: float = PAUSE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> FillReport:
    """把每个餐饮项查一遍。只算不改。

    `pause` / `sleep` 可注入：测试里不该真的等（同 `realign.survey`）。
    """
    fixes: list[MealFix] = []
    for index, slot in enumerate(slots):
        if index and pause:
            sleep(pause)
        fixes.append(resolve_meal(slot, search=search))
    return FillReport(fixes=fixes)


def apply_fixes(
    fixes: Sequence[MealFix], *, conn: sqlite3.Connection | None = None
) -> list[str]:
    """把查到的坐标写进天项，返回改动的天项 id。

    **只写坐标与地址，不写 `poi_id`**：那会让餐厅出现在实体表、候选池与
    结论里（与 `_convert_meal` 当初丢掉坐标是同一个取舍）。

    条件里带着 `lat_gcj02 IS NULL`：别人在这中间补过就不再覆盖——
    一次重跑不该把人工修正冲掉。

    只改没填的，返回值是 id 列表而不是个数：**改了什么要能核对**。
    """
    owned = conn is None
    active = conn or connect()
    changed: list[str] = []
    try:
        for fix in fixes:
            if not fix.resolved:
                continue
            cursor = active.execute(
                "UPDATE day_item SET lat_gcj02 = ?, lng_gcj02 = ?, address = COALESCE(address, ?) "
                "WHERE id = ? AND lat_gcj02 IS NULL",
                (fix.lat_gcj02, fix.lng_gcj02, fix.address, fix.slot.item_id),
            )
            if cursor.rowcount:
                changed.append(fix.slot.item_id)
        active.commit()
        return changed
    finally:
        if owned:
            active.close()
