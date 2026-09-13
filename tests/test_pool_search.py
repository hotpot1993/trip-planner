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


class TestPlanningPathDoesNotTouchTheNetwork:
    """排程这条路上**一次网都不打**。

    这一条原先不是这样。`pool_pois` 会给没有评分的地方补一次高德详情（每个
    缺评分的地方一次请求，无上限、无节流，串行），理由是「引擎有一道评分门禁，
    没评分的地方会被丢掉」。那个理由后来消失了两遍：

    1. `c7bfeb4` 让池子条目**豁免**了评分门禁——门禁只作用在「高德补全」那一部分
       （见下面 `TestTheSearchNode` 里那条「高德那部分照旧受管」）。
    2. 补的那个数也**到不了引擎**：`to_spot` 优先用高德的原始响应转换，而高德对
       这类地方给的 `biz_ext.rating` 是空列表（实测夫子庙、洒金桥都是），
       所以补进 `poi.rating` 列的值在 `to_spot` 那里被丢掉。

    两件事加起来，那些请求换不来排程上的任何差别，只换来延迟与配额消耗——
    真库里 100 个 POI 有 26 个没评分，换成新城市的池子最坏是 60 次串行请求，
    而它发生在**每一次规划请求**里。
    """

    def _unrated(self, db: Path) -> None:
        _poi_with_claim(db, "B_1", "夫子庙")
        with transaction(db) as conn:
            conn.execute("UPDATE poi SET rating = NULL WHERE amap_poi_id = 'B_1'")

    def test_pool_pois_never_calls_amap(self, db: Path, monkeypatch) -> None:
        self._unrated(db)

        def boom(*_args: object, **_kwargs: object):
            raise AssertionError("规划这条路上不该有任何网络请求")

        monkeypatch.setattr("lushu.adapters.poi.fetch_poi", boom)
        monkeypatch.setattr("lushu.adapters.poi.search_pois", boom)

        found = candidate_pool.pool_pois("南京", conn=connect(db))

        assert [poi.name for poi in found] == ["夫子庙"]

    def test_a_missing_rating_is_reported_as_missing(self, db: Path) -> None:
        """缺评分是现状，不是错误——不补、不编，如实带出去。"""
        self._unrated(db)

        found = candidate_pool.pool_pois("南京", conn=connect(db))

        assert found[0].rating is None

    def test_a_place_without_a_rating_is_still_usable(self, db: Path) -> None:
        """没有评分的地方仍然能变成引擎要的 spot——评分不是必需品。"""
        from lushu.engine.pool_search import to_spot

        self._unrated(db)
        poi = candidate_pool.pool_pois("南京", conn=connect(db))[0]

        spot = to_spot(poi)

        assert spot is not None
        assert spot["name"] == "夫子庙"
        assert spot["location"]["lat"] == poi.lat_gcj02


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


class _FakeState:
    """引擎状态的替身：节点只读这四个字段。"""

    def __init__(self, **fields: object) -> None:
        self.destination = fields.get("destination")
        self.max_spots = fields.get("max_spots", 30)
        self.min_rating = fields.get("min_rating", 4.0)
        self.history = fields.get("history", [])


class _PoolNode:
    """跑一遍替换后的节点，喂进去一个假的「高德搜索」结果。"""

    def __init__(
        self,
        *,
        pool: list[CandidatePoi],
        amap: list[dict],
        min_rating: float = 4.0,
        pool_only_from: int = 99,
    ):
        self.pool = pool
        self.amap = amap
        self.min_rating = min_rating
        # 默认给一个够不着的门槛：这一组测的是「池子在前 + 池子豁免门禁」，
        # 封闭世界那条路有它自己的用例（传小一点的数）。
        self.pool_only_from = pool_only_from
        self.asked: list[str] = []

    def run(self) -> dict:
        import asyncio

        async def amap_search(city: str, api_key: str, *, max_spots: int = 30):
            self.asked.append(city)
            return list(self.amap)

        before = pool_search._state.get("original")
        pool_search._state["original"] = amap_search
        try:
            node = pool_search._pool_exempt_node(
                lambda _city: list(self.pool), pool_only_from=self.pool_only_from
            )
            state = _FakeState(
                destination="南京", max_spots=30, min_rating=self.min_rating, history=[]
            )
            return asyncio.run(node(state))
        finally:
            pool_search._state["original"] = before


