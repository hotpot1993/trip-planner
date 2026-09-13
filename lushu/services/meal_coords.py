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
| 只按城市按名搜 | 查到 1 个（回味鸭血粉丝汤，唯一同名店，离夫子庙 200 米），6 个定不下来，路书空洞 7 处一个没少 |
| 加上「绕着当天景点搜」 | 又查到 1 个（子午路张记肉夹馍，陕历博 1.8 公里外的分店），**空洞 7 → 6** |

剩下的 5 个是判据**故意**不补的，各有各的原因：绿柳居（8 家同分）与老孙家泡馍
（4 家同分）在周边还是多家同分——品牌名不带分店，本来就不该猜；蒋有记锅贴
最像的「蒋有记(老门东店)」只有 0.34；南京大牌档（中山陵店）在高德上叫
「南京大牌档(中山陵紫金坊店)」（0.576）；「临潼石榴汁与 biangbiang 面」不是店名。

**一段路要两个端点都有坐标才算得出来**——所以补一个点常常不减少一处空洞。
剩下这 5 个要么人工指定（人知道说的是哪一家），要么就不补。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from lushu.adapters.poi import MAX_LIMIT, PoiSearchError
from lushu.domain.geo import distance_m
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


@dataclass(frozen=True)
class MealCandidate:
    """一个可以指定的候选店。

    `distance_m` 是离**当天最近那处景点**的距离，`nearest_anchor` 是那处的名字。
    人是靠「就在老门东里面」这句话认出来的，而不是靠名字分。
    """

    poi_id: str
    name: str
    address: str | None
    lat_gcj02: float
    lng_gcj02: float
    distance_m: int | None = None
    nearest_anchor: str | None = None
    name_score: float = 0.0


@dataclass
class CandidateList:
    """一个待指定餐饮项的候选，以及一共筛出多少个。"""

    slot: MealSlot
    candidates: list[MealCandidate] = field(default_factory=list)
    total: int = 0
    note: str = ""


# 指定时最多列几个候选。全城搜「绿柳居」有 24 家，一次铺完没人看得下去；
# 按离当天景点的距离排好之后，该看的那几家一定在最前面。
CANDIDATE_LIMIT = 8


