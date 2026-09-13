"""预约规则的种子库、复核门禁与体检。

这一层守的是一条设计红线（Q10）：**未经复核的规则不得展示**。
预约规则错一个字段，用户就会白跑一趟，这是本项目唯一会直接伤害用户的
失败模式，所以「入库」与「可用」必须是两件事，而且中间那道门禁要能被测出来
是会拦人的。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from lushu.domain.booking import RuleStatus, Severity
from lushu.services import booking_store as bs
from lushu.store import connect, initialize, transaction
from tests.test_pipeline import gugong, search_returning

TODAY = date(2026, 9, 12)

CLEAN = {
    "name": "故宫博物院",
    "city": "北京",
    "booking_required": True,
    "advance_days": 7,
    "release_time": "20:00",
    "channels": [
        {"name": "故宫博物院观众服务", "kind": "official_account", "url": "https://gugong.ktmtech.cn/"}
    ],
    "requires_real_name": True,
    "id_required_note": "每个账号最多添加 5 名常用观众",
    "closed_days_weekdays": [0],
    "evidence_url": "https://www.dpm.org.cn/visit.html",
    "source_kind": "official",
    "confidence": "high",
    "releases_at_note": "参观前 7 日的 20:00 放票",
    "note": "周一闭馆；提前把同行人身份证填好",
}


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "booking.db"
    initialize(path)
    return path


def _seed_file(tmp_path: Path, *entries: dict) -> Path:
    path = tmp_path / "seed.json"
    path.write_text(json.dumps({"rules": list(entries)}, ensure_ascii=False), encoding="utf-8")
    return path


def _entry(**overrides: object) -> dict:
    payload = dict(CLEAN)
    payload.update(overrides)
    return payload


def _city(db: Path, adcode: str = "110100", name: str = "北京") -> None:
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO city (adcode, name, updated_at) VALUES (?, ?, ?)",
            (adcode, name, TODAY.isoformat()),
        )


def _fetch(*pois):
    table = {poi.poi_id: poi for poi in pois}

    def fetch(poi_id: str):
        return table.get(poi_id)

    return fetch


class TestLoadSeed:
    def test_reads_a_valid_file(self, tmp_path: Path) -> None:
        path = _seed_file(tmp_path, _entry())
        entries = bs.load_seed(path)
        assert len(entries) == 1
        assert entries[0].name == "故宫博物院"
        assert entries[0].advance_days == 7
        assert entries[0].channels[0].kind.value == "official_account"

    def test_missing_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(bs.SeedError, match="找不到种子文件"):
            bs.load_seed(tmp_path / "nope.json")

    def test_broken_json_is_an_error_not_a_skip(self, tmp_path: Path) -> None:
        """种子是人工维护的短名单，读不出来意味着有人改坏了它。"""
        path = tmp_path / "seed.json"
        path.write_text("{不是 JSON", encoding="utf-8")
        with pytest.raises(bs.SeedError, match="不是合法 JSON"):
            bs.load_seed(path)

    def test_entry_without_city_is_rejected(self, tmp_path: Path) -> None:
        path = _seed_file(tmp_path, _entry(city=None))
        with pytest.raises(bs.SeedError, match="缺 name 或 city"):
            bs.load_seed(path)

    def test_unknown_channel_kind_is_rejected(self, tmp_path: Path) -> None:
        path = _seed_file(
            tmp_path, _entry(channels=[{"name": "某渠道", "kind": "carrier_pigeon"}])
        )
        with pytest.raises(bs.SeedError, match="渠道类型"):
            bs.load_seed(path)

    def test_bad_weekday_is_rejected(self, tmp_path: Path) -> None:
        path = _seed_file(tmp_path, _entry(closed_days_weekdays=[7]))
        with pytest.raises(bs.SeedError, match="0–6"):
            bs.load_seed(path)

    def test_booking_required_must_be_boolean(self, tmp_path: Path) -> None:
        path = _seed_file(tmp_path, _entry(booking_required="yes"))
        with pytest.raises(bs.SeedError, match="布尔值"):
            bs.load_seed(path)


class TestLintSeed:
    def test_clean_entry_has_no_errors(self, tmp_path: Path) -> None:
        findings = bs.lint_seed(bs.load_seed(_seed_file(tmp_path, _entry())), today=TODAY)
        assert [item for item in findings if item.severity is Severity.ERROR] == []

    def test_missing_channel_is_an_error(self, tmp_path: Path) -> None:
        """知道要预约却无处可去，对用户等于没提醒。"""
        findings = bs.lint_seed(
            bs.load_seed(_seed_file(tmp_path, _entry(channels=[]))), today=TODAY
        )
        assert any(item.code == "no_channel" for item in findings)

    def test_bad_release_time_is_an_error(self, tmp_path: Path) -> None:
        """放票时刻错了，用户会按错的时刻去等——这是最直接的白跑。"""
        findings = bs.lint_seed(
            bs.load_seed(_seed_file(tmp_path, _entry(release_time="晚上8点"))), today=TODAY
        )
        assert any(item.code == "bad_release_time" for item in findings)

    def test_implausible_advance_days_is_an_error(self, tmp_path: Path) -> None:
        findings = bs.lint_seed(
            bs.load_seed(_seed_file(tmp_path, _entry(advance_days=365))), today=TODAY
        )
        assert any(item.code == "implausible_advance_days" for item in findings)

    def test_missing_evidence_is_not_an_error_before_review(self, tmp_path: Path) -> None:
        """种子还没复核，没有来源链接只是「材料不齐」，不是「会误导用户」。"""
        findings = bs.lint_seed(
            bs.load_seed(_seed_file(tmp_path, _entry(evidence_url=None))), today=TODAY
        )
        assert not any(item.code == "reviewed_without_evidence" for item in findings)

    def test_advance_days_without_requirement_is_a_warning(self, tmp_path: Path) -> None:
        findings = bs.lint_seed(
            bs.load_seed(
                _seed_file(tmp_path, _entry(booking_required=False, advance_days=3))
            ),
            today=TODAY,
        )
        assert any(item.code == "advance_days_without_required" for item in findings)


class TestSeedRules:
    def test_writes_draft_rules_and_pois(self, db: Path, tmp_path: Path) -> None:
        _city(db)
        path = _seed_file(tmp_path, _entry())

        with connect(db) as conn:
            report = bs.seed_rules(
                path=path, conn=conn, search=search_returning(gugong()), fetch=_fetch()
            )

        assert report.written == 1
        assert report.failed == []

        rule = bs.get_rule(gugong().poi_id, conn=connect(db))
        assert rule is not None
        # 入库不等于可用：这一步写进去的一律是草案
        assert rule.status is RuleStatus.DRAFT
        assert rule.advance_days == 7
        assert rule.release_time == "20:00"
        assert rule.closed_days.weekdays == (0,)
        assert rule.channels[0].name == "故宫博物院观众服务"

    def test_unaligned_entry_is_reported_not_swallowed(self, db: Path, tmp_path: Path) -> None:
        """对不上实体的条目要点名报出来，不能只少写一条。"""
        _city(db)
        path = _seed_file(tmp_path, _entry())

        with connect(db) as conn:
            report = bs.seed_rules(
                path=path, conn=conn, search=search_returning(), fetch=_fetch()
            )

        assert report.written == 0
        assert report.failed[0][0] == "故宫博物院"
        assert "没搜到" in report.failed[0][1]

    def test_only_filter(self, db: Path, tmp_path: Path) -> None:
        _city(db)
        path = _seed_file(tmp_path, _entry(), _entry(name="午门", city="北京"))

        with connect(db) as conn:
            report = bs.seed_rules(
                path=path,
                conn=conn,
                search=search_returning(gugong()),
                fetch=_fetch(),
                only=["故宫博物院"],
            )

        assert report.total == 1

    def test_reseeding_updates_instead_of_duplicating(self, db: Path, tmp_path: Path) -> None:
        _city(db)
        path = _seed_file(tmp_path, _entry(advance_days=7))
        with connect(db) as conn:
            bs.seed_rules(path=path, conn=conn, search=search_returning(gugong()), fetch=_fetch())

        path2 = _seed_file(tmp_path, _entry(advance_days=10))
        with connect(db) as conn:
            bs.seed_rules(path=path2, conn=conn, search=search_returning(gugong()), fetch=_fetch())

        with connect(db) as conn:
            assert bs.rule_stats(conn=conn)["total"] == 1
            rule = bs.get_rule(gugong().poi_id, conn=conn)
        assert rule is not None and rule.advance_days == 10

    def test_seed_errors_are_carried_into_the_report(self, db: Path, tmp_path: Path) -> None:
        _city(db)
        path = _seed_file(tmp_path, _entry(channels=[]))
        with connect(db) as conn:
            report = bs.seed_rules(
                path=path, conn=conn, search=search_returning(gugong()), fetch=_fetch()
            )
        # 有错也照样入库（种子是材料，复核才是签字），但错误要带出来
        assert report.written == 1
        assert any(item.code == "no_channel" for item in report.errors)

    def test_reseeding_keeps_the_review_when_nothing_changed(
        self, db: Path, tmp_path: Path
    ) -> None:
        """加几条新规则不该让已复核的那些凭空失效。

        规则被打回草案意味着用户那边的提醒消失了，而他会以为
        「这个景点不用预约」——正是最怕的失败模式。
        """
        _city(db)
        path = _seed_file(tmp_path, _entry())
        with connect(db) as conn:
            bs.seed_rules(path=path, conn=conn, search=search_returning(gugong()), fetch=_fetch())
        with transaction(db) as conn:
            bs.review_rule(conn=conn, poi_id=gugong().poi_id, reviewed_at=TODAY)

        with connect(db) as conn:
            report = bs.seed_rules(
                path=path, conn=conn, search=search_returning(gugong()), fetch=_fetch()
            )

        assert report.kept_reviewed == 1
        rule = bs.get_rule(gugong().poi_id, conn=connect(db))
        assert rule is not None and rule.status is RuleStatus.REVIEWED

    def test_changed_content_goes_back_to_draft(self, db: Path, tmp_path: Path) -> None:
        """内容真变了（放票时刻变了）就该重新走复核——那时本来就该重核。"""
        _city(db)
        path = _seed_file(tmp_path, _entry(advance_days=7))
        with connect(db) as conn:
            bs.seed_rules(path=path, conn=conn, search=search_returning(gugong()), fetch=_fetch())
        with transaction(db) as conn:
            bs.review_rule(conn=conn, poi_id=gugong().poi_id, reviewed_at=TODAY)

        changed = _seed_file(tmp_path, _entry(advance_days=5))
        with connect(db) as conn:
            report = bs.seed_rules(
                path=changed, conn=conn, search=search_returning(gugong()), fetch=_fetch()
            )

        assert report.kept_reviewed == 0
        rule = bs.get_rule(gugong().poi_id, conn=connect(db))
        assert rule is not None and rule.status is RuleStatus.DRAFT


class TestReviewGate:
    def _seeded(self, db: Path, tmp_path: Path, **overrides: object) -> str:
        _city(db)
        path = _seed_file(tmp_path, _entry(**overrides))
        with connect(db) as conn:
            bs.seed_rules(path=path, conn=conn, search=search_returning(gugong()), fetch=_fetch())
        return gugong().poi_id

    def test_clean_rule_can_be_reviewed(self, db: Path, tmp_path: Path) -> None:
        poi_id = self._seeded(db, tmp_path)
        with transaction(db) as conn:
            assert bs.review_rule(conn=conn, poi_id=poi_id, reviewed_at=TODAY) is True

        rule = bs.get_rule(poi_id, conn=connect(db))
        assert rule is not None
        assert rule.status is RuleStatus.REVIEWED
        assert rule.verify_due_at == date(2026, 12, 11)  # 90 天后

    def test_rule_without_evidence_cannot_be_reviewed(self, db: Path, tmp_path: Path) -> None:
        """这道门禁的全部意义：材料不齐的规则不许放出去。"""
        poi_id = self._seeded(db, tmp_path, evidence_url=None)
        with transaction(db) as conn:
            assert bs.review_rule(conn=conn, poi_id=poi_id, reviewed_at=TODAY) is False
        rule = bs.get_rule(poi_id, conn=connect(db))
        assert rule is not None and rule.status is RuleStatus.DRAFT

    def test_rule_with_bad_release_time_cannot_be_reviewed(
        self, db: Path, tmp_path: Path
    ) -> None:
        poi_id = self._seeded(db, tmp_path, release_time="20点")
        with transaction(db) as conn:
            assert bs.review_rule(conn=conn, poi_id=poi_id, reviewed_at=TODAY) is False

    def test_evidence_can_be_supplied_at_review_time(self, db: Path, tmp_path: Path) -> None:
        poi_id = self._seeded(db, tmp_path, evidence_url=None)
        with transaction(db) as conn:
            assert (
                bs.review_rule(
                    conn=conn,
                    poi_id=poi_id,
                    reviewed_at=TODAY,
                    evidence_url="https://www.dpm.org.cn/visit.html",
                )
                is True
            )

    def test_unknown_poi_is_refused(self, db: Path) -> None:
        with transaction(db) as conn:
            assert bs.review_rule(conn=conn, poi_id="B_MISSING") is False


class TestLintDatabase:
    def test_reviewed_rule_must_keep_evidence(self, db: Path, tmp_path: Path) -> None:
        _city(db)
        path = _seed_file(tmp_path, _entry())
        with connect(db) as conn:
            bs.seed_rules(path=path, conn=conn, search=search_returning(gugong()), fetch=_fetch())
        with transaction(db) as conn:
            bs.review_rule(conn=conn, poi_id=gugong().poi_id, reviewed_at=TODAY)
            # 事后有人把来源抹了
            conn.execute("UPDATE booking_rule SET evidence_url = NULL")

        findings = bs.lint_database(conn=connect(db), today=TODAY)
        assert any(item.code == "reviewed_without_evidence" for item in findings)

    def test_overdue_review_is_reported(self, db: Path, tmp_path: Path) -> None:
        _city(db)
        path = _seed_file(tmp_path, _entry())
        with connect(db) as conn:
            bs.seed_rules(path=path, conn=conn, search=search_returning(gugong()), fetch=_fetch())
        with transaction(db) as conn:
            bs.review_rule(conn=conn, poi_id=gugong().poi_id, reviewed_at=date(2025, 1, 1))

        findings = bs.lint_database(conn=connect(db), today=TODAY)
        assert any(item.code == "review_overdue" for item in findings)

    def test_errors_come_first(self, db: Path, tmp_path: Path) -> None:
        _city(db)
        path = _seed_file(tmp_path, _entry(booking_required=False, advance_days=3))
        with connect(db) as conn:
            bs.seed_rules(path=path, conn=conn, search=search_returning(gugong()), fetch=_fetch())
        findings = bs.lint_database(conn=connect(db), today=TODAY)
        assert findings
        assert findings[0].severity is Severity.ERROR or all(
            item.severity is not Severity.ERROR for item in findings
        )


class TestRuleStats:
    def test_counts_reviewed_and_evidence(self, db: Path, tmp_path: Path) -> None:
        _city(db)
        path = _seed_file(tmp_path, _entry())
        with connect(db) as conn:
            bs.seed_rules(path=path, conn=conn, search=search_returning(gugong()), fetch=_fetch())
            assert bs.rule_stats(conn=conn)["reviewed"] == 0
        with transaction(db) as conn:
            bs.review_rule(conn=conn, poi_id=gugong().poi_id, reviewed_at=TODAY)
        with connect(db) as conn:
            stats = bs.rule_stats(conn=conn)
        assert stats["reviewed"] == 1
        assert stats["with_evidence"] == 1


class TestAlertsForTrip:
    def _trip_with_visit(self, db: Path) -> str:
        """造一份只有一天一项的行程，指向故宫。"""
        _city(db)
        poi = gugong()
        with transaction(db) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO poi (amap_poi_id, name, city_adcode, adcode, "
                "typecode, lat_gcj02, lng_gcj02, fetched_at) VALUES (?, ?, '110100', "
                "'110101', '110201', 39.918, 116.397, ?)",
                (poi.poi_id, poi.name, TODAY.isoformat()),
            )
            conn.execute(
                "INSERT INTO trip (id, name, start_date, created_at, updated_at) "
                "VALUES ('trip_1', '北京三日', ?, ?, ?)",
                (TODAY.isoformat(), TODAY.isoformat(), TODAY.isoformat()),
            )
            conn.execute(
                "INSERT INTO city_stay (id, trip_id, city_adcode, city_name, seq, stay_days) "
                "VALUES ('cs_1', 'trip_1', '110100', '北京', 0, 1)"
            )
            conn.execute(
                "INSERT INTO day (id, trip_id, city_stay_id, date, seq_in_stay) "
                "VALUES ('day_1', 'trip_1', 'cs_1', ?, 0)",
                ((date(2026, 10, 1)).isoformat(),),
            )
            conn.execute(
                "INSERT INTO day_item (id, day_id, seq, kind, poi_id, title, origin) "
                "VALUES ('di_1', 'day_1', 0, 'poi', ?, '故宫博物院', 'ai')",
                (poi.poi_id,),
            )
        return "trip_1"

    def _rule(self, db: Path, tmp_path: Path, *, reviewed: bool) -> None:
        path = _seed_file(tmp_path, _entry())
        with connect(db) as conn:
            bs.seed_rules(path=path, conn=conn, search=search_returning(gugong()), fetch=_fetch())
        if reviewed:
            with transaction(db) as conn:
                bs.review_rule(conn=conn, poi_id=gugong().poi_id, reviewed_at=TODAY)

    def test_draft_rule_produces_no_alert(self, db: Path, tmp_path: Path) -> None:
        """未经复核的规则不上清单——宁可少提醒，也不能让用户白跑。"""
        trip_id = self._trip_with_visit(db)
        self._rule(db, tmp_path, reviewed=False)

        with connect(db) as conn:
            assert bs.alerts_for_trip(trip_id, conn=conn, today=TODAY) == []
            # 但要说得出「这里没提醒是因为规则还是草案」
            assert bs.pending_rule_pois(trip_id, conn=conn) == ["故宫博物院"]

    def test_reviewed_rule_produces_an_alert(self, db: Path, tmp_path: Path) -> None:
        trip_id = self._trip_with_visit(db)
        self._rule(db, tmp_path, reviewed=True)

        with connect(db) as conn:
            alerts = bs.alerts_for_trip(trip_id, conn=conn, today=TODAY)

        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.poi_name == "故宫博物院"
        # 10 月 1 日游览、提前 7 天放票 → 9 月 24 日放票 → 距今 12 天
        assert alert.release_date == date(2026, 9, 24)
        assert alert.days_until_release == 12
        assert alert.release_time == "20:00"

    def test_trip_without_visits_is_empty(self, db: Path) -> None:
        _city(db)
        assert bs.alerts_for_trip("trip_none", conn=connect(db), today=TODAY) == []
