"""候选池：网友推荐过的地方 vs 地图上有的地方。

M5 的验收条件是「打开城市看到的是网友推荐而非一片 POI」。这一层要守的就是
那句话——一片 POI 的问题是它们**没有观点**：高德会告诉你「这里有个公园」，
不会告诉你「这个公园傍上上去最好，白天没遮阴」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lushu.adapters.poi import PoiSearchError
from lushu.domain.knowledge import ClaimEvidence
from lushu.services import candidate_pool as cp
from lushu.services import knowledge_store as ks
from lushu.store import connect, initialize, transaction
from tests.test_pipeline import gugong, search_returning

NOW = "2026-09-13T10:00:00"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "pool.db"
    initialize(path)
    return path


def _city(db: Path, adcode: str = "110100", name: str = "北京") -> None:
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO city (adcode, name, updated_at) VALUES (?, ?, ?)",
            (adcode, name, NOW),
        )


def _poi(db: Path, poi_id: str, name: str, **overrides: object) -> None:
    """插一个 POI。坐标是 NOT NULL——排程离不开它，所以不可能缺。"""
    fields: dict[str, object] = {
        "address": None,
        "lat": 39.918,
        "lng": 116.397,
        "rating": None,
        "open_time": None,
        "city": "110100",
        "adcode": "110101",
    }
    fields.update(overrides)
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO poi (amap_poi_id, name, city_adcode, adcode, typecode, "
            "address, lat_gcj02, lng_gcj02, rating, open_time, fetched_at) "
            "VALUES (?, ?, ?, ?, '110201', ?, ?, ?, ?, ?, ?)",
            (
                poi_id,
                name,
                fields["city"],
                fields["adcode"],
                fields["address"],
                fields["lat"],
                fields["lng"],
                fields["rating"],
                fields["open_time"],
                NOW,
            ),
        )


def _poi_with_hours():
    """一个带开放时间与评分的高德候选，用来测补全。"""
    from dataclasses import replace

    return replace(gugong(), open_time="08:30-17:00", rating=4.8)


def _claim(
    db: Path,
    *,
    poi_id: str,
    text: str,
    polarity: str = "highlight",
    confidence: str = "high",
    sources: int = 3,
    facet: str = "photo",
) -> str:
    with transaction(db) as conn:
        conn.execute(
            "INSERT INTO source_document (id, site, body_text, body_sha256, imported_at, "
            "import_kind) VALUES (?, 'manual', '正文', ?, ?, 'paste')",
            (f"src_{text[:6]}", text[:8].encode().hex()[:12], NOW),
        )
        claim_id = ks.save_claim(
            conn=conn,
            subject_type="poi",
            subject_name="故宫",
            poi_id=poi_id,
            polarity=polarity,
            facet=facet,
            text=text,
            evidence=[ClaimEvidence(source_document_id=f"src_{text[:6]}", quote=text)],
            first_seen_at=NOW,
            verify_due_at=None,
        )
        # save_claim 会按证据重算置信度，这里直接按测试需要定死
        conn.execute(
            "UPDATE claim SET confidence = ?, independent_source_count = ? WHERE id = ?",
            (confidence, sources, claim_id),
        )
    return claim_id


class TestCityCandidates:
    def test_lists_places_with_claims(self, db: Path) -> None:
        _city(db)
        _poi(db, "B1", "故宫博物院")
        _claim(db, poi_id="B1", text="想拍没人的太和殿要开门就冲")

        rows = cp.city_candidates("110100", conn=connect(db))

        assert len(rows) == 1
        assert rows[0].name == "故宫博物院"
        assert rows[0].source == cp.PoolSource.KNOWLEDGE
        assert rows[0].recommended
        assert rows[0].claim_count == 1

    def test_places_without_claims_are_excluded(self, db: Path) -> None:
        """一条结论都没有的地方不进候选池。

        它对规划并不比高德的一条搜索结果更有用，混进来只会让
        「网友推荐」这个信号贬值。
        """
        _city(db)
        _poi(db, "B1", "故宫博物院")
        _poi(db, "B2", "某个没人提过的地方")
        _claim(db, poi_id="B1", text="网友提过的那个地方")

        rows = cp.city_candidates("110100", conn=connect(db))
        assert [row.name for row in rows] == ["故宫博物院"]

    def test_highlights_and_avoids_are_kept_apart(self, db: Path) -> None:
        """打卡与避坑是两类信息。混成一句含糊的话就没用了。"""
        _city(db)
        _poi(db, "B1", "故宫博物院")
        _claim(db, poi_id="B1", text="午门进神武门出", polarity="highlight")
        _claim(db, poi_id="B1", text="不卖现场票", polarity="avoid", facet="queue")

        row = cp.city_candidates("110100", conn=connect(db))[0]

        assert [item.text for item in row.highlights] == ["午门进神武门出"]
        assert [item.text for item in row.avoids] == ["不卖现场票"]

    def test_high_confidence_claims_come_first(self, db: Path) -> None:
        _city(db)
        _poi(db, "B1", "故宫博物院")
        _claim(db, poi_id="B1", text="单源说法", confidence="single_source", sources=1)
        _claim(db, poi_id="B1", text="多源说法", confidence="high", sources=5)

        row = cp.city_candidates("110100", conn=connect(db))[0]
        assert row.highlights[0].text == "多源说法"

    def test_more_recommended_places_come_first(self, db: Path) -> None:
        _city(db)
        _poi(db, "B1", "甲地")
        _poi(db, "B2", "乙地")
        _claim(db, poi_id="B1", text="甲地一")
        _claim(db, poi_id="B2", text="乙地一")
        _claim(db, poi_id="B2", text="乙地二")

        rows = cp.city_candidates("110100", conn=connect(db))
        assert [row.name for row in rows] == ["乙地", "甲地"]

    def test_empty_city_returns_nothing(self, db: Path) -> None:
        _city(db)
        assert cp.city_candidates("110100", conn=connect(db)) == []

    def test_other_cities_are_not_mixed_in(self, db: Path) -> None:
        _city(db)
        _city(db, "610100", "西安")
        _poi(db, "B1", "故宫博物院")
        _claim(db, poi_id="B1", text="北京的说法")
        _poi(db, "B9", "西安城墙", city="610100", adcode="610103")
        _claim(db, poi_id="B9", text="西安的说法")

        rows = cp.city_candidates("110100", conn=connect(db))
        assert [row.name for row in rows] == ["故宫博物院"]


class TestBookingIsAttached:
    def _rule(self, db: Path, poi_id: str, *, status: str) -> None:
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO booking_rule (poi_id, booking_required, advance_days, "
                "release_time, status, evidence_url, reviewed_at, updated_at) "
                "VALUES (?, 1, 7, '20:00', ?, ?, ?, ?)",
                (poi_id, status, "https://example.cn/" if status == "reviewed" else None,
                 "2026-01-01" if status == "reviewed" else None, NOW),
            )

    def test_reviewed_rule_reaches_the_pool(self, db: Path) -> None:
        _city(db)
        _poi(db, "B1", "故宫博物院")
        _claim(db, poi_id="B1", text="要预约")
        self._rule(db, "B1", status="reviewed")

        row = cp.city_candidates("110100", conn=connect(db))[0]
        assert row.booking_required is True
        assert row.booking_days == 7
        assert row.booking_time == "20:00"

    def test_rule_without_release_window_still_says_booking_is_required(
        self, db: Path
    ) -> None:
        """「要预约，但官方没公布放票口径」不能显示成「没有预约信息」。

        实测踩过：兵马俑那条规则就是这样（advance_days 为 None），界面上
        当时显示成「无预约信息」——等于把「必须预约」说成了「不用管」，
        正是这个项目最怕的失败模式。
        """
        _city(db)
        _poi(db, "B1", "秦始皇兵马俑博物馆")
        _claim(db, poi_id="B1", text="要预约")
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO booking_rule (poi_id, booking_required, advance_days, "
                "status, evidence_url, reviewed_at, updated_at) "
                "VALUES ('B1', 1, NULL, 'reviewed', 'https://example.cn/', '2026-01-01', ?)",
                (NOW,),
            )

        row = cp.city_candidates("110100", conn=connect(db))[0]
        assert row.booking_required is True
        assert row.booking_days is None

    def test_no_rule_is_not_the_same_as_no_booking(self, db: Path) -> None:
        """没有已复核的规则时是 `None`——「不知道」，不是「不需要」。"""
        _city(db)
        _poi(db, "B1", "故宫博物院")
        _claim(db, poi_id="B1", text="要预约")

        row = cp.city_candidates("110100", conn=connect(db))[0]
        assert row.booking_required is None

    def test_rule_saying_not_required_is_reported_as_such(self, db: Path) -> None:
        """已复核的「不需要预约」是一个结论，与「没有规则」是两回事。"""
        _city(db)
        _poi(db, "B1", "故宫博物院")
        _claim(db, poi_id="B1", text="不用预约")
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO booking_rule (poi_id, booking_required, status, "
                "evidence_url, reviewed_at, updated_at) "
                "VALUES ('B1', 0, 'reviewed', 'https://example.cn/', '2026-01-01', ?)",
                (NOW,),
            )

        row = cp.city_candidates("110100", conn=connect(db))[0]
        assert row.booking_required is False

    def test_draft_rule_does_not(self, db: Path) -> None:
        """未经复核的规则不得展示（Q10）。否则界面上会出现一条没人核过的
        放票时刻，而用户会照着它去等。"""
        _city(db)
        _poi(db, "B1", "故宫博物院")
        _claim(db, poi_id="B1", text="要预约")
        self._rule(db, "B1", status="draft")

        row = cp.city_candidates("110100", conn=connect(db))[0]
        assert row.booking_required is None
        assert row.booking_days is None
        assert row.booking_time is None


class TestFillGaps:
    """补齐的是**开放时间、评分、地址**这几样。

    坐标在库里是 NOT NULL（排程离不开它），所以不可能是缺的那一项。
    """

    def test_fills_missing_hours(self, db: Path) -> None:
        _city(db)
        _poi(db, "B1", "故宫博物院", open_time=None)
        _claim(db, poi_id="B1", text="一条结论")

        rows = cp.city_candidates("110100", conn=connect(db))
        assert rows[0].open_time is None

        filled = cp.fill_gaps(rows, fetch=lambda poi_id: _poi_with_hours())
        assert filled[0].open_time == "08:30-17:00"

    def test_does_not_overwrite_existing_values(self, db: Path) -> None:
        """ADR-0001：硬事实以官方接口为准，而库里的值本来就是从高德来的。"""
        _city(db)
        _poi(db, "B1", "故宫博物院", rating=4.9, open_time="08:00-18:00")
        _claim(db, poi_id="B1", text="一条结论")

        rows = cp.city_candidates("110100", conn=connect(db))
        filled = cp.fill_gaps(rows, fetch=lambda poi_id: _poi_with_hours())
        assert filled[0].rating == 4.9
        assert filled[0].open_time == "08:00-18:00"

    def test_missing_poi_is_left_alone(self, db: Path) -> None:
        """高德取不到就保持原样——补全是加分项，不是必经之路。"""
        _city(db)
        _poi(db, "B1", "故宫博物院", open_time=None)
        _claim(db, poi_id="B1", text="一条结论")

        rows = cp.city_candidates("110100", conn=connect(db))
        filled = cp.fill_gaps(rows, fetch=lambda poi_id: None)
        assert filled[0].open_time is None


class TestAmapFallback:
    def test_uncovered_city_falls_back_to_amap(self, db: Path) -> None:
        _city(db, "530100", "昆明")
        pool = cp.pool_for_city(
            "530100",
            city_name="昆明",
            conn=connect(db),
            search=search_returning(gugong()),
        )

        assert len(pool.candidates) == 1
        assert pool.candidates[0].source == cp.PoolSource.AMAP
        assert not pool.candidates[0].recommended

    def test_knowledge_base_wins_over_amap(self, db: Path) -> None:
        """有知识库就不回落。两条路的结果**不混**——混了的话，
        「网友推荐」这个信号就没了。"""
        _city(db)
        _poi(db, "B1", "故宫博物院")
        _claim(db, poi_id="B1", text="网友说的")

        pool = cp.pool_for_city(
            "110100",
            city_name="北京",
            conn=connect(db),
            search=search_returning(gugong()),
        )

        assert [item.name for item in pool.candidates] == ["故宫博物院"]
        assert all(item.source == cp.PoolSource.KNOWLEDGE for item in pool.candidates)

    def test_amap_failure_is_reported_not_raised(self, db: Path) -> None:
        """补全只是加分项，失败不该让整页报错，但也不能静默。

        桩抛的是**真适配器会抛的那一种**：`_get_payload` 把连接错误、HTTP 错误、
        非法 JSON、缺 key 全部包成 `PoiSearchError`。原先这个桩抛的是
        `RuntimeError`，于是它顺手盖住了一件事——`pool_for_city` 当时接的是
        `Exception`，我们自己的接线错误也会被显示成「高德补全没成功」。
        """
        _city(db, "530100", "昆明")

        def boom(keywords: str, city: str):
            raise PoiSearchError("高德 POI 搜索请求失败：ConnectError")

        pool = cp.pool_for_city("530100", city_name="昆明", conn=connect(db), search=boom)
        assert pool.candidates == []
        assert pool.amap_error is not None
        assert "ConnectError" in pool.amap_error

    def test_our_own_bug_is_not_reported_as_an_amap_failure(self, db: Path) -> None:
        """回调签名接错了是我们的事，必须炸出来，不能算成高德的失败。

        这不是假想的错：`_live_search()` 原先直接把 `search_pois` 返回，而
        `amap_fallback` 按位置传两个参数，于是那条路每次调用都是一个
        `TypeError`，被宽 catch 收成一句「高德补全没成功：TypeError…」。
        「知识库没覆盖的城市直接搜高德」（设计 5.1）**从来没有工作过**，
        而界面上只是少了几条候选。
        """
        _city(db, "530100", "昆明")

        def wrong_shape(keywords: str, city: str, extra: str):
            raise AssertionError("不该走到这里")

        with pytest.raises(TypeError):
            cp.pool_for_city("530100", city_name="昆明", conn=connect(db), search=wrong_shape)

    def test_live_search_matches_the_call_shape(self, monkeypatch) -> None:
        """`_live_search()` 返回的东西必须能按 `(关键词, 城市)` 调用。

        真适配器的城市是关键字参数（`search_pois(keywords, *, city, ...)`），
        所以桩也照这个样子定义——用它才能量出接线对不对。别处四处
        （booking_store / meal_coords / pipeline / realign）都是
        `lambda keywords, city: search_pois(keywords, city=city)`，
        只有候选池这条漏了。
        """
        seen: dict[str, str] = {}

        def fake_search_pois(keywords: str, *, city: str, **kwargs: object):
            seen["keywords"] = keywords
            seen["city"] = city
            return "搜索结果"

        monkeypatch.setattr("lushu.adapters.poi.search_pois", fake_search_pois)

        searcher = cp._live_search()

        assert searcher("景点", "昆明") == "搜索结果"
        assert seen == {"keywords": "景点", "city": "昆明"}

    def test_no_search_and_no_data_gives_an_empty_pool(self, db: Path) -> None:
        _city(db, "530100", "昆明")
        pool = cp.pool_for_city("530100", conn=connect(db))
        assert pool.candidates == []
        assert pool.amap_error is None


class TestCoverage:
    def test_thin_coverage_is_reported(self, db: Path) -> None:
        """知识库还很薄时要说实话，不能让空池子冒充「这城市没什么可去的」。"""
        _city(db)
        _poi(db, "B1", "故宫博物院")
        _claim(db, poi_id="B1", text="唯一的一条")

        pool = cp.pool_for_city("110100", conn=connect(db))
        assert pool.recommended_count == 1
        assert pool.covered is False

    def test_enough_coverage_is_reported_as_covered(self, db: Path) -> None:
        _city(db)
        for index in range(cp.THIN_COVERAGE):
            _poi(db, f"B{index}", f"第{index}处")
            _claim(db, poi_id=f"B{index}", text=f"第{index}处的说法")

        pool = cp.pool_for_city("110100", conn=connect(db))
        assert pool.covered is True
