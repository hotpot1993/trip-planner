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

**真机跑过的账**（真实行程，7 个餐饮项）：

| 做法 | 结果 |
|---|---|
| 只按城市按名搜 | 查到 1 个（鸭血粉丝汤），6 个定不下来，路书空洞 7 处一个没少 |
| 加上「绕着当天景点搜」 | 又查到 1 个（子午路张记肉夹馍，陕历博 1.8 公里外的分店），**空洞 7 → 6** |

剩下的 5 个是判据**故意**不补的，各有各的原因：绿柳居与老孙家泡馍在周边还是
多家同分（品牌名没带分店，本来就不该猜）、许记糕点最像的只有 0.34、
南京大牌档（中山陵店）在高德上叫「南京大牌档(中山陵金陵店)」、
「临潼石榴汁与 biangbiang 面」不是店名。

**一段路要两个端点都有坐标才算得出来**——所以补一个点常常不减少一处空洞。
剩下这 5 个要么人工指定（人知道说的是哪一家），要么就不补。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from lushu.adapters.poi import PoiSearchError
from lushu.store.connection import connect

# 高德 QPS 限制实测会撞（`CUQPS_HAS_EXCEEDED_THE_LIMIT`），
# 每次搜索之间歇一下。与 `realign.survey` 同一个数。
PAUSE_SECONDS = 0.4

# 高德的一级类型码：05 是餐饮服务。typecode 缺失时退化到中文类型串
# （实测库里 typecode 常为空，见 docs/M3-PROBE.md 第四节）。
_FOOD_TYPECODE_PREFIX = "05"
_FOOD_TYPE_NAME_PREFIX = "餐饮"


@dataclass(frozen=True)
class Anchor:
    """同一天里有坐标的一处（通常是景点）。

    周边搜索绕着它做：行程里的餐饮名本来就是引擎按当天景点周边搜出来的，
    所以它们的落点也在那一片。
    """

    item_id: str
    title: str
    lat_gcj02: float
    lng_gcj02: float


@dataclass(frozen=True)
class MealSlot:
    """一个缺坐标的餐饮天项。"""

    item_id: str
    trip_id: str
    day_date: str
    title: str
    city_name: str
    city_adcode: str | None = None
    anchors: tuple[Anchor, ...] = ()


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
    # 靠周边搜索定下来时，绕着哪一处找到的。**证据要跟着结论走**：
    # 「在中山陵周边找到」比一个坐标更能让人判断这次匹配对不对。
    near: str = ""

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
    """缺坐标的餐饮天项，连着它那天的锚点。已经填过的不再出现——重跑是幂等的。"""
    owned = conn is None
    active = conn or connect()
    try:
        # 坐标的取用顺序是「先实体、后天项」——与 `roadbook_service` 和
        # `trip_store._load_day` 同一条规矩（ADR-0001 的补充）。
        # **第一次写这里时只看了 day_item**，于是真实行程上一个锚点都取不到：
        # 迁移 10 才让天项自己记坐标，而历史行程的景点坐标一直在 poi 表里。
        # 表现是「周边搜索一次都没跑」，而原因看不出来。
        rows = active.execute(
            "SELECT i.id, d.id AS day_id, d.trip_id, d.date, i.title, cs.city_name, "
            "       cs.city_adcode "
            "FROM day_item i "
            "JOIN day d ON d.id = i.day_id "
            "JOIN city_stay cs ON cs.id = d.city_stay_id "
            "LEFT JOIN poi p ON p.amap_poi_id = i.poi_id "
            "WHERE i.kind = 'meal' "
            "  AND COALESCE(p.lat_gcj02, i.lat_gcj02) IS NULL "
            "  AND (? IS NULL OR d.trip_id = ?) "
            "ORDER BY d.date, i.seq",
            (trip_id, trip_id),
        ).fetchall()
        # 锚点一次全取回来再按天分组，而不是每个餐饮项查一次（避免 N+1）。
        # 库很小，多取几行不值得为它拼动态 SQL。
        anchor_rows = active.execute(
            "SELECT i.day_id, i.id, i.title, "
            "       COALESCE(p.lat_gcj02, i.lat_gcj02) AS lat, "
            "       COALESCE(p.lng_gcj02, i.lng_gcj02) AS lng "
            "FROM day_item i "
            "LEFT JOIN poi p ON p.amap_poi_id = i.poi_id "
            "WHERE i.kind = 'poi' "
            "  AND COALESCE(p.lat_gcj02, i.lat_gcj02) IS NOT NULL "
            "  AND COALESCE(p.lng_gcj02, i.lng_gcj02) IS NOT NULL "
            "ORDER BY i.seq"
        ).fetchall()
    finally:
        if owned:
            active.close()

    by_day: dict[str, list[Anchor]] = {}
    for row in anchor_rows:
        by_day.setdefault(row["day_id"], []).append(
            Anchor(
                item_id=row["id"],
                title=row["title"],
                lat_gcj02=row["lat"],
                lng_gcj02=row["lng"],
            )
        )

    return [
        MealSlot(
            item_id=row["id"],
            trip_id=row["trip_id"],
            day_date=row["date"],
            title=row["title"],
            city_name=row["city_name"] or "",
            city_adcode=row["city_adcode"],
            anchors=tuple(by_day.get(row["day_id"], ())),
        )
        for row in rows
    ]


