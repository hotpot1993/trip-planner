"""复验扫描（设计 4.6 / Q49）。

这一层盯的是一种**不会报错的失败**：日期一天天过去，库里那行数据一个字都没变，
用户看到的仍然是「提前 7 天 20:00 放票」，而那条规则是三个月前核的。
没有任何异常、没有任何空值、没有任何 404——界面照常渲染，只是内容是旧的。

所以测试的重点不是「能不能筛出过期的」，而是几件更容易做错的事：

1. **到期当天算不算过期**——报告与写库必须是同一个答案（曾经是 `days_left < 0`
   与 `verify_due_at <= 今天` 两个判据，于是同一天里报告说「还有 0 天」、
   而 `--apply` 已经把它翻了）。
2. **周期只有一处定义**——本模块不持有 90/180 这些数字，只用别处算好的日期。
   一旦有人把周期抄进来，两处会在某次调整后分岔。
3. **标注而不隐藏**——状态翻成 `needs_reverify` 之后，行程里的结论必须照样
   拿得到。状态一变就消失，正是设计里明确禁止的那种做法。
"""

from __future__ import annotations

import itertools
from datetime import date, timedelta
from pathlib import Path

import pytest

from lushu.domain.booking import BookingRule, Channel, ChannelKind, RuleStatus
from lushu.domain.knowledge import ClaimEvidence, Facet, refresh_days_for
from lushu.services import booking_store as bs
from lushu.services import knowledge_store as ks
from lushu.services import trip_insights as ti
from lushu.services import verify as vy
from lushu.store import connect, initialize, transaction

TODAY = date(2026, 9, 13)
NOW = "2026-09-13T10:00:00"
POI_ID = "B000A8UIN8"
OTHER_POI = "B000A8UIN9"

# 素材 id 只是为了让结论有来源。用计数器而不是 hash(text)：
# hash 对字符串是逐进程随机的，同一份测试两次跑出来的库不该不一样。
_DOCUMENTS = itertools.count(1)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "verify.db"
    initialize(path)
    return path


