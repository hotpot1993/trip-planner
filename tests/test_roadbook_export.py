"""路书的装配与渲染。

契约层（`tests/test_domain_roadbook.py`）管「一份合格的路书必须有什么」；
这一层管「从库里装出来」与「渲染成能离线打开的 HTML」。

测试盯三件事：

1. **装配不编内容**——算不出来的东西留空，不填一个看起来合理的值
2. **渲染零外部加载**——这是离线可读的执行点
3. **导航深链保留**——地图被砍掉之后留下的就是这部分（ADR-0006）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lushu.domain.knowledge import ClaimEvidence
from lushu.services import knowledge_store as ks
from lushu.services import roadbook_render as render
from lushu.services import roadbook_service as service
from lushu.store import connect, initialize, transaction

NOW = "2026-09-13T10:00:00"
POI_A = "B_GUGONG"
POI_B = "B_JINGSHAN"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "roadbook.db"
    initialize(path)
    return path


def _seed(db: Path, *, with_claims: bool = False, with_transfer: bool = False) -> None:
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO city (adcode, name, updated_at) VALUES ('110100', '北京', ?)",
            (NOW,),
        )
        for poi_id, name, lat, lng, address in (
            (POI_A, "故宫博物院", 39.918, 116.397, "景山前街 4 号"),
            (POI_B, "景山公园", 39.928, 116.390, "景山西街 44 号"),
        ):
            conn.execute(
                "INSERT INTO poi (amap_poi_id, name, city_adcode, adcode, typecode, address, "
                "lat_gcj02, lng_gcj02, fetched_at) VALUES (?, ?, '110100', '110101', "
                "'110201', ?, ?, ?, ?)",
                (poi_id, name, address, lat, lng, NOW),
            )
        conn.execute(
            "INSERT INTO trip (id, name, start_date, created_at, updated_at) "
            "VALUES ('trip_1', '北京一日', '2026-10-01', ?, ?)",
            (NOW, NOW),
        )
        conn.execute(
            "INSERT INTO city_stay (id, trip_id, city_adcode, city_name, seq, stay_days) "
            "VALUES ('cs_1', 'trip_1', '110100', '北京', 0, 1)"
        )
        conn.execute(
            "INSERT INTO day (id, trip_id, city_stay_id, date, seq_in_stay, theme) "
            "VALUES ('day_1', 'trip_1', 'cs_1', '2026-10-01', 0, '中轴线')"
        )
        conn.execute(
            "INSERT INTO day_item (id, day_id, seq, kind, poi_id, title, start_time, origin) "
            "VALUES ('di_1', 'day_1', 0, 'poi', ?, '故宫博物院', '09:00', 'ai')",
            (POI_A,),
        )
        conn.execute(
            "INSERT INTO day_item (id, day_id, seq, kind, poi_id, title, start_time, origin) "
            "VALUES ('di_2', 'day_1', 1, 'poi', ?, '景山公园', '15:00', 'ai')",
            (POI_B,),
        )
        # 一个没有 poi_id 的餐饮项：算不出坐标的那种
        conn.execute(
            "INSERT INTO day_item (id, day_id, seq, kind, title, start_time, origin) "
            "VALUES ('di_3', 'day_1', 2, 'meal', '四季民福', '18:00', 'ai')"
        )
        if with_transfer:
            conn.execute(
                "INSERT INTO intercity_transfer (id, trip_id, from_city_stay_id, "
                "to_city_stay_id, day_id, mode, note) VALUES "
                "('tf_1', 'trip_1', 'cs_1', 'cs_1', 'day_1', 'rail', '还没到放票期')"
            )
        conn.execute(
            "INSERT INTO budget_item (id, trip_id, category, label, amount, currency) "
            "VALUES ('bud_1', 'trip_1', 'ticket', '门票', 60, 'CNY')"
        )
        if with_claims:
            conn.execute(
                "INSERT INTO source_document (id, site, body_text, body_sha256, imported_at, "
                "import_kind) VALUES ('src_1', 'manual', '正文', 'sha_1', ?, 'paste')",
                (NOW,),
            )
            ks.save_claim(
                conn=conn,
                subject_type="poi",
                subject_name="故宫",
                poi_id=POI_A,
                polarity="avoid",
                facet="entrance",
                text="只有午门能进，北门只出不进",
                evidence=[
                    ClaimEvidence(source_document_id="src_1", quote="只有午门能进")
                ],
                first_seen_at=NOW,
                verify_due_at=None,
            )
            # 再挂一条**三源**的打卡：路书要能同时说清「一个人在说」与
            # 「三个人都在说」，只造一条的话两种措辞测不全
            for extra in ("src_2", "src_3"):
                conn.execute(
                    "INSERT INTO source_document (id, site, body_text, body_sha256, "
                    "imported_at, import_kind) VALUES (?, 'manual', '正文', ?, ?, 'paste')",
                    (extra, f"sha_{extra}", NOW),
                )
            ks.save_claim(
                conn=conn,
                subject_type="poi",
                subject_name="故宫",
                poi_id=POI_A,
                polarity="highlight",
                facet="queue",
                text="开门就冲，八点半前进太和殿基本没人",
                evidence=[
                    ClaimEvidence(source_document_id=doc, quote="开门就冲")
                    for doc in ("src_1", "src_2", "src_3")
                ],
                first_seen_at=NOW,
                verify_due_at=None,
            )


class TestHaversine:
    def test_zero_for_the_same_point(self) -> None:
        point = service._Point("甲", 39.9, 116.4)
        assert service.haversine_m(point, point) == pytest.approx(0, abs=1)

    def test_known_distance(self) -> None:
        """故宫到景山约一公里出头。"""
        left = service._Point("故宫", 39.918, 116.397)
        right = service._Point("景山", 39.928, 116.390)
        distance = service.haversine_m(left, right)
        assert distance is not None
        assert 1000 < distance < 1400

    def test_missing_coordinates_give_none(self) -> None:
        left = service._Point("甲", None, None)
        right = service._Point("乙", 39.9, 116.4)
        assert service.haversine_m(left, right) is None


class TestEstimateLeg:
    def test_short_hop_is_a_walk(self) -> None:
        leg = service.estimate_leg(
            service._Point("故宫", 39.918, 116.397), service._Point("景山", 39.928, 116.390)
        )
        assert leg is not None
        assert leg.mode == "walk"
        assert leg.distance_m is not None and leg.distance_m > 0
        assert leg.duration_min is not None

    def test_far_hop_is_not_a_walk(self) -> None:
        leg = service.estimate_leg(
            service._Point("故宫", 39.918, 116.397), service._Point("颐和园", 39.999, 116.275)
        )
        assert leg is not None
        assert leg.mode in ("metro", "taxi")

    def test_estimate_says_it_is_an_estimate(self) -> None:
        """没有地图的页面上写「步行 20 分钟」，用户会当真。

        必须说清这是直线估算，实际更长——否则他会按这个时间安排行程。
        """
        leg = service.estimate_leg(
            service._Point("甲", 39.918, 116.397), service._Point("乙", 39.928, 116.390)
        )
        assert leg is not None
        assert leg.note and "估算" in leg.note

    def test_navigation_link_is_produced(self) -> None:
        """地图被砍掉了，导航深链是留下的那部分（ADR-0006）。"""
        leg = service.estimate_leg(
            service._Point("甲", 39.918, 116.397), service._Point("乙", 39.928, 116.390)
        )
        assert leg is not None
        assert leg.nav_url and leg.nav_url.startswith("https://uri.amap.com/")

    def test_no_coordinates_gives_no_leg(self) -> None:
        """算不出来就留空，不编一段看起来合理的文字。"""
        leg = service.estimate_leg(
            service._Point("甲", None, None), service._Point("乙", 39.9, 116.4)
        )
        assert leg is None


class TestAssemble:
    def test_builds_days_and_items(self, db: Path) -> None:
        _seed(db)
        book, warnings = service.assemble("trip_1", conn=connect(db))

        assert book.name == "北京一日"
        assert book.total_days == 1
        assert book.item_count == 3
        assert book.days[0].city_name == "北京"
        assert book.days[0].theme == "中轴线"
        assert warnings == []

    def test_items_are_sorted_by_time(self, db: Path) -> None:
        _seed(db)
        book, _ = service.assemble("trip_1", conn=connect(db))
        times = [item.start_time for item in book.days[0].items]
        assert times == ["09:00", "15:00", "18:00"]

    def test_legs_are_between_items(self, db: Path) -> None:
        _seed(db)
        book, _ = service.assemble("trip_1", conn=connect(db))
        day = book.days[0]
        # 3 项 → 2 段
        assert len(day.legs) == 2
        # 第一段有坐标，第二段（到没有 poi_id 的餐厅）没有
        assert day.legs[0] is not None
        assert day.legs[1] is None

    def test_missing_coordinates_warn_rather_than_guess(self, db: Path) -> None:
        _seed(db)
        book, _ = service.assemble("trip_1", conn=connect(db))
        assert book.days[0].legs[1] is None

    def test_attaches_soft_experience(self, db: Path) -> None:
        """设计 5.4：软经验生成后挂载，不注入模型。"""
        _seed(db, with_claims=True)
        book, _ = service.assemble("trip_1", conn=connect(db))

        first = book.days[0].items[0]
        assert [claim.text for claim in first.avoids] == ["只有午门能进，北门只出不进"]
        # 来源数必须跟着走到路书上：这一页是带上路的那一份，分不出
        # 「三个人都说」与「一个人说」的话，信任模型在最需要它的地方缺席
        assert first.avoids[0].independent_source_count == 1

    def test_booking_rule_reaches_the_item(self, db: Path) -> None:
        _seed(db)
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO booking_rule (poi_id, booking_required, advance_days, "
                "release_time, status, evidence_url, reviewed_at, updated_at) "
                "VALUES (?, 1, 7, '20:00', 'reviewed', 'https://example.cn/', "
                "'2026-01-01', ?)",
                (POI_A, NOW),
            )
        book, _ = service.assemble("trip_1", conn=connect(db))
        assert book.days[0].items[0].booking == "需预约，提前 7 天 20:00放票"

    def test_draft_booking_rule_does_not_reach_the_item(self, db: Path) -> None:
        """未经复核的规则不得展示（Q10）——路书是给别人用的东西。"""
        _seed(db)
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO booking_rule (poi_id, booking_required, advance_days, "
                "status, updated_at) VALUES (?, 1, 7, 'draft', ?)",
                (POI_A, NOW),
            )
        book, _ = service.assemble("trip_1", conn=connect(db))
        assert book.days[0].items[0].booking is None

    def test_transfer_without_stations_still_reads(self, db: Path) -> None:
        """预售期外查不到车次，起终站是空的——不能写成「高铁：高铁」。"""
        _seed(db, with_transfer=True)
        book, _ = service.assemble("trip_1", conn=connect(db))
        text = book.days[0].transfer
        assert text is not None
        assert text != "高铁：高铁"
        assert "还没到放票期" in text

    def test_budget_is_carried(self, db: Path) -> None:
        _seed(db)
        book, _ = service.assemble("trip_1", conn=connect(db))
        assert ("门票", "¥60") in book.budget

    def test_unknown_trip_raises(self, db: Path) -> None:
        with pytest.raises(ValueError, match="没有这份行程"):
            service.assemble("trip_none", conn=connect(db))


class TestRender:
    def _html(self, db: Path, **kwargs: object) -> str:
        _seed(db, **kwargs)  # type: ignore[arg-type]
        book, _ = service.assemble("trip_1", conn=connect(db))
        return render.render(book)

    def test_no_external_resources(self, db: Path) -> None:
        """离线可读的执行点：页面不能自己去加载任何东西。"""
        assert render.external_resources(self._html(db)) == []

    def test_navigation_links_are_kept(self, db: Path) -> None:
        """导航深链是用户主动点的，不算「外部加载」——一开始我把两者混了。"""
        html = self._html(db)
        assert "uri.amap.com" in html
        # 但它仍然不算要加载的资源
        assert render.external_resources(html) == []

    def test_is_a_complete_document(self, db: Path) -> None:
        html = self._html(db)
        assert html.startswith("<!doctype html>")
        assert html.rstrip().endswith("</html>")
        assert 'lang="zh-CN"' in html
        assert "viewport" in html  # 手机优先

    def test_escapes_user_content(self, db: Path) -> None:
        """素材是网上的东西，标题里可能有尖括号——不转义就是一个注入点。"""
        _seed(db)
        with transaction(db) as conn:
            conn.execute("UPDATE trip SET name = '<script>alert(1)</script>' WHERE id = 'trip_1'")
        book, _ = service.assemble("trip_1", conn=connect(db))
        html = render.render(book)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_soft_experience_appears(self, db: Path) -> None:
        html = self._html(db, with_claims=True)
        assert "只有午门能进" in html

    def test_every_claim_says_how_many_sources_it_has(self, db: Path) -> None:
        """每条结论都要说清它有几个独立来源。

        这一页是带上路的那一份：断网、在路边低头看，没法回主项目查「这条是
        谁说的」。原先它只有一个字符串，于是「三个人都这么说」与「一个人这么
        说」在手机上长得一模一样——而区分这两件事正是整个信任模型存在的理由
        （Q34、ADR-0008），最需要它的地方反而缺席。
        """
        html = self._html(db, with_claims=True)

        assert "待验证的个例（只有 1 个来源）" in html
        assert "3 个独立来源" in html

    def test_missing_leg_says_so(self, db: Path) -> None:
        """没有坐标的那段路如实说，不编一段看起来合理的文字。

        但要**说给读这一页的人听**：旅行途中在手机上翻的人，既不知道说的是哪儿，
        也不知道该干什么。所以句中要点名目的地。
        """
        html = self._html(db)

        assert "没有坐标" in html
        assert "按名字问路" in html
        assert "这段路没有坐标，到当地问一下" not in html, "那句话是给作者看的，不该发到路上"

    def test_missing_leg_names_its_destination(self, db: Path) -> None:
        """一句「没有坐标」重复六遍等于没说——每条要说清是去哪儿。"""
        html = self._html(db)
        between = html.split("<div class='leg'>")

        assert any(segment.startswith("到「") for segment in between[1:])

    def test_days_are_labelled(self, db: Path) -> None:
        html = self._html(db)
        assert "2026-10-01" in html
        assert "中轴线" in html

    def test_footer_states_the_limits(self, db: Path) -> None:
        """这份文件会脱离项目独立存在，它的局限必须写在它自己身上。"""
        html = self._html(db)
        assert "离线生成" in html
        assert "以官方渠道为准" in html
        assert "估算" in html

    def test_is_small_enough_for_a_phone(self, db: Path) -> None:
        assert len(self._html(db).encode("utf-8")) < 60_000
