"""把路段查成真实距离。

路书里的路段原先全是直线估算。真实步行距离通常是直线的 1.3 倍，
**写着「步行 1200 米」而实际要走 1800 米**，正是设计里说的「现场会很意外」。

这里守三件事：

1. **哪一个接口由直线距离决定，而那个阈值与估算器是同一个数。**
   两处各取一个数，就会出「估算器说该走、路径规划说该打车」的自相矛盾。
2. **路段挂在起点那一项上**，且带一个自失效的键（`leg_to_item_id`）。
   行程一改就对不上，路书自然退回估算——不做「行程变了要清缓存」的记账。
3. **顺序与路书一致**。天项之间「谁在前」如果两边算法不同，路段会挂到
   错误的一对端点之间，而这种错在页面上看起来完全正常。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lushu.adapters.route import RouteError, RoutePlan
from lushu.domain.roadbook import leg_fingerprint
from lushu.services import leg_planner as lp
from lushu.services import roadbook_service as rs
from lushu.store import connect, initialize, transaction

NOW = "2026-09-13T10:00:00"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "legs.db"
    initialize(path)
    return path


def _trip(db: Path, *items: tuple[str, str | None, float | None, float | None]) -> None:
    """建一份行程。每个 item 是（标题, 开始时刻, lat, lng），kind 都是 poi。

    景点坐标写进 `poi` 表——**历史行程就是这个形状**（迁移 10 才让天项自己记）。
    """
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO city (adcode, name, updated_at) VALUES ('320100', '南京', ?)",
            (NOW,),
        )
        conn.execute(
            "INSERT INTO trip (id, name, start_date, created_at, updated_at) "
            "VALUES ('trip_1', '南京一日', '2026-11-12', ?, ?)",
            (NOW, NOW),
        )
        conn.execute(
            "INSERT INTO city_stay (id, trip_id, city_adcode, city_name, seq, stay_days) "
            "VALUES ('cs_1', 'trip_1', '320100', '南京', 0, 1)"
        )
        conn.execute(
            "INSERT INTO day (id, trip_id, city_stay_id, date, seq_in_stay) "
            "VALUES ('day_1', 'trip_1', 'cs_1', '2026-11-12', 0)"
        )
        for index, (title, start, lat, lng) in enumerate(items):
            if lat is None or lng is None:
                # 没有坐标的天项：通常是还没对上实体（poi 表的坐标是非空约束，
                # 所以它压根没有实体行）。这正是路书契约里那条提醒的来源。
                conn.execute(
                    "INSERT INTO day_item (id, day_id, seq, kind, title, start_time, origin) "
                    "VALUES (?, 'day_1', ?, 'poi', ?, ?, 'ai')",
                    (f"di_{index}", index, title, start),
                )
                continue
            poi_id = f"B_{index}"
            conn.execute(
                "INSERT OR REPLACE INTO poi (amap_poi_id, name, city_adcode, adcode, typecode, "
                "lat_gcj02, lng_gcj02, fetched_at) VALUES (?, ?, '320100', '320104', '110201', "
                "?, ?, ?)",
                (poi_id, title, lat, lng, NOW),
            )
            conn.execute(
                "INSERT INTO day_item (id, day_id, seq, kind, poi_id, title, start_time, origin) "
                "VALUES (?, 'day_1', ?, 'poi', ?, ?, ?, 'ai')",
                (f"di_{index}", index, poi_id, title, start),
            )


def _plan(**overrides: object) -> RoutePlan:
    base: dict[str, object] = {"mode": "walk", "distance_m": 1400, "duration_min": 19}
    base.update(overrides)
    return RoutePlan(**base)  # type: ignore[arg-type]


class TestPendingLegs:
    def test_finds_every_consecutive_pair(self, db: Path) -> None:
        _trip(db, ("夫子庙", "09:00", 32.0209, 118.7886), ("老门东", "14:00", 32.0116, 118.7876))

        slots = lp.pending_legs(conn=connect(db))

        assert len(slots) == 1
        assert slots[0].from_title == "夫子庙"
        assert slots[0].to_title == "老门东"
        assert slots[0].straight_m > 900  # 实测一千米上下

    def test_uses_the_roadbook_order(self, db: Path) -> None:
        """顺序与路书一致。两边算法不同，路段会挂到错误的一对端点之间。"""
        _trip(
            db,
            ("晚的", "14:00", 32.0116, 118.7876),
            ("早的", "09:00", 32.0209, 118.7886),
        )

        slots = lp.pending_legs(conn=connect(db))

        assert [slot.from_title for slot in slots] == ["早的"]

    def test_items_without_time_go_last(self, db: Path) -> None:
        _trip(
            db,
            ("没定时刻的", None, 32.0300, 118.7900),
            ("九点的", "09:00", 32.0209, 118.7886),
        )

        slots = lp.pending_legs(conn=connect(db))

        assert slots[0].from_title == "九点的"
        assert slots[0].to_title == "没定时刻的"

    def test_a_pair_missing_coordinates_is_skipped(self, db: Path) -> None:
        """两端都要有坐标才算得出来——这是路书契约里的老规矩。"""
        _trip(db, ("有坐标的", "09:00", 32.0209, 118.7886), ("没坐标的", "14:00", None, None))

        assert lp.pending_legs(conn=connect(db)) == []

    def test_chooses_walking_by_the_same_threshold_as_the_estimator(self, db: Path) -> None:
        """阈值必须与估算器是同一个数，否则会出现自相矛盾的建议。"""
        assert rs.WALK_LIMIT_M == rs.NEEDS_LEG_METRES

    def test_a_stored_leg_is_not_pending_again(self, db: Path) -> None:
        """重跑要幂等。"""
        _trip(db, ("夫子庙", "09:00", 32.0209, 118.7886), ("老门东", "14:00", 32.0116, 118.7876))
        report = lp.plan_legs(lp.pending_legs(conn=connect(db)), walk=lambda _s: _plan(), pause=0)
        lp.apply_fixes(report.fixes, conn=connect(db))

        assert lp.pending_legs(conn=connect(db)) == []

    def test_moving_the_item_to_another_entity_invalidates_the_leg(self, db: Path) -> None:
        """**只记「通向哪一项」挡不住坐标变化。**

        `ls align recheck` 会把天项挪到另一个实体上（`UPDATE day_item SET poi_id`），
        坐标随之改变而 id 没变。那时旧路段还在，只是已经不是这两个地方之间的
        距离了——**一条错的距离，在路书上看起来和一个对的一模一样**。
        """
        _trip(db, ("夫子庙", "09:00", 32.0209, 118.7886), ("老门东", "14:00", 32.0116, 118.7876))
        report = lp.plan_legs(lp.pending_legs(conn=connect(db)), walk=lambda _s: _plan(), pause=0)
        lp.apply_fixes(report.fixes, conn=connect(db))
        assert lp.pending_legs(conn=connect(db)) == []

        # 对齐把「老门东」挪到了另一个实体上：换个 poi_id，坐标也就换了
        with transaction(db) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO poi (amap_poi_id, name, city_adcode, adcode, typecode, "
                "lat_gcj02, lng_gcj02, fetched_at) VALUES "
                "('B_elsewhere', '老门东景区', '320100', '320104', '110201', 32.0500, 118.8000, ?)",
                (NOW,),
            )
            conn.execute("UPDATE day_item SET poi_id = 'B_elsewhere' WHERE id = 'di_1'")

        assert len(lp.pending_legs(conn=connect(db))) == 1

    def test_changing_the_itinerary_invalidates_the_stored_leg(self, db: Path) -> None:
        """**自失效的键**：行程一改，旧路段就对不上，自动退回待办。

        不做「行程变了要清缓存」的记账——那种记账迟早会漏，而漏掉的后果是
        路书里凭空多出一段属于旧行程的路。
        """
        _trip(db, ("夫子庙", "09:00", 32.0209, 118.7886), ("老门东", "14:00", 32.0116, 118.7876))
        report = lp.plan_legs(lp.pending_legs(conn=connect(db)), walk=lambda _s: _plan(), pause=0)
        lp.apply_fixes(report.fixes, conn=connect(db))

        # 中间插一项：原先「夫子庙 → 老门东」的路段不再指向下一项
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO day_item (id, day_id, seq, kind, title, start_time, origin, "
                "lat_gcj02, lng_gcj02) VALUES "
                "('di_new', 'day_1', 1, 'meal', '蒋有记锅贴', '12:00', 'ai', 32.0120, 118.7880)"
            )

        slots = lp.pending_legs(conn=connect(db))

        assert [(slot.from_title, slot.to_title) for slot in slots] == [
            ("夫子庙", "蒋有记锅贴"),
            ("蒋有记锅贴", "老门东"),
        ]


class TestPlanLegs:
    def _one(self, db: Path) -> lp.LegSlot:
        _trip(db, ("夫子庙", "09:00", 32.0209, 118.7886), ("老门东", "14:00", 32.0116, 118.7876))
        return lp.pending_legs(conn=connect(db))[0]

    def test_short_legs_ask_for_walking(self, db: Path) -> None:
        slot = self._one(db)

        report = lp.plan_legs([slot], walk=lambda _s: _plan(), drive=lambda _s: _plan(mode="taxi"), pause=0)

        assert report.resolved[0].plan is not None
        assert report.resolved[0].plan.mode == "walk"

    def test_long_legs_ask_for_driving(self, db: Path) -> None:
        """超过步行上限的段不该去问步行路线——那答案没意义。"""
        _trip(db, ("夫子庙", "09:00", 32.0209, 118.7886), ("禄口机场", "14:00", 31.7400, 118.8700))
        slot = lp.pending_legs(conn=connect(db))[0]
        assert slot.walk is False
        asked: list[str] = []

        def walk(_slot: lp.LegSlot) -> RoutePlan:
            asked.append("walk")
            return _plan()

        def drive(_slot: lp.LegSlot) -> RoutePlan:
            asked.append("drive")
            return _plan(mode="taxi", distance_m=41000, duration_min=45)

        report = lp.plan_legs([slot], walk=walk, drive=drive, pause=0)

        assert asked == ["drive"]
        assert report.resolved[0].plan is not None
        assert report.resolved[0].plan.mode == "taxi"

    def test_a_failure_is_reported_not_guessed(self, db: Path) -> None:
        """查不到就说查不到，**不要退回一个编出来的数**。

        路书里的路段如果一半是真的、一半是估的，而两者看起来一样，
        用户没法判断哪一段能信。
        """
        slot = self._one(db)

        def boom(_slot: lp.LegSlot) -> RoutePlan:
            raise RouteError("DAILY_QUERY_OVER_LIMIT")

        report = lp.plan_legs([slot], walk=boom, drive=boom, pause=0)

        assert report.resolved == []
        assert "DAILY_QUERY_OVER_LIMIT" in report.unresolved[0].reason

    def test_our_own_bugs_are_not_dressed_up_as_api_failures(self, db: Path) -> None:
        """只接 `RouteError`：签名写错之类的 bug 该炸就炸。"""
        slot = self._one(db)

        def broken(_slot: lp.LegSlot) -> RoutePlan:
            raise TypeError("takes 1 positional argument")

        with pytest.raises(TypeError):
            lp.plan_legs([slot], walk=broken, drive=broken, pause=0)

    def test_does_not_sleep_before_the_first_call(self, db: Path) -> None:
        _trip(
            db,
            ("夫子庙", "09:00", 32.0209, 118.7886),
            ("老门东", "14:00", 32.0116, 118.7876),
            ("中华门", "16:00", 32.0090, 118.7830),
        )
        slept: list[float] = []

        lp.plan_legs(
            lp.pending_legs(conn=connect(db)),
            walk=lambda _s: _plan(),
            drive=lambda _s: _plan(mode="taxi"),
            pause=0.4,
            sleep=slept.append,
        )

        assert slept == [0.4]


class TestApplyLegs:
    def _slot(self, db: Path) -> lp.LegSlot:
        _trip(db, ("夫子庙", "09:00", 32.0209, 118.7886), ("老门东", "14:00", 32.0116, 118.7876))
        return lp.pending_legs(conn=connect(db))[0]

    def test_writes_to_the_starting_item(self, db: Path) -> None:
        """路段是「从这一项出发怎么去下一项」，挂在起点上。

        挂到终点就分不清「到达」与「出发」了。
        """
        slot = self._slot(db)
        fix = lp.LegFix(slot=slot, plan=_plan(distance_m=1420, duration_min=17))

        changed = lp.apply_fixes([fix], conn=connect(db))

        assert changed == [slot.from_item_id]
        row = connect(db).execute(
            "SELECT leg_mode, leg_distance_m, leg_duration_min, leg_key FROM day_item "
            "WHERE id = ?",
            (slot.from_item_id,),
        ).fetchone()
        assert row["leg_mode"] == "walk"
        assert row["leg_distance_m"] == 1420
        assert row["leg_duration_min"] == 17
        # 指纹覆盖两端坐标与通向哪一项——这三样任何一样变了，路段就不再作数
        assert row["leg_key"] == leg_fingerprint(
            from_lat=slot.from_lat,
            from_lng=slot.from_lng,
            to_lat=slot.to_lat,
            to_lng=slot.to_lng,
            to_ref=slot.to_item_id,
        )

    def test_unresolved_fixes_write_nothing(self, db: Path) -> None:
        slot = self._slot(db)
        assert lp.apply_fixes([lp.LegFix(slot=slot, reason="查不到")], conn=connect(db)) == []


class TestRoadbookUsesTheStoredLeg:
    def _stored(self, db: Path) -> None:
        _trip(db, ("夫子庙", "09:00", 32.0209, 118.7886), ("老门东", "14:00", 32.0116, 118.7876))
        report = lp.plan_legs(
            lp.pending_legs(conn=connect(db)),
            walk=lambda _s: _plan(distance_m=1420, duration_min=17),
            pause=0,
        )
        lp.apply_fixes(report.fixes, conn=connect(db))

    def test_the_real_distance_replaces_the_estimate(self, db: Path) -> None:
        self._stored(db)

        book, _ = rs.assemble("trip_1", conn=connect(db))
        leg = book.days[0].legs[0]

        assert leg is not None
        assert leg.distance_m == 1420
        assert leg.mode == "walk"

    def test_a_real_leg_carries_no_estimate_caveat(self, db: Path) -> None:
        """「（直线距离估算，实际路程更长）」挂在真数据上，用户会以为它也不准。"""
        self._stored(db)

        book, _ = rs.assemble("trip_1", conn=connect(db))
        leg = book.days[0].legs[0]

        assert leg is not None
        assert leg.note is None

    def test_it_still_has_a_navigation_link(self, db: Path) -> None:
        """真实路段同样要能在手机上点开——导航链接是地图被砍掉之后留下的那部分。"""
        self._stored(db)

        book, _ = rs.assemble("trip_1", conn=connect(db))
        leg = book.days[0].legs[0]

        assert leg is not None
        assert leg.nav_url is not None
        # 坐标取自实体表（历史行程的景点坐标在那儿），不是天项那两列
        assert "118.7886" in leg.nav_url

    def test_an_invalidated_leg_falls_back_to_the_estimate(self, db: Path) -> None:
        """行程改过之后，旧路段自己失效，路书退回估算并如实标注。"""
        self._stored(db)
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO day_item (id, day_id, seq, kind, title, start_time, origin, "
                "lat_gcj02, lng_gcj02) VALUES "
                "('di_new', 'day_1', 1, 'meal', '蒋有记锅贴', '12:00', 'ai', 32.0120, 118.7880)"
            )

        book, _ = rs.assemble("trip_1", conn=connect(db))
        first, second = book.days[0].legs

        # 第一段（夫子庙 → 新插的那一项）没有存过，是估算，带提醒
        assert first is not None
        assert first.note is not None
        assert "估算" in first.note
        # 第二段（新项 → 老门东）同样没存过
        assert second is not None
        assert second.note is not None