def meal_candidates(
    slot: MealSlot,
    *,
    search=None,
    around=None,
    limit: int = CANDIDATE_LIMIT,
    pause: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> CandidateList:
    """把一个待指定餐饮项的候选摊出来，**先按名字像不像、再按离当天景点多远**排。

    自动判据定不下来的时候（同名分店太多、行程里写的是菜名），只有人知道
    说的是哪一家。人靠什么认？对「绿柳居」这种，不是名字分——24 家全都一样像，
    而是「就在老门东里面」。所以同分时距离说话。

    但距离不能当主序：高德的周边搜索会带回附近**别的**饭馆，它们离得更近，
    会把真正像的那几家挤下去。详见下面排序键处的注释。

    这里**不按名字分筛掉候选**：自动判据嫌「蒋有记(老门东店)」只有 0.34，
    可人在屏幕上一眼就知道是它。筛掉它等于把答案藏起来。
    结构性判据照旧（同城、是吃饭的地方）——那是「是不是同一类东西」，不是判断。
    """
    from lushu.domain.align import name_score

    if search is None or around is None:
        from lushu.adapters.poi import search_around_pois, search_pois

        search = search or (lambda keywords, city: search_pois(keywords, city=city, limit=MAX_LIMIT))
        around = around or (
            lambda keywords, lat_gcj02, lng_gcj02: search_around_pois(
                keywords, lat_gcj02=lat_gcj02, lng_gcj02=lng_gcj02, limit=MAX_LIMIT
            )
        )

    found: dict[str, MealCandidate] = {}
    notes: list[str] = []

    def take(result) -> None:
        for poi in result.candidates:
            if not poi.in_city(slot.city_adcode) or not _is_food(poi):
                continue
            if poi.lat_gcj02 is None or poi.lng_gcj02 is None:
                continue
            # 距离是**算出来的**，与「哪一次搜索找到它」无关：城市搜索也会
            # 返回店，那些同样要显示「离当天景点多远」。第一版把距离绑在
            # 「是哪处锚点的周边搜索带回来的」，于是城市搜索来的候选全显示
            # 「距离算不出」——而我们手里明明有坐标。
            distance, anchor_title = _nearest_anchor(poi, slot.anchors)
            found[poi.poi_id] = MealCandidate(
                poi_id=poi.poi_id,
                name=poi.name,
                address=poi.address,
                lat_gcj02=poi.lat_gcj02,
                lng_gcj02=poi.lng_gcj02,
                distance_m=distance,
                nearest_anchor=anchor_title,
                name_score=name_score(slot.title, poi.name),
            )

    try:
        take(search(slot.title, slot.city_name))
    except PoiSearchError as exc:
        notes.append(f"城市搜索失败：{exc}")

    for anchor in slot.anchors:
        if pause:
            sleep(pause)
        try:
            # 与 `resolve_meal` 里的调法**必须一致**：同一个注入的搜索函数，
            # 一处按关键字传、一处按位置传，测试用假函数时会当场炸，
            # 用真函数时则可能悄悄传反（高德的 location 是「经度,纬度」）。
            take(around(slot.title, lat_gcj02=anchor.lat_gcj02, lng_gcj02=anchor.lng_gcj02))
        except PoiSearchError as exc:
            notes.append(f"绕着{anchor.title}搜索失败：{exc}")

    ordered = sorted(
        found.values(),
        # **名字分是主序，距离是次序。**
        #
        # 一开始只按距离排，实测不行：高德的周边搜索会带回附近**别的**饭馆
        # （搜「南京大牌档（中山陵店）」，紫金坊边上那些火烧店、面馆全进来了），
        # 它们离得最近，于是把真正像的那几家挤到后面去——人打开看到的是
        # 一串不相干的店。
        #
        # 反过来先按名字分排，正好落在两种真实情形上：
        # - 品牌名不带分店（「绿柳居」的 24 家全是 0.9）：同分，距离接着说话，
        #   于是夫子庙边上那几家排在最前——这正是人认出答案的方式；
        # - 高德的名字与行程里写的不一样（「蒋有记锅贴」对「蒋有记(老门东店)」
        #   只有 0.34）：分都低，距离仍然说话。
        #
        # 距离算不出的排在同分的最后，而不是当成 0——0 米看起来像就在旁边。
        key=lambda item: (
            -item.name_score,
            item.distance_m is None,
            item.distance_m if item.distance_m is not None else 0,
            item.name,
        ),
    )
    return CandidateList(
        slot=slot,
        candidates=ordered[:limit],
        total=len(ordered),
        note="；".join(notes),
    )


def _nearest_anchor(poi, anchors: tuple[Anchor, ...]) -> tuple[int | None, str | None]:
    """这一处候选离当天最近的那处景点多远。

    没有锚点就算不出——返回 `None` 而**不是 0**：0 米看起来像就在旁边。
    """
    if not anchors or poi.lat_gcj02 is None or poi.lng_gcj02 is None:
        return None, None
    best = min(
        anchors,
        key=lambda a: distance_m(poi.lat_gcj02, poi.lng_gcj02, a.lat_gcj02, a.lng_gcj02),
    )
    return round(distance_m(poi.lat_gcj02, poi.lng_gcj02, best.lat_gcj02, best.lng_gcj02)), best.title


def pending_by_trip(trip_id: str, *, conn: sqlite3.Connection | None = None) -> list[MealSlot]:
    """某份行程里待指定的餐饮项。界面用它，与命令行同一处来源。"""
    return pending_meals(conn=conn, trip_id=trip_id)


def pending_item(
    trip_id: str, item_id: str, *, conn: sqlite3.Connection | None = None
) -> MealSlot | None:
    """待指定列表里的某一项；不是待指定项就返回 None。

    界面与命令行都要先问这一句：**不是待办的东西不该被写**。
    它同时也回答了「这一项属不属于这份行程」。
    """
    for slot in pending_meals(conn=conn, trip_id=trip_id):
        if slot.item_id == item_id:
            return slot
    return None


def pin_meal(
    item_id: str,
    *,
    lat_gcj02: float,
    lng_gcj02: float,
    address: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """人工指定一处餐饮的坐标。返回是否真的写了。

    **只写空着的那些**（`lat_gcj02 IS NULL`）。这不是保守，是因为这一列一旦
    有值就有人核对过——重跑一次自动补齐或再点一次「就这个」，都不该把它冲掉。

    仍然不写 `poi_id`：给餐厅编一个实体 id 会污染实体表（ADR-0002 说的是景点）。
    """
    owned = conn is None
    active = conn or connect()
    try:
        cursor = active.execute(
            "UPDATE day_item SET lat_gcj02 = ?, lng_gcj02 = ?, address = COALESCE(address, ?) "
            "WHERE id = ? AND kind = 'meal' AND lat_gcj02 IS NULL",
            (lat_gcj02, lng_gcj02, address, item_id),
        )
        active.commit()
        return bool(cursor.rowcount)
    finally:
        if owned:
            active.close()


def plan_fill(    slots: Sequence[MealSlot],
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
