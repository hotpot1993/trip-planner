"""给餐饮天项补坐标。

这条命令存在的理由是**修好算法不等于修好数据**：转换层已经会保留餐厅坐标了，
但那只对之后生成的行程生效，迁移不回填历史数据。于是路书上 7 处
「两处之间没有路段说明」全部落在餐饮项上，而它们的坐标恰好全是空的。

所以测试分两半：

1. **判据够保守**——对不上实体、搜到同名小店、城市没 adcode，
   都必须留空。路书的坐标是唯一的空间线索（没有地图），补一个错的比留空更坏。
2. **幂等且不覆盖**——重跑不重复写，别人在这中间补过就不动它。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lushu.domain.poi import CandidatePoi
from lushu.services import meal_coords as mc
from lushu.store import connect, initialize, transaction
from tests.test_pipeline import search_returning

NOW = "2026-09-13T10:00:00"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "meal_coords.db"
    initialize(path)
    return path


def _trip_with_meals(db: Path, *meals: tuple[str, float | None, float | None]) -> None:
    """建一份带餐饮项的行程。每个 meal 是（店名, lat, lng）。"""
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO city (adcode, name, updated_at) VALUES ('320100', '南京', ?)",
            (NOW,),
        )
        conn.execute(
            "INSERT INTO trip (id, name, start_date, created_at, updated_at) "
            "VALUES ('trip_1', '南京三日', '2026-11-12', ?, ?)",
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
        for index, (title, lat, lng) in enumerate(meals):
            conn.execute(
                "INSERT INTO day_item (id, day_id, seq, kind, title, origin, lat_gcj02, lng_gcj02) "
                "VALUES (?, 'day_1', ?, 'meal', ?, 'ai', ?, ?)",
                (f"di_{index}", index, title, lat, lng),
            )


def _shop(
    poi_id: str = "B_NJ_1",
    name: str = "南京大牌档（中山陵店）",
    *,
    adcode: str = "320102",
    lat: float | None = 32.0545,
    lng: float | None = 118.8541,
    address: str | None = "中山门外石象路 7 号",
) -> CandidatePoi:
    return CandidatePoi(
        poi_id=poi_id,
        name=name,
        typecode="050100",
        type_name="餐饮服务;中餐厅",
        adcode=adcode,
        city_name="南京市",
        address=address,
        lng_gcj02=lng,
        lat_gcj02=lat,
    )


class TestPendingMeals:
    def test_finds_meals_without_coordinates(self, db: Path) -> None:
        _trip_with_meals(db, ("南京大牌档", None, None), ("绿柳居", None, None))

        slots = mc.pending_meals(conn=connect(db))

        assert [slot.title for slot in slots] == ["南京大牌档", "绿柳居"]
        assert slots[0].city_name == "南京"
        assert slots[0].city_adcode == "320100"

    def test_meals_that_already_have_coordinates_are_left_alone(self, db: Path) -> None:
        """重跑要幂等：已经补过的不能再出现在待办里。"""
        _trip_with_meals(db, ("南京大牌档", 32.05, 118.85), ("绿柳居", None, None))

        assert [slot.title for slot in mc.pending_meals(conn=connect(db))] == ["绿柳居"]

    def test_can_be_limited_to_one_trip(self, db: Path) -> None:
        _trip_with_meals(db, ("南京大牌档", None, None))

        assert mc.pending_meals(conn=connect(db), trip_id="trip_other") == []


class TestResolution:
    def test_a_clean_hit_is_kept(self, db: Path) -> None:
        _trip_with_meals(db, ("南京大牌档（中山陵店）", None, None))
        slot = mc.pending_meals(conn=connect(db))[0]

        fix = mc.resolve_meal(slot, search=search_returning(_shop()))

        assert fix.resolved is True
        assert fix.lat_gcj02 == 32.0545
        assert fix.address == "中山门外石象路 7 号"

    def test_nothing_found_stays_empty(self, db: Path) -> None:
        """通用菜名搜不到是常态，留空并说清是「搜不到」。"""
        _trip_with_meals(db, ("鸭血粉丝汤", None, None))
        slot = mc.pending_meals(conn=connect(db))[0]

        fix = mc.resolve_meal(slot, search=search_returning())

        assert fix.resolved is False
        assert "没有搜到" in fix.reason

    def test_a_candidate_in_another_city_is_refused(self, db: Path) -> None:
        """城市门禁：搜到的是别的城市的同名店，绝不能写进来。

        这是最危险的一种——名字完全一样，坐标却在几百公里外，
        路书会让人「步行 800 米」走到另一个省。
        """
        _trip_with_meals(db, ("南京大牌档", None, None))
        slot = mc.pending_meals(conn=connect(db))[0]
        elsewhere = _shop("B_BJ_1", "南京大牌档", adcode="110101", lat=39.9, lng=116.4)

        fix = mc.resolve_meal(slot, search=search_returning(elsewhere))

        assert fix.resolved is False
        assert "都不在南京" in fix.reason

    def test_a_match_without_coordinates_stays_empty(self, db: Path) -> None:
        """对上了实体但高德没给坐标——写不进去，也不该写个 0 进去。"""
        _trip_with_meals(db, ("南京大牌档", None, None))
        slot = mc.pending_meals(conn=connect(db))[0]

        fix = mc.resolve_meal(
            slot, search=search_returning(_shop(lat=None, lng=None))
        )

        assert fix.resolved is False
        assert "没给坐标" in fix.reason

    def test_missing_adcode_says_so(self, db: Path) -> None:
        """城市没入库时，align 会一律拒绝——那看起来像「名字对不上」。

        这条钉住的是**报出来的原因要说真话**：不然人会去改名字，而问题在城市。
        """
        _trip_with_meals(db, ("南京大牌档", None, None))
        slot = mc.pending_meals(conn=connect(db))[0]
        homeless = mc.MealSlot(
            item_id=slot.item_id,
            trip_id=slot.trip_id,
            day_date=slot.day_date,
            title=slot.title,
            city_name="某某城",
            city_adcode=None,
        )

        fix = mc.resolve_meal(homeless, search=search_returning(_shop()))

        assert fix.resolved is False
        assert "adcode" in fix.reason

    def test_a_search_failure_is_not_a_missing_place(self, db: Path) -> None:
        """网络/限流挂了与「查不到这个地方」是两件事，不能混成一句话。"""
        _trip_with_meals(db, ("南京大牌档", None, None))
        slot = mc.pending_meals(conn=connect(db))[0]

        def boom(_keywords: str, _city: str):
            raise RuntimeError("CUQPS_HAS_EXCEEDED_THE_LIMIT")

        fix = mc.resolve_meal(slot, search=boom)

        assert fix.resolved is False
        assert "搜索失败" in fix.reason
        assert "CUQPS" in fix.reason

    def test_two_identical_shops_are_left_empty(self, db: Path) -> None:
        """**并列第一就是不唯一，不猜。** 这条是这套判据的主力。

        「鸭血粉丝汤」这类通用菜名会搜出一串同名小店，全部同分。随便挑一个写进去，
        路书就会让人「步行 300 米」走到另一家店，而且从页面上完全看不出来。
        """
        _trip_with_meals(db, ("鸭血粉丝汤", None, None))
        slot = mc.pending_meals(conn=connect(db))[0]
        first = _shop("B_NJ_1", "鸭血粉丝汤", lat=32.03, lng=118.79)
        second = _shop("B_NJ_2", "鸭血粉丝汤", lat=32.04, lng=118.80)

        fix = mc.resolve_meal(slot, search=search_returning(first, second))

        assert fix.resolved is False
        assert "分不出是哪一家" in fix.reason

    def test_a_clear_winner_beats_the_plain_brand_name(self, db: Path) -> None:
        """分店名比品牌名更像——这时不叫并列，选分店。

        「南京大牌档（中山陵店）」与「南京大牌档」在高德上是两个不同的 POI，
        名字完整的那个才是行程里说的那一家。
        """
        _trip_with_meals(db, ("南京大牌档（中山陵店）", None, None))
        slot = mc.pending_meals(conn=connect(db))[0]
        branch = _shop("B_NJ_1", "南京大牌档(中山陵店)", lat=32.0545, lng=118.8541)
        brand = _shop("B_NJ_2", "南京大牌档", lat=32.04, lng=118.78)

        fix = mc.resolve_meal(slot, search=search_returning(brand, branch))

        assert fix.resolved is True
        assert fix.matched_name == "南京大牌档(中山陵店)"

    def test_a_non_food_candidate_is_refused(self, db: Path) -> None:
        """同名但不是饭馆——名字再像也不能拿它当坐标。

        这里刻意不复用 `classify_poi`：它说餐厅 IRRELEVANT 是对的
        （候选池里不该有饭馆），但把它的结论反过来用会把公交站也放进来。
        """
        _trip_with_meals(db, ("绿柳居", None, None))
        slot = mc.pending_meals(conn=connect(db))[0]
        station = CandidatePoi(
            poi_id="B_NJ_9",
            name="绿柳居",
            typecode="150200",
            type_name="交通设施服务;公交车站",
            adcode="320102",
            city_name="南京市",
            lng_gcj02=118.79,
            lat_gcj02=32.03,
        )

        fix = mc.resolve_meal(slot, search=search_returning(station))

        assert fix.resolved is False
        assert "都不是吃饭的地方" in fix.reason

    def test_a_dish_description_is_not_a_shop(self, db: Path) -> None:
        """一串菜名不是店名——**这是实测撞出来的那一个**。

        真实行程里有一项叫「临潼石榴汁与 biangbiang 面」（兵马俑门口吃的东西），
        它按包含关系命中了「biangbiang面」那家店，而**那家店在城北未央路，
        离兵马俑 35 公里**。名字分是够的（包含 → 0.90），所以只能靠
        「这不像店名」这一关挡。
        """
        _trip_with_meals(db, ("临潼石榴汁与 biangbiang 面", None, None))
        slot = mc.pending_meals(conn=connect(db))[0]

        fix = mc.resolve_meal(
            slot, search=search_returning(_shop("B_XA_1", "biangbiang面", lat=34.299, lng=108.944))
        )

        assert fix.resolved is False
        assert "不像店名" in fix.reason

    def test_a_weak_name_match_is_refused(self, db: Path) -> None:
        """差得太远的名字不硬套——那多半是另一家店，不是别名。"""
        _trip_with_meals(db, ("许记糕点", None, None))
        slot = mc.pending_meals(conn=connect(db))[0]

        fix = mc.resolve_meal(slot, search=search_returning(_shop(name="许记(许家巷店)")))

        assert fix.resolved is False
        assert "名字差得远" in fix.reason


class TestPlanFill:
    def test_does_not_sleep_before_the_first_search(self, db: Path) -> None:
        _trip_with_meals(db, ("南京大牌档", None, None), ("绿柳居", None, None))
        slept: list[float] = []

        mc.plan_fill(
            mc.pending_meals(conn=connect(db)),
            search=search_returning(_shop()),
            pause=0.4,
            sleep=slept.append,
        )

        assert slept == [0.4]  # 两次搜索之间只歇一次

    def test_splits_resolved_from_unresolved(self, db: Path) -> None:
        _trip_with_meals(db, ("南京大牌档（中山陵店）", None, None), ("鸭血粉丝汤", None, None))
        slots = mc.pending_meals(conn=connect(db))

        def search(keywords: str, city: str):
            table = _shop() if "大牌档" in keywords else None
            return search_returning(*([table] if table else []))(keywords, city)

        report = mc.plan_fill(slots, search=search, pause=0)

        assert [fix.slot.title for fix in report.resolved] == ["南京大牌档（中山陵店）"]
        assert [fix.slot.title for fix in report.unresolved] == ["鸭血粉丝汤"]


class TestApply:
    def test_writes_coordinates_and_address(self, db: Path) -> None:
        _trip_with_meals(db, ("南京大牌档（中山陵店）", None, None))
        slot = mc.pending_meals(conn=connect(db))[0]
        fix = mc.resolve_meal(slot, search=search_returning(_shop()))

        changed = mc.apply_fixes([fix], conn=connect(db))

        assert changed == [slot.item_id]
        row = connect(db).execute(
            "SELECT lat_gcj02, lng_gcj02, address, poi_id FROM day_item WHERE id = ?",
            (slot.item_id,),
        ).fetchone()
        assert (row["lat_gcj02"], row["lng_gcj02"]) == (32.0545, 118.8541)
        assert row["address"] == "中山门外石象路 7 号"
        assert row["poi_id"] is None, "餐厅不该被写进 poi 表（ADR-0002 说的是景点）"

    def test_does_not_overwrite_what_is_already_there(self, db: Path) -> None:
        """一次重跑不该把别人（或人工）补过的坐标冲掉。"""
        _trip_with_meals(db, ("南京大牌档（中山陵店）", None, None))
        slot = mc.pending_meals(conn=connect(db))[0]
        fix = mc.resolve_meal(slot, search=search_returning(_shop()))
        with transaction(db) as conn:
            conn.execute(
                "UPDATE day_item SET lat_gcj02 = 1.0, lng_gcj02 = 2.0 WHERE id = ?",
                (slot.item_id,),
            )

        changed = mc.apply_fixes([fix], conn=connect(db))

        assert changed == []
        row = connect(db).execute(
            "SELECT lat_gcj02 FROM day_item WHERE id = ?", (slot.item_id,)
        ).fetchone()
        assert row["lat_gcj02"] == 1.0

    def test_unresolved_fixes_write_nothing(self, db: Path) -> None:
        _trip_with_meals(db, ("鸭血粉丝汤", None, None))
        slot = mc.pending_meals(conn=connect(db))[0]
        fix = mc.resolve_meal(slot, search=search_returning())

        assert mc.apply_fixes([fix], conn=connect(db)) == []

    def test_is_idempotent(self, db: Path) -> None:
        """写完之后就不该再出现在待办里——这条路书的路段说明已经补上了。"""
        _trip_with_meals(db, ("南京大牌档（中山陵店）", None, None))
        slot = mc.pending_meals(conn=connect(db))[0]
        fix = mc.resolve_meal(slot, search=search_returning(_shop()))

        mc.apply_fixes([fix], conn=connect(db))

        assert mc.pending_meals(conn=connect(db)) == []