def _poi(db: Path, poi_id: str = POI_ID, name: str = "故宫博物院") -> None:
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO city (adcode, name, updated_at) VALUES ('110100', '北京', ?)",
            (NOW,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO poi (amap_poi_id, name, city_adcode, adcode, typecode, "
            "lat_gcj02, lng_gcj02, fetched_at) VALUES (?, ?, '110100', '110101', "
            "'110201', 39.918, 116.397, ?)",
            (poi_id, name, NOW),
        )


def _rule(
    db: Path,
    *,
    reviewed_at: date | None,
    poi_id: str = POI_ID,
    status: RuleStatus = RuleStatus.REVIEWED,
    advance_days: int | None = 7,
    release_time: str | None = "20:00",
) -> None:
    rule = BookingRule(
        poi_id=poi_id,
        booking_required=True,
        status=status,
        advance_days=advance_days,
        release_time=release_time,
        channels=(Channel(name="官方公众号", kind=ChannelKind.OFFICIAL_ACCOUNT),),
        reviewed_at=reviewed_at,
    )
    with transaction(db) as conn:
        bs.write_rule(conn=conn, rule=rule, now=NOW)


def _claim(
    db: Path,
    *,
    verify_due_at: str | None,
    text: str = "周一闭馆，别白跑",
    subject_name: str = "故宫",
    poi_id: str | None = POI_ID,
    facet: str = "hours",
    status: str = "active",
) -> str:
    with transaction(db) as conn:
        document = f"src_{next(_DOCUMENTS)}"
        conn.execute(
            "INSERT OR REPLACE INTO source_document (id, site, body_text, body_sha256, "
            "imported_at, import_kind) VALUES (?, 'manual', '正文', ?, ?, 'paste')",
            (document, document, NOW),
        )
        claim_id = ks.save_claim(
            conn=conn,
            subject_type="poi",
            subject_name=subject_name,
            poi_id=poi_id,
            polarity="avoid",
            facet=facet,
            text=text,
            evidence=[ClaimEvidence(source_document_id=document, quote=text)],
            first_seen_at=NOW,
            verify_due_at=verify_due_at,
        )
        if status != "active":
            conn.execute("UPDATE claim SET status = ? WHERE id = ?", (status, claim_id))
    return claim_id


def _trip_with_item(db: Path, poi_id: str = POI_ID) -> str:
    with transaction(db) as conn:
        conn.execute(
            "INSERT INTO trip (id, name, start_date, created_at, updated_at) "
            "VALUES ('trip_1', '北京三日', '2026-10-01', ?, ?)",
            (NOW, NOW),
        )
        conn.execute(
            "INSERT INTO city_stay (id, trip_id, city_adcode, city_name, seq, stay_days) "
            "VALUES ('cs_1', 'trip_1', '110100', '北京', 0, 1)"
        )
        conn.execute(
            "INSERT INTO day (id, trip_id, city_stay_id, date, seq_in_stay) "
            "VALUES ('day_1', 'trip_1', 'cs_1', '2026-10-01', 0)"
        )
        conn.execute(
            "INSERT INTO day_item (id, day_id, seq, kind, poi_id, title, origin) "
            "VALUES ('di_1', 'day_1', 0, 'poi', ?, '故宫博物院', 'ai')",
            (poi_id,),
        )
    return "trip_1"


class TestDueRules:
    def test_expired_rule_is_listed(self, db: Path) -> None:
        _poi(db)
        _rule(db, reviewed_at=TODAY - timedelta(days=100))

        found = vy.due_rules(conn=connect(db), today=TODAY)

        assert [item.label for item in found] == ["故宫博物院"]
        assert found[0].kind == "rule"
        assert found[0].days_left == -10
        assert found[0].overdue is True
        assert found[0].description == "已过期 10 天"
        assert found[0].due_at == TODAY - timedelta(days=10)

    def test_freshly_reviewed_rule_is_not_listed(self, db: Path) -> None:
        """今天复核的，90 天后才到期，离得远，不该出现在「该看一眼」里。"""
        _poi(db)
        _rule(db, reviewed_at=TODAY)

        assert vy.due_rules(conn=connect(db), today=TODAY) == []

    def test_draft_rule_is_never_listed(self, db: Path) -> None:
        """草案不对用户可见，没有人依赖它，扫描不该为它报警。"""
        _poi(db)
        _rule(db, reviewed_at=None, status=RuleStatus.DRAFT)

        assert vy.due_rules(conn=connect(db), today=TODAY) == []

    def test_draft_with_a_due_date_is_still_skipped(self, db: Path) -> None:
        """即使某行草案数据里留着到期日（正常流程不会这样），也不扫它。

        这条钉的是「按 status 挡一道」这件事本身：只靠 `verify_due_at is None`
        来挡，等于把「不对用户可见」的规则混进用户的待办里。
        """
        _poi(db)
        _rule(db, reviewed_at=TODAY - timedelta(days=200), status=RuleStatus.DRAFT)

        assert vy.due_rules(conn=connect(db), today=TODAY) == []

    def test_the_boundary_is_soon_days(self, db: Path) -> None:
        """`SOON_DAYS` 是闭区间：正好第 14 天要看，第 15 天还不用。"""
        _poi(db)
        _poi(db, OTHER_POI, "中国国家博物馆")
        _rule(db, reviewed_at=TODAY - timedelta(days=90 - vy.SOON_DAYS))
        _rule(
            db,
            poi_id=OTHER_POI,
            reviewed_at=TODAY - timedelta(days=90 - vy.SOON_DAYS - 1),
        )

        found = vy.due_rules(conn=connect(db), today=TODAY)

        assert [item.label for item in found] == ["故宫博物院"]
        assert found[0].days_left == vy.SOON_DAYS

    def test_due_today_counts_as_overdue(self, db: Path) -> None:
        """到期当天就是到期，不是「还有 0 天」。"""
        _poi(db)
        _rule(db, reviewed_at=TODAY - timedelta(days=90))

        item = vy.due_rules(conn=connect(db), today=TODAY)[0]

        assert item.days_left == 0
        assert item.overdue is True
        assert item.description == "今天到期"

    def test_unknown_release_window_is_said_not_zeroed(self, db: Path) -> None:
        """算不出放票日不是 0 天。

        17 个必须预约的知名景点里 12 个从不公布放票天数（见
        `BookingRule.has_release_window`），把它印成「提前 0 天」是在编。
        """
        _poi(db)
        _rule(db, reviewed_at=TODAY - timedelta(days=90), advance_days=None, release_time=None)

        item = vy.due_rules(conn=connect(db), today=TODAY)[0]

        assert item.detail == "放票口径未公布"

    def test_release_time_is_shown_alongside(self, db: Path) -> None:
        _poi(db)
        _rule(db, reviewed_at=TODAY - timedelta(days=90))

        assert vy.due_rules(conn=connect(db), today=TODAY)[0].detail == "提前 7 天 20:00"


class TestDueClaims:
    def test_expired_claim_is_listed(self, db: Path) -> None:
        _poi(db)
        _claim(db, verify_due_at=(TODAY - timedelta(days=3)).isoformat())

        found, timeless = vy.due_claims(conn=connect(db), today=TODAY)

        assert len(found) == 1
        assert found[0].kind == "claim"
        assert found[0].label == "故宫"
        assert found[0].detail == "周一闭馆，别白跑"
        assert found[0].days_left == -3
        assert timeless == 0

    def test_timeless_claims_are_counted_not_listed(self, db: Path) -> None:
        """拍照机位不设期限（Q49）。报个数，「没扫到」与「不设期限」才分得开。"""
        _poi(db)
        _claim(db, verify_due_at=None, text="想拍没人的太和殿要开门就冲", facet="photo")

        found, timeless = vy.due_claims(conn=connect(db), today=TODAY)

        assert found == []
        assert timeless == 1

    def test_retired_claims_are_out_of_scope_entirely(self, db: Path) -> None:
        """明确废弃的结论不算「该重核了」，也不该混进不设期限的计数里。"""
        _poi(db)
        _claim(db, verify_due_at=(TODAY - timedelta(days=3)).isoformat(), status="retired")
        _claim(db, verify_due_at=None, status="retired", text="另一条废弃的", facet="photo")

        found, timeless = vy.due_claims(conn=connect(db), today=TODAY)

        assert found == []
        assert timeless == 0

    def test_already_marked_claim_stays_in_the_list(self, db: Path) -> None:
        """标成待复验的结论照样要出现在扫描里。

        否则「已经提醒过一次」就变成「以后不再提醒」——而过期不会因为
        被提醒过一次就变新鲜。
        """
        _poi(db)
        _claim(db, verify_due_at=(TODAY - timedelta(days=3)).isoformat(), status="needs_reverify")

        found, _ = vy.due_claims(conn=connect(db), today=TODAY)

        assert len(found) == 1

    def test_far_future_claim_is_not_listed(self, db: Path) -> None:
        _poi(db)
        _claim(db, verify_due_at=(TODAY + timedelta(days=100)).isoformat())

        assert vy.due_claims(conn=connect(db), today=TODAY)[0] == []

    def test_subject_name_beats_the_entity_name(self, db: Path) -> None:
        """原文里的叫法更接近用户的说法（「湖南省博」vs「湖南省博物馆」）。"""
        _poi(db)
        _claim(db, verify_due_at=TODAY.isoformat(), subject_name="故宫")

        assert vy.due_claims(conn=connect(db), today=TODAY)[0][0].label == "故宫"

    def test_label_falls_back_to_the_entity_name(self, db: Path) -> None:
        """`subject_name` 为空时退回实体名——**按 poi_id 查，不是按结论 id**。

        这两张表的主键长得一模一样（都是 `B...` 形状的串），拿结论 id 去查
        POI 名字不会报错，只会查不到，然后所有标签一起变成兜底文案。
        """
        _poi(db)
        _claim(db, verify_due_at=TODAY.isoformat(), subject_name="")

        assert vy.due_claims(conn=connect(db), today=TODAY)[0][0].label == "故宫博物院"

    def test_label_falls_back_to_placeholder(self, db: Path) -> None:
        """既没写主体、也对不上实体时给一句人话，不给空串。"""
        _poi(db)
        _claim(db, verify_due_at=TODAY.isoformat(), subject_name="", poi_id=None)

        assert vy.due_claims(conn=connect(db), today=TODAY)[0][0].label == "（未写主体）"


class TestScan:
    def test_merges_both_kinds_oldest_first(self, db: Path) -> None:
        _poi(db)
        _claim(db, verify_due_at=(TODAY - timedelta(days=1)).isoformat())
        _rule(db, reviewed_at=TODAY - timedelta(days=100))

        report = vy.scan(conn=connect(db), today=TODAY)

        assert [item.kind for item in report.items] == ["rule", "claim"]
        assert report.today == TODAY
        assert len(report.overdue) == 2
        assert report.soon == []

    def test_splits_overdue_from_soon(self, db: Path) -> None:
        _poi(db)
        _claim(db, verify_due_at=(TODAY - timedelta(days=1)).isoformat())
        _rule(db, reviewed_at=TODAY - timedelta(days=85))

        report = vy.scan(conn=connect(db), today=TODAY)

        assert [item.kind for item in report.overdue] == ["claim"]
        assert [item.kind for item in report.soon] == ["rule"]
        assert len(report.rules) == 1
        assert len(report.claims) == 1

    def test_empty_database_scans_clean(self, db: Path) -> None:
        """空库不是错误，扫描要说「没有」而不是崩。"""
        report = vy.scan(conn=connect(db), today=TODAY)

        assert report.items == []
        assert report.timeless == 0

    def test_stats_counts_the_five_numbers(self, db: Path) -> None:
        _poi(db)
        _claim(db, verify_due_at=(TODAY - timedelta(days=1)).isoformat())
        _claim(db, verify_due_at=None, text="机位", facet="photo")
        _rule(db, reviewed_at=TODAY - timedelta(days=85))

        assert vy.stats(conn=connect(db), today=TODAY) == {
            "overdue": 1,
            "soon": 1,
            "rules": 1,
            "claims": 1,
            "timeless": 1,
        }

    def test_a_wider_window_answers_a_different_question(self, db: Path) -> None:
        """默认两周是「现在该动手了吗」，调大是「出发前哪些会到期」。"""
        _poi(db)
        _claim(db, verify_due_at=(TODAY + timedelta(days=60)).isoformat())

        assert vy.scan(conn=connect(db), today=TODAY).items == []
        wide = vy.scan(conn=connect(db), today=TODAY, soon_days=90)
        assert [item.kind for item in wide.items] == ["claim"]
        assert wide.items[0].days_left == 60
        assert wide.items[0].description == "还有 60 天"


class TestNextDue:
    """「今天没事」与「以后也没事」是两件事。

    22 条规则是同一天复核的，于是它们会在同一天一起到期。用户需要知道
    下一次该在什么时候回来，否则扫描就成了一次性的报告。
    """

    def test_returns_the_earliest_upcoming_date(self, db: Path) -> None:
        _poi(db)
        _claim(db, verify_due_at=(TODAY + timedelta(days=30)).isoformat())
        _rule(db, reviewed_at=TODAY)

        assert vy.next_due(conn=connect(db), today=TODAY) == TODAY + timedelta(days=30)

    def test_ignores_what_has_already_expired(self, db: Path) -> None:
        """已经过期的不算「下一次」——那要现在处理，而且 scan 已经单独报出来了。"""
        _poi(db)
        _claim(db, verify_due_at=(TODAY - timedelta(days=5)).isoformat())

        assert vy.next_due(conn=connect(db), today=TODAY) is None

    def test_empty_database_has_no_next_date(self, db: Path) -> None:
        assert vy.next_due(conn=connect(db), today=TODAY) is None

    def test_counts_the_due_date_itself(self, db: Path) -> None:
        """到期当天仍然是「下一次」——那天要核，不是已经过去的事。"""
        _poi(db)
        _claim(db, verify_due_at=TODAY.isoformat())

        assert vy.next_due(conn=connect(db), today=TODAY) == TODAY

    def test_draft_rules_do_not_set_the_schedule(self, db: Path) -> None:
        """草案不对用户可见，它的到期日不该成为用户的下一个待办。"""
        _poi(db)
        _rule(db, reviewed_at=None, status=RuleStatus.DRAFT)

        assert vy.next_due(conn=connect(db), today=TODAY) is None


class TestMarkDueClaims:
    def test_flips_only_the_overdue_ones(self, db: Path) -> None:
        _poi(db)
        overdue = _claim(db, verify_due_at=(TODAY - timedelta(days=1)).isoformat())
        _claim(db, verify_due_at=(TODAY + timedelta(days=3)).isoformat(), text="快到期但还没到")

        changed = vy.mark_due_claims(conn=connect(db), today=TODAY)

        assert changed == [overdue]
        assert _status(db, overdue) == "needs_reverify"

    def test_returns_ids_so_the_change_can_be_checked(self, db: Path) -> None:
        """这条命令的副作用在界面上看不出来，只报个数没人能核对它动了哪几条。"""
        _poi(db)
        first = _claim(db, verify_due_at=(TODAY - timedelta(days=9)).isoformat(), text="先到期的")
        second = _claim(db, verify_due_at=(TODAY - timedelta(days=2)).isoformat(), text="后到期的")

        assert vy.mark_due_claims(conn=connect(db), today=TODAY) == [first, second]

    def test_is_idempotent(self, db: Path) -> None:
        _poi(db)
        _claim(db, verify_due_at=(TODAY - timedelta(days=1)).isoformat())

        vy.mark_due_claims(conn=connect(db), today=TODAY)
        assert vy.mark_due_claims(conn=connect(db), today=TODAY) == []

    def test_leaves_retired_claims_alone(self, db: Path) -> None:
        """废弃的结论不该被「升格」成待复验——那会把它重新拉回复验队列。"""
        _poi(db)
        claim_id = _claim(
            db,
            verify_due_at=(TODAY - timedelta(days=1)).isoformat(),
            text="已经废弃的说法",
            status="retired",
        )

        assert vy.mark_due_claims(conn=connect(db), today=TODAY) == []
        assert _status(db, claim_id) == "retired"

    def test_the_timeless_ones_are_untouched(self, db: Path) -> None:
        _poi(db)
        claim_id = _claim(db, verify_due_at=None, text="机位", facet="photo")

        assert vy.mark_due_claims(conn=connect(db), today=TODAY) == []
        assert _status(db, claim_id) == "active"

    def test_marked_claims_are_still_shown_in_the_trip(self, db: Path) -> None:
        """**标注而不隐藏**（设计 4.6）。

        这条是这一层最容易做错的地方：把状态翻掉之后，若某个读路径按
        `status = 'active'` 过滤，用户那边这些经验就凭空消失了——他会以为
        这个景点没有任何提醒。过期不等于无效，只等于「你得知道这是旧的」。
        """
        _poi(db)
        _trip_with_item(db)
        _claim(db, verify_due_at=(TODAY - timedelta(days=1)).isoformat())

        before = ti.insights_for_trip("trip_1", conn=connect(db)).total_claims
        vy.mark_due_claims(conn=connect(db), today=TODAY)
        after = ti.insights_for_trip("trip_1", conn=connect(db)).total_claims

        assert before == 1
        assert after == 1, "结论被标成待复验之后就查不到了——这是隐藏，不是标注"


class TestOnePeriodOnePlace:
    """周期只在 `domain/knowledge.py` 与 `domain/booking.py` 各定义一次。

    本模块只用别处算好的日期。有人把 90/180 抄进 `verify.py` 的那天，
    两处会在下一次调整后分岔，而分岔的表现是「界面上还新鲜、扫描说已过期」。
    """

    def test_module_holds_no_period_constants(self) -> None:
        source = Path(vy.__file__).read_text(encoding="utf-8")
        for number in refresh_days_for.__globals__["_REFRESH_DAYS"].values():
            if number is not None:
                assert f"= {number}" not in source, f"verify.py 里抄了一份 {number} 天的周期"

    def test_stored_due_date_matches_the_domain_rule(self, db: Path) -> None:
        """入库时写下的到期日，必须等于按 `refresh_days_for` 算出来的那一天。

        扫描读的是那一列（与 `trip_insights`、工作台保持一致），所以列一旦
        与域里的周期脱节，扫描报的就是个假日期。
        """
        _poi(db)
        _claim(db, verify_due_at=(TODAY + timedelta(days=90)).isoformat(), facet="hours")
        _claim(
            db,
            verify_due_at=None,
            text="机位",
            facet="photo",
        )

        rows = {
            row["facet"]: row["verify_due_at"]
            for row in connect(db).execute("SELECT facet, verify_due_at FROM claim")
        }

        assert rows["hours"] == (TODAY + timedelta(days=refresh_days_for(Facet.HOURS))).isoformat()
        assert rows["photo"] is None
        assert refresh_days_for(Facet.PHOTO) is None


def _status(db: Path, claim_id: str) -> str:
    conn = connect(db)
    try:
        return conn.execute("SELECT status FROM claim WHERE id = ?", (claim_id,)).fetchone()[0]
    finally:
        conn.close()