def _pool_poi(name: str) -> CandidatePoi:
    return CandidatePoi(poi_id=f"B_{name}", name=name, lat_gcj02=32.0, lng_gcj02=118.8)


class TestTheSearchNode:
    """替换后的节点：池子在前、池子条目不受评分门禁管。

    引擎原先那道 `filter_by_rating` 的意图是滤掉高德搜出来的噪声（评分缺失的
    往往是个广场、停车场、上车点）。但池子里的地方不是「高德搜出来的」——
    它们是网友真的写过的地方，评分缺失只是高德没给（实测**夫子庙就没有**）。
    拿滤噪声的规则去滤已经认定过的条目，是判据用错了对象。

    代价是那个节点的几行逻辑在我们这边留了一份副本：所以这里既钉「池子豁免」，
    也钉「高德那部分照旧被滤」——只钉一半，副本就可能在另一半点上悄悄走偏。
    """

    def test_a_pool_entry_without_a_rating_survives(self) -> None:
        """夫子庙：高德对它根本没有评分，但网友写过，所以它要活下来。"""
        result = _PoolNode(
            pool=[_pool_poi("夫子庙")],
            amap=[
                {"name": "夫子庙", "rating": None},
                {"name": "中山陵景区", "rating": 4.9},
                {"name": "某个广场", "rating": 3.5},
            ],
        ).run()

        assert [spot["name"] for spot in result["pois"]] == ["夫子庙", "中山陵景区"]

    def test_amap_spots_are_still_filtered(self) -> None:
        """高德那部分照旧受管——不然这道门禁就等于拆了。"""
        result = _PoolNode(
            pool=[],
            amap=[
                {"name": "中山陵景区", "rating": 4.9},
                {"name": "某个广场", "rating": 3.5},
                {"name": "没评分的广场", "rating": None},
            ],
        ).run()

        assert [spot["name"] for spot in result["pois"]] == ["中山陵景区"]

    def test_the_pool_comes_first(self) -> None:
        result = _PoolNode(
            pool=[_pool_poi("老门东")],
            amap=[
                {"name": "中山陵景区", "rating": 4.9},
                {"name": "老门东", "rating": 4.8},
            ],
        ).run()

        assert [spot["name"] for spot in result["pois"]] == ["老门东", "中山陵景区"]

    def test_a_place_is_not_listed_twice(self) -> None:
        """池子里的地方大多也搜得到：不同来源的同名条目只留池子那一份。"""
        result = _PoolNode(
            pool=[_pool_poi("夫子庙")],
            amap=[{"name": "夫子庙", "rating": 4.8}],
        ).run()

        assert [spot["name"] for spot in result["pois"]] == ["夫子庙"]

    def test_a_full_pool_still_asks_amap(self) -> None:
        """池子满不等于不用问高德——**行程的备选要够多**，而池子只有几个。

        这一条与上一版不同：那时「池子够数就不打高德」是省一次请求；
        现在池子与高德是**分开编号**的，高德那部分再多也要补齐（它只是
        排在池子后面、且要过评分门禁）。
        """
        node = _PoolNode(
            pool=[_pool_poi("老门东")],
            amap=[{"name": "中山陵景区", "rating": 4.9}],
        )

        result = node.run()

        assert node.asked == ["南京"]
        assert [spot["name"] for spot in result["pois"]] == ["老门东", "中山陵景区"]

    def test_the_history_note_says_what_happened(self) -> None:
        """阶段日志里要能看出「池子几个、高德几个、滤掉几个」。"""
        result = _PoolNode(
            pool=[_pool_poi("夫子庙")],
            amap=[
                {"name": "夫子庙", "rating": None},
                {"name": "某个广场", "rating": 3.5},
            ],
        ).run()

        note = result["history"][-1]
        assert "候选池 1 个" in note
        assert "不受评分门禁管" in note
        assert "滤掉 1" in note


