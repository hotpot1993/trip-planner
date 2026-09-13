"""候选池：规划某座城市时，先摆出网友真正推荐过的地方。

设计第 5.1 节：**候选池以攻略知识库为主、高德搜索补全**（Q27）。
界面上先列出网友真正推荐过的地方，带打卡与避坑信息及置信度；
高德负责补齐坐标与开放时间。知识库尚未覆盖的城市仍可直接搜高德。

这一层存在的理由就是 M5 的验收条件——「打开城市看到的是网友推荐而非一片 POI」。
一片 POI 的问题是它们**没有观点**：高德会告诉你「这里有个公园」，
不会告诉你「这个公园傍晚上去最好，白天没遮阴」。前者地图上到处都是，
后者才是这份攻略的价值。

两条刻意的设计：

1. **来源要标出来。** 一条候选是「三个网友推荐过」还是「高德搜出来的」，
   对用户的意义完全不同，界面上必须分得开。所以每条候选带 `source`。
2. **没有结论的地方不进候选池。** 知识库里的 POI 若一条结论都没挂上，
   它对规划并不比高德的一条搜索结果更有用——把它混进来只会让
   「网友推荐」这个信号贬值。空城市要如实说「这里还没有攻略数据」，
   而不是假装有。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace

from lushu.adapters.poi import PoiSearchError
from lushu.domain.knowledge import Confidence, Polarity
from lushu.domain.poi import CandidatePoi
from lushu.services import knowledge_store as ks
from lushu.store.connection import connect

# 知识库覆盖到多少条才算「这座城市有攻略数据」。
# 太少的话（比如只有一条）不足以支撑规划，界面上要如实说「数据还很少」。
THIN_COVERAGE = 3


class PoolSource:
    """候选从哪来。界面上要分得开——它们对用户的意义不同。"""

    KNOWLEDGE = "knowledge"  # 攻略知识库：网友真的写过
    AMAP = "amap"  # 高德搜索补全：地图上有，但还没有人推荐过


@dataclass(frozen=True)
class Candidate:
    """候选池里的一条。"""

    poi_id: str
    name: str
    city_adcode: str | None = None
    address: str | None = None
    lat_gcj02: float | None = None
    lng_gcj02: float | None = None
    rating: float | None = None
    open_time: str | None = None
    source: str = PoolSource.KNOWLEDGE
    # 挂在这个地方上的结论。打卡与避坑分开——它们是两类信息，混在一起
    # 等于把「值得去」与「注意什么」揉成一句含糊的话。
    highlights: tuple[ks.ClaimRow, ...] = ()
    avoids: tuple[ks.ClaimRow, ...] = ()
    # 需不需要预约。**`None` 是「没有已复核的规则」，不是「不需要预约」**——
    # 两者在界面上必须分得开。同样地，`booking_required=True` 而
    # `booking_days` 为 None 是「要预约，但官方没公布放票口径」，
    # 那不是「没有预约信息」。实测踩过：兵马俑那条规则就是这样，
    # 当时界面上显示成「无预约信息」，等于把「必须预约」说成了「不用管」。
    booking_required: bool | None = None
    booking_days: int | None = None  # 需不需要预约、提前几天
    booking_time: str | None = None

    @property
    def claim_count(self) -> int:
        return len(self.highlights) + len(self.avoids)

    @property
    def high_confidence_count(self) -> int:
        return sum(
            1
            for claim in self.highlights + self.avoids
            if claim.confidence is Confidence.HIGH
        )

    @property
    def recommended(self) -> bool:
        """是不是「网友推荐过」的地方。高德补全的那些不算。"""
        return self.source == PoolSource.KNOWLEDGE and self.claim_count > 0


@dataclass
class CityPool:
    """一座城市的候选池。"""

    city_adcode: str
    candidates: list[Candidate] = field(default_factory=list)
    # 回落到高德搜索失败的原因。**只在「知识库没有这座城市」那条路上会被设置**
    # （`fill_gaps` 那条补全路径目前没有调用方）。回落失败不该让整页报错，
    # 但也不能静默：用户看到的是一片空池子，得知道那不是「这城市没什么可去的」。
    amap_error: str | None = None

    @property
    def recommended_count(self) -> int:
        return sum(1 for item in self.candidates if item.recommended)

    @property
    def covered(self) -> bool:
        """知识库覆盖够不够。不够时界面要说实话，而不是假装有攻略。"""
        return self.recommended_count >= THIN_COVERAGE


def _claims_by_poi(rows: list[ks.ClaimRow]) -> dict[str, list[ks.ClaimRow]]:
    grouped: dict[str, list[ks.ClaimRow]] = {}
    for row in rows:
        if row.poi_id:
            grouped.setdefault(row.poi_id, []).append(row)
    return grouped


def _split(claims: list[ks.ClaimRow]) -> tuple[tuple[ks.ClaimRow, ...], tuple[ks.ClaimRow, ...]]:
    """按极性分开。高置信的排前面——它是「多个人都这么说」的那些。

    注意 `ClaimRow.polarity` 是**字符串**（`confidence` 才是枚举），
    所以这里用 `==` 而不是 `is`——`is` 会静默地一条都匹配不上。
    """
    order = lambda item: (  # noqa: E731 - 排序键，用一行更清楚
        item.confidence is not Confidence.HIGH,
        -item.independent_source_count,
    )
    highlights = tuple(
        sorted((c for c in claims if c.polarity == Polarity.HIGHLIGHT), key=order)
    )
    avoids = tuple(sorted((c for c in claims if c.polarity == Polarity.AVOID), key=order))
    return highlights, avoids


def _poi_rows(conn: sqlite3.Connection, poi_ids: list[str]) -> dict[str, sqlite3.Row]:
    if not poi_ids:
        return {}
    placeholders = ",".join("?" for _ in poi_ids)
    rows = conn.execute(
        f"SELECT * FROM poi WHERE amap_poi_id IN ({placeholders})", poi_ids
    ).fetchall()
    return {row["amap_poi_id"]: row for row in rows}


def _booking_rules(conn: sqlite3.Connection, poi_ids: list[str]) -> dict[str, sqlite3.Row]:
    """**只取已复核的**（Q10）：未经复核的规则不得展示。

    取草案的话，界面上会出现一条没人核过的放票时刻，而用户会照着它去等。
    """
    if not poi_ids:
        return {}
    placeholders = ",".join("?" for _ in poi_ids)
    rows = conn.execute(
        f"SELECT * FROM booking_rule WHERE status = 'reviewed' "
        f"AND poi_id IN ({placeholders})",
        poi_ids,
    ).fetchall()
    return {row["poi_id"]: row for row in rows}


def city_candidates(
    city_adcode: str, *, conn: sqlite3.Connection | None = None, limit: int = 60
) -> list[Candidate]:
    """一座城市里**有结论挂着**的那些地方。

    一条结论都没有的 POI 不进候选池：它对规划并不比高德的一条搜索结果更有用，
    混进来只会让「网友推荐」这个信号贬值。
    """
    owned = conn is None
    active = conn or connect()
    try:
        claims = ks.claims_for_city(city_adcode, conn=active)
        grouped = _claims_by_poi(claims)
        if not grouped:
            return []

        by_id = _poi_rows(active, list(grouped))
        rules = _booking_rules(active, list(grouped))

        candidates: list[Candidate] = []
        for poi_id, items in grouped.items():
            row = by_id.get(poi_id)
            highlights, avoids = _split(items)
            rule = rules.get(poi_id)
            candidates.append(
                Candidate(
                    poi_id=poi_id,
                    name=(row["name"] if row else None) or poi_id,
                    city_adcode=row["city_adcode"] if row else city_adcode,
                    address=row["address"] if row else None,
                    lat_gcj02=row["lat_gcj02"] if row else None,
                    lng_gcj02=row["lng_gcj02"] if row else None,
                    rating=row["rating"] if row else None,
                    open_time=row["open_time"] if row else None,
                    source=PoolSource.KNOWLEDGE,
                    highlights=highlights,
                    avoids=avoids,
                    booking_required=bool(rule["booking_required"]) if rule else None,
                    booking_days=rule["advance_days"] if rule else None,
                    booking_time=rule["release_time"] if rule else None,
                )
            )
    finally:
        if owned:
            active.close()

    # 结论多的排前面：那是网友提得最多的地方。同分时按名字，保证结果可复现。
    candidates.sort(key=lambda item: (-item.claim_count, -item.high_confidence_count, item.name))
    return candidates[:limit]


def city_adcode_for(city_name: str, *, conn: sqlite3.Connection | None = None) -> str | None:
    """城市名 → adcode。

    引擎给的是**名字**（用户输入的目的地），库里存的是 adcode。两边写法常常
    差一个字（引擎给「南京」，库里是「南京市」），所以双向前缀匹配。
    匹配不上就返回 None——那不是错误，是「这座城市还没进过库」，
    调用方据此走原路（纯高德搜索）。
    """
    text = (city_name or "").strip()
    if not text:
        return None
    owned = conn is None
    active = conn or connect()
    try:
        row = active.execute(
            "SELECT adcode FROM city WHERE name = ? "
            "   OR name LIKE ? || '%' OR ? LIKE name || '%' "
            "ORDER BY LENGTH(name) LIMIT 1",
            (text, text, text),
        ).fetchone()
    finally:
        if owned:
            active.close()
    return row["adcode"] if row else None


def _poi_entities(
    poi_ids: list[str], *, conn: sqlite3.Connection
) -> list[CandidatePoi]:
    """按给到的顺序取回完整的 POI（含 `raw_json`）。

    为什么不用 `_poi_rows`：那个只取界面要显示的那几列。这里要交给引擎，
    它优先用原始响应转换（见 `engine/pool_search.to_spot`），所以得带上
    `raw_json` 与类型列。
    """
    if not poi_ids:
        return []
    marks = ",".join("?" * len(poi_ids))
    rows = conn.execute(
        f"SELECT * FROM poi WHERE amap_poi_id IN ({marks})",  # noqa: S608 - 占位符按个数拼，值仍走参数
        poi_ids,
    ).fetchall()
    by_id = {row["amap_poi_id"]: row for row in rows}

    found: list[CandidatePoi] = []
    for poi_id in poi_ids:
        row = by_id.get(poi_id)
        if row is None:
            continue
        found.append(
            CandidatePoi(
                poi_id=row["amap_poi_id"],
                name=row["name"],
                typecode=row["typecode"],
                type_name=row["type"],
                adcode=row["adcode"],
                city_name=None,
                address=row["address"],
                lng_gcj02=row["lng_gcj02"],
                lat_gcj02=row["lat_gcj02"],
                tel=row["tel"],
                photo_url=row["photo_url"],
                rating=row["rating"],
                open_time=row["open_time"],
                parent_id=row["parent_poi_id"],
                raw_json=row["raw_json"],
            )
        )
    return found


def pool_pois(
    city_name: str,
    *,
    conn: sqlite3.Connection | None = None,
    limit: int = 60,
    fetch=None,
) -> list[CandidatePoi]:
    """一座城市候选池里的地方，交给规划引擎用。

    **判据与城市页同源**：先调 `city_candidates`（「有结论挂着才进池」那条
    规则住在那里），再把结果换成引擎要的形状。两处各写一遍「什么算进池子」，
    城市页与排程迟早会对同一个城市给出不同的池子——而那种不一致看起来
    像是「规划不如页面懂我」。

    引擎给的是城市名，所以先做一次名字 → adcode 的翻译；翻译不出来
    （城市还没进过库）就返回空，调用方据此走纯高德搜索。

    最后会**补一次缺失的评分**（见 `_fill_ratings`）：引擎的景点搜索之后有一道
    「评分缺失视为不达标」的门禁，池子里没评分的地方会被它丢掉——夫子庙就是
    这样一个。补的是高德自己的评分，不是我们编的数。
    """
    adcode = city_adcode_for(city_name, conn=conn)
    if adcode is None:
        return []

    picked = city_candidates(adcode, conn=conn, limit=limit)
    if not picked:
        return []

    owned = conn is None
    active = conn or connect()
    try:
        pois = _poi_entities([item.poi_id for item in picked], conn=active)
        return _fill_ratings(pois, conn=active, fetch=fetch)
    finally:
        if owned:
            active.close()


def _fill_ratings(
    pois: list[CandidatePoi], *, conn: sqlite3.Connection, fetch=None
) -> list[CandidatePoi]:
    """给没有评分的地方补上高德的评分，并写回库里。

    为什么这一步不能省：引擎的 `attraction_search_node` 拿到景点清单之后要过
    一道 `filter_by_rating`，**评分缺失一律视为不达标**。池子里的地方是我们
    已经认定「有人写过」的，却会因为高德没给评分被那道门禁丢掉——
    夫子庙正是如此（本库里 11 个池子条目有 2 个没评分）。

    补的是高德接口里那个评分，**不是编一个数**。查不到就保持原样：
    缺评分是现状，不是错误，不该让一次规划因此挂掉。
    """
    missing = [poi for poi in pois if poi.rating is None]
    if not missing:
        return pois

    if fetch is None:
        from lushu.adapters.poi import fetch_poi

        fetch = fetch_poi

    found_ratings: dict[str, float] = {}
    for poi in missing:
        try:
            found = fetch(poi.poi_id)
        except Exception:  # noqa: BLE001 - 缺评分是现状，不该让规划挂掉
            continue
        if found is not None and found.rating is not None:
            found_ratings[poi.poi_id] = found.rating

    if not found_ratings:
        return pois

    for poi_id, rating in found_ratings.items():
        conn.execute("UPDATE poi SET rating = ? WHERE amap_poi_id = ?", (rating, poi_id))
    conn.commit()

    return [replace(poi, rating=found_ratings.get(poi.poi_id, poi.rating)) for poi in pois]


def fill_gaps(
    candidates: list[Candidate], *, fetch, limit: int = 20
) -> list[Candidate]:
    """用高德补齐知识库缺失的坐标与开放时间。

    **只补空缺，不覆盖**（ADR-0001：硬事实以官方接口为准，而库里的
    `open_time` 本来就是从高德来的）。需要补的是那些网友提过、但 M3 对齐时
    没顾上写全字段的地方。
    """
    from dataclasses import replace

    filled: list[Candidate] = []
    budget = limit
    for item in candidates:
        needs = item.lat_gcj02 is None or item.open_time is None
        if not needs or budget <= 0:
            filled.append(item)
            continue
        budget -= 1
        poi = fetch(item.poi_id)
        if poi is None:
            filled.append(item)
            continue
        filled.append(
            replace(
                item,
                address=item.address or poi.address,
                lat_gcj02=item.lat_gcj02 or poi.lat_gcj02,
                lng_gcj02=item.lng_gcj02 or poi.lng_gcj02,
                rating=item.rating if item.rating is not None else poi.rating,
                open_time=item.open_time or poi.open_time,
            )
        )
    return filled


def amap_fallback(
    city_name: str, *, search, limit: int = 30
) -> list[Candidate]:
    """知识库没有覆盖的城市，直接搜高德。

    这些候选 `source=amap`：地图上有，但还没有网友推荐过。
    界面上必须把它们与「网友真的写过」的那些分开——
    把一片高德 POI 说成「推荐」就是这套东西最不该犯的错。
    """
    found = search("景点", city_name)
    candidates: list[Candidate] = []
    for poi in found.candidates[:limit]:
        candidates.append(
            Candidate(
                poi_id=poi.poi_id,
                name=poi.name,
                city_adcode=poi.adcode,
                address=poi.address,
                lat_gcj02=poi.lat_gcj02,
                lng_gcj02=poi.lng_gcj02,
                rating=poi.rating,
                open_time=poi.open_time,
                source=PoolSource.AMAP,
            )
        )
    return candidates


def _live_search():
    """真实的高德搜索。**在服务层里 import 适配器**——接口层不许碰 adapters
    （架构边界测试会拦，实测拦过一次）。

    要包一层：`search_pois` 的第二个参数是**关键字**（`city=`），而 `amap_fallback`
    按位置传 `(关键词, 城市)`。**原先这里直接把函数返回了**，于是调用时是一个
    TypeError，被 `pool_for_city` 的宽 catch 收进 `amap_error`——界面上写着
    「高德补全没成功：TypeError: search_pois() takes 1 positional argument but
    2 were given」，把我们的接错线说成了高德的失败。后果是**「知识库没覆盖的
    城市直接搜高德」这条路从来没工作过**（设计 5.1），而表现出来只是少了几条
    候选加一句没人细看的小字。

    别处四处（`booking_store`、`meal_coords`、`pipeline`、`realign`）都是这个
    包法；只有这里漏了，因为只有这里把它当成「原样传下去的函数」而不是
    「一个 (关键词, 城市) 形状的回调」。
    """
    from lushu.adapters.poi import search_pois

    return lambda keywords, city: search_pois(keywords, city=city)


def _live_fetch():
    """`fetch_poi(poi_id, ...)` 的第一个参数就是 id，按位置传没问题。"""
    from lushu.adapters.poi import fetch_poi

    return fetch_poi


def pool_for_city(
    city_adcode: str,
    *,
    city_name: str | None = None,
    conn: sqlite3.Connection | None = None,
    search=None,
    fetch=None,
    fill: bool = False,
    limit: int = 60,
) -> CityPool:
    """一座城市的完整候选池。

    知识库有货就用知识库（可选再用高德补空缺字段）；一条都没有才回落到搜高德。
    **两条路的结果不混在一起**：混了的话，「网友推荐」这个信号就没了。

    `fill` 才会去问高德（每个缺字段的候选一次请求，慢且要配额）。
    默认只读本地库——界面首屏不该等一串网络请求。
    """
    keywords = city_candidates(city_adcode, conn=conn, limit=limit)
    pool = CityPool(city_adcode=city_adcode)

    if keywords and fill:
        keywords = fill_gaps(keywords, fetch=fetch or _live_fetch())

    if keywords:
        pool.candidates = keywords
        return pool

    if not city_name:
        return pool

    searcher = search or _live_search()
    try:
        pool.candidates = amap_fallback(city_name, search=searcher, limit=limit)
    except PoiSearchError as exc:
        # **只接 PoiSearchError。** 高德挂了不该让整页报错，但我们自己的接线
        # 错误必须炸出来——原先这里接的是 Exception，于是一个签名不匹配的
        # TypeError 被显示成「高德补全没成功」，那条路坏了一整轮都没人发现。
        # 这与 `pipeline._fetch_from_db`、`meal_coords` 是同一条规矩。
        pool.amap_error = f"{type(exc).__name__}: {exc}"
    return pool