def resolve_meal(slot: MealSlot, *, search, around=None) -> MealFix:
    """把一个餐饮名对到高德的店上，只取**分得清**的那一种。

    **不能用 `domain/align.py` 那套。** 它回答的是「这是哪个景点」，为此有一道
    类型门禁：`poi.kind is IRRELEVANT` 直接拒绝——而餐厅按 `classify_poi`
    恰好就是 IRRELEVANT（这是对的：候选池里出现饭馆是噪声）。拿它来对餐厅，
    每一个都会被判「类型不是目的地」，一句都补不上。

    这两个问题的要求本来就不同：景点要的是**唯一实体**（ADR-0002，一个地方
    一个 id），餐厅不进实体表，只需要「这家店在哪」。所以这里另立判据，
    但**沿用它的名称分**（`name_score`）与同一个及格线
    （`MATCH_SCORE_THRESHOLD`）：名字像不像是一回事，没必要有两套。

    两次尝试，范围不同：

    1. **按城市按名搜**。够用的时候最省事，但它会把全城同名分店一起端出来。
    2. **绕着当天有坐标的景点做周边搜**（`around`）。这是更接近真相的范围——
       这些店名本来就是引擎按当天景点周边搜出来的。实测「绿柳居」全城 8 家
       同分，绕着夫子庙搜只有一家。第一处锚点给出**明确**答案就停。

    每轮的判据都是四道关：

    1. **同城**（adcode 前四位）——搜到别的城市的同名店是最危险的一种错，
       坐标差几百公里，而名字一模一样。
    2. **是吃饭的地方**（高德一级类型码 05）。
    3. **名字够像**（`name_score` ≥ 及格线）。
    4. **没有并列第一**——并列就是不唯一，不猜。通用菜名（「鸭血粉丝汤」）
       会搜出一串同名小店，全部并列，于是留空。
    """
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
    def judge(found, *, near: str = "") -> MealFix | None:
        """一次尝试的判据。定下来就返回结果，定不下来返回 None。"""
        decision = _decide(slot, found.candidates, near=near)
        return decision.fix

    try:
        found = search(slot.title, slot.city_name)
    except PoiSearchError as exc:
        # **只接住「搜索失败」这一种。** 把 `Exception` 全接掉的话，
        # 注入进来的搜索函数写错签名也会显示成「高德搜索失败：unexpected
        # keyword argument」——那是我们的 bug，该炸就炸，不该伪装成网络问题。
        return MealFix(slot, reason=f"高德搜索失败：{exc}")

    if not found.candidates:
        city_note = "高德没有搜到这个名字（通用菜名常常如此）"
    else:
        hit = judge(found)
        if hit is not None:
            return hit
        city_note = _decide(slot, found.candidates).reason

    # ── 第二轮：绕着当天的景点找 ──────────────────────────────
    if around is None or not slot.anchors:
        return MealFix(slot, reason=city_note)

    tried: list[str] = []
    for anchor in slot.anchors:
        tried.append(anchor.title)
        try:
            nearby = around(
                slot.title, lat_gcj02=anchor.lat_gcj02, lng_gcj02=anchor.lng_gcj02
            )
        except PoiSearchError as exc:
            return MealFix(slot, reason=f"{city_note}；周边搜索失败：{exc}")
        hit = judge(nearby, near=anchor.title)
        if hit is not None:
            return hit

    return MealFix(
        slot,
        reason=f"{city_note}；绕着当天的 {'、'.join(tried[:3])} 也没找到更明确的",
    )


@dataclass(frozen=True)
class _Decision:
    """一次尝试的结论：定下来给结果，定不下来给原因。"""

    fix: MealFix | None = None
    reason: str = ""


def _decide(slot: MealSlot, candidates, *, near: str = "") -> _Decision:
    """四道关，一次算完，结论与理由出自同一处。

    **只有这一处做判据。** 先前「算结果」与「算理由」各写了一遍，于是
    「对上了但高德没给坐标」只有一边知道——报出来的原因成了「有 1 家同分的店」
    （一家怎么会同分）。一处判断不能有两个答案，算两遍迟早分岔。
    """
    from lushu.domain.align import MATCH_SCORE_THRESHOLD, name_score

    in_city = [poi for poi in candidates if poi.in_city(slot.city_adcode)]
    if not in_city:
        return _Decision(reason=f"搜到的都不在{slot.city_name}，不能拿别的城市的同名店顶上")
    food = [poi for poi in in_city if _is_food(poi)]
    if not food:
        return _Decision(reason="搜到的都不是吃饭的地方")

    scored = sorted(
        ((name_score(slot.title, poi.name), poi) for poi in food), key=lambda pair: -pair[0]
    )
    best_score, best = scored[0]
    if best_score < MATCH_SCORE_THRESHOLD:
        return _Decision(reason=f"搜到的最像的是「{best.name}」，但名字差得远（{best_score}）")

    ties = [poi for score, poi in scored if score == best_score]
    if len(ties) > 1:
        return _Decision(
            reason=f"有 {len(ties)} 家同分的店（「{ties[0].name}」等），分不出是哪一家——"
            "通用菜名基本都会卡在这里"
        )

    if best.lat_gcj02 is None or best.lng_gcj02 is None:
        return _Decision(reason=f"对上了「{best.name}」，但高德没给坐标")
    return _Decision(
        fix=MealFix(
            slot,
            matched_name=best.name,
            lat_gcj02=best.lat_gcj02,
            lng_gcj02=best.lng_gcj02,
            address=best.address,
            reason=f"名字命中（{best_score}）",
            near=near,
        )
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
    around=None,
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
        fixes.append(resolve_meal(slot, search=search, around=around))
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