class TestTheClosedWorld:
    """池子够用时就**只用池子**（设计 5.1：「排程不得引入候选池之外的景点」）。

    两句要一起读：高德「补全」是在知识库还没覆盖这座城市的时候（同节原文：
    「知识库尚未覆盖的城市仍可直接搜高德」）。池子够用还把高德那一片塞进去，
    后果实测过——LLM 从里面挑了「不老村」「水墨大埝旅游区」，两个都在四十
    公里外的郊区，而池子里明明有五个网友写过的地方。
    """

    def test_a_big_enough_pool_does_not_even_ask_amap(self) -> None:
        node = _PoolNode(
            pool=[_pool_poi(name) for name in ("中山陵景区", "南京博物院", "夫子庙")],
            amap=[{"name": "不老村", "rating": 4.7}],
            pool_only_from=3,
        )

        result = node.run()

        assert node.asked == [], "池子够用时不该再打高德——打就会把池子外的塞进清单"
        assert [spot["name"] for spot in result["pois"]] == [
            "中山陵景区",
            "南京博物院",
            "夫子庙",
        ]

    def test_a_thin_pool_falls_back_to_amap(self) -> None:
        """池子不够时照旧补全：城市只有一两条时封闭世界会把行程排空。"""
        node = _PoolNode(
            pool=[_pool_poi("中山陵景区")],
            amap=[{"name": "某个广场", "rating": 4.5}],
            pool_only_from=3,
        )

        result = node.run()

        assert node.asked == ["南京"]
        assert [spot["name"] for spot in result["pois"]] == ["中山陵景区", "某个广场"]

    def test_an_empty_pool_is_the_old_behaviour(self) -> None:
        """知识库没覆盖的城市：与从前完全一样，纯高德搜索。"""
        node = _PoolNode(pool=[], amap=[{"name": "某个景点", "rating": 4.6}], pool_only_from=3)

        result = node.run()

        assert node.asked == ["南京"]
        assert [spot["name"] for spot in result["pois"]] == ["某个景点"]

    def test_the_note_says_the_world_was_closed(self) -> None:
        """阶段日志要能看出「这次为什么没有高德那一堆」。"""
        result = _PoolNode(
            pool=[_pool_poi(name) for name in ("甲", "乙", "丙")],
            amap=[],
            pool_only_from=3,
        ).run()

        note = result["history"][-1]
        assert "封闭世界门槛" in note
        assert "不引入池子之外的景点" in note


class TestInstall:
    def test_no_provider_means_the_original(self) -> None:
        """池子没覆盖的城市原样走原路——不能因为接了这个而少掉什么。"""
        from third_party.floattrip.planning import graph, nodes

        pool_search.install(None)

        assert nodes.attraction_search_node is pool_search._state["original_node"]
        assert graph.attraction_search_node is pool_search._state["original_graph_node"]

    def test_the_graph_gets_the_replaced_node_too(self) -> None:
        """`graph.py` 自己持有一份引用（模块级 import），不换它等于没换。

        这一条是**真机撞出来的**：图是在函数里现搭的，用的是
        `graph.attraction_search_node`，只换 `nodes` 上那个不起作用。
        """
        from third_party.floattrip.planning import graph, nodes

        pool_search.install(lambda _city: [])

        assert nodes.attraction_search_node is graph.attraction_search_node
        assert nodes.attraction_search_node is not pool_search._state["original_node"]

    def test_the_state_does_not_leak_between_plans(self) -> None:
        """上一次带池子、这一次不带，必须真的不带。"""
        from third_party.floattrip.planning import graph, nodes

        pool_search.install(lambda _city: [])
        pool_search.install(None)

        assert nodes.attraction_search_node is pool_search._state["original_node"]
        assert graph.attraction_search_node is pool_search._state["original_graph_node"]

    def test_installing_twice_keeps_the_original(self) -> None:
        """反复装不能把原节点包成「池子 → 池子 → 高德」。"""
        from third_party.floattrip.planning import nodes

        provider = lambda _city: []  # noqa: E731
        pool_search.install(provider)
        first = pool_search._state["original"]
        pool_search.install(provider)

        assert pool_search._state["original"] is first
        assert nodes.attraction_search_node is not pool_search._state["original_node"]
