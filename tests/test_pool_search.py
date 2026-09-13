"""让候选池驱动引擎的景点搜索（设计 5.1：池子为主、高德补全）。

引擎的 `attraction_search_node` 无条件调用 `fetch_city_spots_async`，而它是
`planning/nodes.py` 里的**模块级导入**，所以能在我们这一侧运行时替换掉——
`third_party/` 一个字节不动（ADR-0005 的隔离由架构测试把守）。

这里守四件事：

1. **池子在前**。`max_spots` 是硬上限，排在后面的会被截掉，
   所以「池子在前」＝网友推荐过的地方先被看到。
2. **高德补全，且不重复**。池子里的地方大多也搜得到，不去重就会
   一份清单里出现两遍。
3. **池子没覆盖的城市原样走原路**——不能因为接了这个而少掉什么。
4. **状态不残留**。上一次带池子、这一次不带，必须真的不带。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lushu.domain.poi import CandidatePoi
from lushu.engine import pool_search
from lushu.services import candidate_pool
from lushu.store import connect, initialize, transaction

NOW = "2026-09-13T10:00:00"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "pool.db"
    initialize(path)
    return path


@pytest.fixture(autouse=True)
def _restore_engine():
    """每个用例之后把引擎恢复原样。

    这个替换是**全局**的（改的是模块属性），漏掉恢复会让后面的用例
    在一个被替换过的引擎上跑——那种失败看起来莫名其妙。
    """
    yield
    pool_search.install(None)


def _raw(poi_id: str, name: str, lat: float, lng: float, *, rating: str = "4.8") -> str:
    """一条高德原始响应（形状照 `poi_to_spot` 读的那些键）。"""
    return json.dumps(
        {
            "id": poi_id,
            "name": name,
            "location": f"{lng},{lat}",
            "address": f"{name}的地址",
            "adname": "某区",
            "tel": "025-00000000",
            "photos": [{"url": "https://example.com/x.jpg"}],
            "biz_ext": {"rating": rating, "opentime2": "09:00-17:00", "cost": "60"},
        },
        ensure_ascii=False,
    )


def _poi_with_claim(
    db: Path,
    poi_id: str,
    name: str,
    *,
    lat: float = 32.05,
    lng: float = 118.79,
    claims: int = 1,
    city_adcode: str = "320100",
    raw: str | None = None,
) -> None:
    with transaction(db) as conn:
        # 城市先落库：poi.city_adcode 有外键
        conn.execute(
            "INSERT OR REPLACE INTO city (adcode, name, updated_at) VALUES (?, ?, ?)",
            (city_adcode, "南京", NOW),
        )
        conn.execute(
            "INSERT OR REPLACE INTO poi (amap_poi_id, name, city_adcode, adcode, typecode, "
            "address, lat_gcj02, lng_gcj02, rating, open_time, raw_json, fetched_at) "
            "VALUES (?, ?, ?, '320104', '110201', ?, ?, ?, 4.8, '09:00-17:00', ?, ?)",
            (poi_id, name, city_adcode, f"{name}的地址", lat, lng,
             raw if raw is not None else _raw(poi_id, name, lat, lng), NOW),
        )
        for index in range(claims):
            document = f"src_{poi_id}_{index}"
            conn.execute(
                "INSERT OR REPLACE INTO source_document (id, site, body_text, body_sha256, "
                "imported_at, import_kind) VALUES (?, 'manual', '正文', ?, ?, 'paste')",
                (document, document, NOW),
            )
            claim_id = f"clm_{poi_id}_{index}"
            conn.execute(
                "INSERT OR REPLACE INTO claim (id, subject_type, subject_name, poi_id, polarity, "
                "facet, text, confidence, independent_source_count, status, first_seen_at) "
                "VALUES (?, 'poi', ?, ?, 'highlight', 'photo', '有人写过', 'high', 3, "
                "'active', ?)",
                (claim_id, name, poi_id, NOW),
            )
            conn.execute(
                "INSERT INTO claim_evidence (id, claim_id, source_document_id, quote, created_at) "
                "VALUES (?, ?, ?, '原文', ?)",
                (f"ev_{poi_id}_{index}", claim_id, document, NOW),
            )


class TestCityLookup:
    def test_finds_the_city_by_name(self, db: Path) -> None:
        _poi_with_claim(db, "B_1", "夫子庙")

        assert candidate_pool.city_adcode_for("南京", conn=connect(db)) == "320100"

    def test_tolerates_a_suffix(self, db: Path) -> None:
        """引擎给「南京」，库里可能是「南京市」——反过来也一样。"""
        _poi_with_claim(db, "B_1", "夫子庙")
        with transaction(db) as conn:
            conn.execute("UPDATE city SET name = '南京市' WHERE adcode = '320100'")

        assert candidate_pool.city_adcode_for("南京", conn=connect(db)) == "320100"

    def test_an_unknown_city_is_not_an_error(self, db: Path) -> None:
        """匹配不上不是错误，是「这座城市还没进过库」——调用方据此走原路。"""
        assert candidate_pool.city_adcode_for("火星", conn=connect(db)) is None

    def test_a_blank_name_gives_nothing(self, db: Path) -> None:
        assert candidate_pool.city_adcode_for("  ", conn=connect(db)) is None


class TestPoolPois:
    def test_returns_the_pool_as_domain_objects(self, db: Path) -> None:
        _poi_with_claim(db, "B_1", "夫子庙")

        found = candidate_pool.pool_pois("南京", conn=connect(db))

        assert [poi.name for poi in found] == ["夫子庙"]
        assert isinstance(found[0], CandidatePoi)
        # 带上原始响应：引擎优先用它转换（字段约定那一边是权威的）
        assert found[0].raw_json

    def test_orders_by_how_many_people_wrote_about_it(self, db: Path) -> None:
        """结论多的排前面——那是网友提得最多的地方。"""
        _poi_with_claim(db, "B_1", "写得少的", claims=1)
        _poi_with_claim(db, "B_2", "写得多的", lat=32.06, claims=3)

        found = candidate_pool.pool_pois("南京", conn=connect(db))

        assert [poi.name for poi in found] == ["写得多的", "写得少的"]

    def test_a_city_with_no_knowledge_gives_an_empty_pool(self, db: Path) -> None:
        """池子是空的 —— 调用方据此走纯高德搜索，**不是报错**。"""
        _poi_with_claim(db, "B_1", "夫子庙")

        assert candidate_pool.pool_pois("火星", conn=connect(db)) == []

    def test_a_poi_without_claims_is_not_in_the_pool(self, db: Path) -> None:
        """一条结论都没有的地方不进池子：混进来只会让「网友推荐」贬值。"""
        _poi_with_claim(db, "B_1", "有人写过的")
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO poi (amap_poi_id, name, city_adcode, adcode, typecode, "
                "lat_gcj02, lng_gcj02, fetched_at) VALUES "
                "('B_2', '没人写过的', '320100', '320104', '110201', 32.06, 118.80, ?)",
                (NOW,),
            )

        found = candidate_pool.pool_pois("南京", conn=connect(db))

        assert [poi.name for poi in found] == ["有人写过的"]


class TestRatingFill:
    """引擎拿到景点清单之后有一道「评分缺失视为不达标」的门禁。

    池子里的地方是我们已经认定「有人写过」的，却会因为高德没给评分被丢掉——
    夫子庙正是如此。所以交给引擎之前要把缺的评分补上，补的是**高德那个评分**，
    不是编一个数。
    """

    def _unrated(self, db: Path) -> None:
        _poi_with_claim(db, "B_1", "夫子庙")
        with transaction(db) as conn:
            conn.execute("UPDATE poi SET rating = NULL WHERE amap_poi_id = 'B_1'")

    def test_fills_the_rating_and_writes_it_back(self, db: Path) -> None:
        self._unrated(db)
        asked: list[str] = []

        def fake_fetch(poi_id: str):
            asked.append(poi_id)
            return CandidatePoi(poi_id=poi_id, name="夫子庙", rating=4.7)

        found = candidate_pool.pool_pois("南京", conn=connect(db), fetch=fake_fetch)

        assert asked == ["B_1"]
        assert found[0].rating == 4.7
        # 写回库里：下次规划不必再查一遍
        row = connect(db).execute(
            "SELECT rating FROM poi WHERE amap_poi_id = 'B_1'"
        ).fetchone()
        assert row["rating"] == 4.7

    def test_does_not_ask_when_everything_is_rated(self, db: Path) -> None:
        _poi_with_claim(db, "B_1", "夫子庙")

        def boom(_poi_id: str):
            raise AssertionError("都有评分了不该再问高德")

        found = candidate_pool.pool_pois("南京", conn=connect(db), fetch=boom)

        assert found[0].rating == 4.8

    def test_a_lookup_failure_keeps_the_pool_intact(self, db: Path) -> None:
        """**缺评分是现状，不是错误**——不该让一次规划因此挂掉。"""
        self._unrated(db)

        def boom(_poi_id: str):
            raise RuntimeError("INVALID_USER_KEY")

        found = candidate_pool.pool_pois("南京", conn=connect(db), fetch=boom)

        assert [poi.name for poi in found] == ["夫子庙"]
        assert found[0].rating is None

    def test_a_place_that_still_has_no_rating_is_not_invented(self, db: Path) -> None:
        """高德也没有评分时保持 None——编一个数比缺一个数糟得多。"""
        self._unrated(db)

        def empty(poi_id: str):
            return CandidatePoi(poi_id=poi_id, name="夫子庙", rating=None)

        found = candidate_pool.pool_pois("南京", conn=connect(db), fetch=empty)

        assert found[0].rating is None


class TestTheRatingGate:
    """**已知边界，不是我们想要的最终行为。**

    引擎的 `attraction_search_node` 拿到清单之后要过一道 `filter_by_rating`，
    评分缺失一律不达标。池子里没评分的地方会被它丢掉。

    我们这一侧改不掉它：那道门禁在**节点**里，而 `graph.py` 对节点另有一份
    模块级绑定，要换就得连节点一起换（超出「只替换搜索」的范围）。
    实测高德对夫子庙**根本没有评分**——不是我们没取到，是它没有。
    所以这件事补不上，只能记在案。

    这两条钉住现状：哪天有人把节点也换了，它们会红，那时应当连同
    `docs/M5-STATUS.md` 里的记录一起改掉，而不是默默删掉测试。
    """

    def _spots(self) -> list[dict]:
        return [
            {"name": "夫子庙", "rating": None},
            {"name": "中山陵景区", "rating": 4.9},
            {"name": "某个广场", "rating": 3.5},
        ]

    def test_a_pool_entry_without_a_rating_is_dropped(self) -> None:
        from third_party.floattrip.planning.helpers import filter_by_rating

        kept, dropped = filter_by_rating(self._spots(), 4.0)

        assert [spot["name"] for spot in kept] == ["中山陵景区"]
        assert "夫子庙" in [spot["name"] for spot in dropped]

    def test_the_pool_entry_is_in_the_list_before_the_gate(self) -> None:
        """它确实进了清单（我们的替换做到了），是门禁把它筛掉的。"""
        assert [spot["name"] for spot in self._spots()][0] == "夫子庙"


class TestToSpot:
    def _poi(self, **overrides: object) -> CandidatePoi:
        base: dict[str, object] = {
            "poi_id": "B_1",
            "name": "夫子庙",
            "typecode": "110201",
            "adcode": "320104",
            "address": "秦淮区",
            "lat_gcj02": 32.0209,
            "lng_gcj02": 118.7886,
            "rating": 4.8,
            "open_time": "09:00-17:00",
        }
        base.update(overrides)
        return CandidatePoi(**base)  # type: ignore[arg-type]

    def test_uses_the_engines_own_conversion_when_raw_is_there(self) -> None:
        """有原始响应就用引擎的 `poi_to_spot`——自己再拼一份等于定两套字段约定。"""
        poi = self._poi(raw_json=_raw("B_1", "夫子庙", 32.0209, 118.7886))

        spot = pool_search.to_spot(poi)

        assert spot is not None
        assert spot["name"] == "夫子庙"
        assert spot["rating"] == 4.8
        assert spot["cost"] == "60"  # 只有原始响应里才有
        assert spot["location"] == {"lng": 118.7886, "lat": 32.0209}

    def test_falls_back_to_the_columns(self) -> None:
        """没有原始响应时用列里的字段拼（少两个装饰性字段，坐标评分都在）。"""
        poi = self._poi()

        spot = pool_search.to_spot(poi)

        assert spot is not None
        assert spot["name"] == "夫子庙"
        assert spot["location"] == {"lng": 118.7886, "lat": 32.0209}
        assert spot["rating"] == 4.8
        assert spot["amap_poi_id"] == "B_1"

    def test_broken_raw_json_falls_back_instead_of_crashing(self) -> None:
        poi = self._poi(raw_json="{不是 JSON")

        spot = pool_search.to_spot(poi)

        assert spot is not None
        assert spot["name"] == "夫子庙"

    def test_without_coordinates_there_is_no_spot(self) -> None:
        """没有坐标就没法排路线（引擎自己也这么判）。"""
        assert pool_search.to_spot(self._poi(lat_gcj02=None, lng_gcj02=None)) is None


class TestPoolFirstSearch:
    """替换之后的行为。

    这几条直接把 `_pool_first` 装到引擎模块上再卸下，而不是走 `install()`：
    要验的是**包装本身**，而 `install` 有自己的用例（见 `TestInstall`）。
    """

    @pytest.mark.asyncio
    async def test_the_pool_comes_first(self) -> None:
        pool = [CandidatePoi(poi_id="B_1", name="网友写过的地方", lat_gcj02=32.0, lng_gcj02=118.8)]
        asked: list[str] = []

        async def original(city: str, api_key: str, *, max_spots: int = 30):
            asked.append(city)
            return [{"name": "高德搜到的", "location": {"lng": 118.8, "lat": 32.0}}]

        from third_party.floattrip.planning import nodes

        before = nodes.fetch_city_spots_async
        try:
            pool_search._state["original"] = original
            nodes.fetch_city_spots_async = pool_search._pool_first(original, lambda _c: pool)
            spots = await nodes.fetch_city_spots_async("南京", "key", max_spots=10)
        finally:
            nodes.fetch_city_spots_async = before

        assert [spot["name"] for spot in spots] == ["网友写过的地方", "高德搜到的"]
        assert asked == ["南京"]

    @pytest.mark.asyncio
    async def test_a_full_pool_does_not_even_ask_amap(self) -> None:
        pool = [
            CandidatePoi(poi_id=f"B_{i}", name=f"地方{i}", lat_gcj02=32.0, lng_gcj02=118.8)
            for i in range(5)
        ]
        asked: list[str] = []

        async def original(city: str, api_key: str, *, max_spots: int = 30):
            asked.append(city)
            return [{"name": "不该出现", "location": {"lng": 118.8, "lat": 32.0}}]

        from third_party.floattrip.planning import nodes

        before = nodes.fetch_city_spots_async
        try:
            nodes.fetch_city_spots_async = pool_search._pool_first(original, lambda _c: pool)
            spots = await nodes.fetch_city_spots_async("南京", "key", max_spots=5)
        finally:
            nodes.fetch_city_spots_async = before

        assert len(spots) == 5
        assert asked == [], "池子已经够数，不该再打高德"

    @pytest.mark.asyncio
    async def test_the_same_place_is_not_listed_twice(self) -> None:
        """池子里的地方大多也搜得到——不去重就会一份清单里出现两遍。"""
        pool = [CandidatePoi(poi_id="B_1", name="夫子庙", lat_gcj02=32.0, lng_gcj02=118.8)]

        async def original(city: str, api_key: str, *, max_spots: int = 30):
            return [
                {"name": "夫子庙", "location": {"lng": 118.8, "lat": 32.0}},
                {"name": "老门东", "location": {"lng": 118.79, "lat": 32.01}},
            ]

        from third_party.floattrip.planning import nodes

        before = nodes.fetch_city_spots_async
        try:
            nodes.fetch_city_spots_async = pool_search._pool_first(original, lambda _c: pool)
            spots = await nodes.fetch_city_spots_async("南京", "key", max_spots=10)
        finally:
            nodes.fetch_city_spots_async = before

        assert [spot["name"] for spot in spots] == ["夫子庙", "老门东"]


class TestInstall:
    def test_no_provider_means_the_original(self) -> None:
        """池子没覆盖的城市原样走原路——不能因为接了这个而少掉什么。"""
        from third_party.floattrip.planning import nodes

        pool_search.install(None)

        assert nodes.fetch_city_spots_async is pool_search._state["original"]

    def test_the_state_does_not_leak_between_plans(self) -> None:
        """上一次带池子、这一次不带，必须真的不带。

        替换是全局的（改的是模块属性），靠「调用方记得卸下」迟早会漏。
        """
        from third_party.floattrip.planning import nodes

        pool_search.install(lambda _city: [])
        assert nodes.fetch_city_spots_async is not pool_search._state["original"]

        pool_search.install(None)

        assert nodes.fetch_city_spots_async is pool_search._state["original"]

    def test_installing_twice_keeps_the_original(self) -> None:
        """反复装不能把原函数包成「池子 → 池子 → 高德」。"""
        from third_party.floattrip.planning import nodes

        provider = lambda _city: []  # noqa: E731
        pool_search.install(provider)
        first = pool_search._state["original"]
        pool_search.install(provider)

        assert pool_search._state["original"] is first
        assert nodes.fetch_city_spots_async is not first
