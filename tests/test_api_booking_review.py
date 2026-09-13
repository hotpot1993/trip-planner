"""预约规则复核队列的接口（Q47 的第三类队列）。

这道门禁的意义是「不让未复核的规则到达用户」——预约规则错一个字段，
用户就会白跑一趟。所以这里要守住两件事：

1. **体检结果与规则一起返回**。分两次请求的话，界面就有机会显示一条
   没有体检结论的规则，人就可能签下一个本该被拦住的字。
2. **有 error 的规则复核不过**，而且要说清是哪一关没过。
"""

from __future__ import annotations

from lushu.domain.booking import BookingRule, Channel, ChannelKind, RuleStatus
from lushu.services import booking_store as bs
from tests.test_api_booking import stub_cities  # noqa: F401  （夹具靠导入进本模块）


def _seed(api_client, *, poi_id: str, name: str, **overrides) -> None:
    """直接往库里写一条 POI 与一条规则。"""
    from lushu.config import DB_PATH
    from lushu.store import connect

    conn = connect(DB_PATH)
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO city (adcode, name, updated_at) "
                "VALUES ('410100', '郑州', '2026-09-13')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO poi (amap_poi_id, name, city_adcode, adcode, "
                "typecode, lat_gcj02, lng_gcj02, fetched_at) VALUES "
                "(?, ?, '410100', '410102', '140100', 34.75, 113.66, '2026-09-13')",
                (poi_id, name),
            )
            params = {
                "booking_required": True,
                "advance_days": 5,
                "release_time": "17:00",
                "channels": (Channel("官方公众号", ChannelKind.OFFICIAL_ACCOUNT),),
                "requires_real_name": True,
                "evidence_url": "https://www.chnmus.net/",
                "status": RuleStatus.DRAFT,
                "reviewed_at": None,
            }
            params.update(overrides)
            bs.write_rule(
                conn=conn,
                rule=BookingRule(poi_id=poi_id, **params),  # type: ignore[arg-type]
                now="2026-09-13T10:00:00",
            )
    finally:
        conn.close()


class TestListRules:
    def test_lists_with_findings_attached(self, api_client) -> None:
        """体检结果与规则一起返回：复核是「看着体检结果签字」。"""
        _seed(api_client, poi_id="B_HN", name="河南博物院")

        rows = api_client.get("/api/workbench/booking-rules").json()

        assert len(rows) == 1
        row = rows[0]
        assert row["poi_name"] == "河南博物院"
        assert row["advance_days"] == 5
        assert row["release_time"] == "17:00"
        assert row["errors"] == []
        assert isinstance(row["warnings"], list)

    def test_reports_errors_for_a_broken_rule(self, api_client) -> None:
        _seed(api_client, poi_id="B_BAD", name="某馆", release_time="晚上5点")

        row = api_client.get("/api/workbench/booking-rules").json()[0]

        assert row["errors"]
        assert any("HH:MM" in item for item in row["errors"])

    def test_pending_only_filters_out_reviewed(self, api_client) -> None:
        _seed(api_client, poi_id="B_A", name="甲馆")
        _seed(api_client, poi_id="B_B", name="乙馆")
        api_client.post("/api/workbench/booking-rules/B_A/review", json={})

        pending = api_client.get("/api/workbench/booking-rules?pending_only=true").json()
        assert [row["poi_id"] for row in pending] == ["B_B"]

    def test_empty_when_no_rules(self, api_client) -> None:
        assert api_client.get("/api/workbench/booking-rules").json() == []


class TestReview:
    def test_clean_rule_can_be_reviewed(self, api_client) -> None:
        _seed(api_client, poi_id="B_HN", name="河南博物院")

        response = api_client.post("/api/workbench/booking-rules/B_HN/review", json={})

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "reviewed"
        assert payload["reviewed_at"] is not None
        # 复验到期 = 复核日 + 90 天
        assert payload["verify_due_at"] is not None

    def test_rule_without_evidence_is_refused_with_a_reason(self, api_client) -> None:
        """门禁要拦人，而且要**说清是哪一关没过**。"""
        _seed(api_client, poi_id="B_NOEV", name="某馆", evidence_url=None)

        response = api_client.post("/api/workbench/booking-rules/B_NOEV/review", json={})

        assert response.status_code == 422
        assert "来源" in response.json()["detail"]

    def test_broken_release_time_is_refused(self, api_client) -> None:
        _seed(api_client, poi_id="B_BAD", name="某馆", release_time="17点")

        response = api_client.post("/api/workbench/booking-rules/B_BAD/review", json={})

        assert response.status_code == 422
        assert "HH:MM" in response.json()["detail"]

    def test_evidence_can_be_supplied_at_review_time(self, api_client) -> None:
        """人一边核一边补材料，是这条流程里最自然的动作。"""
        _seed(api_client, poi_id="B_NOEV", name="某馆", evidence_url=None)

        response = api_client.post(
            "/api/workbench/booking-rules/B_NOEV/review",
            json={"evidence_url": "https://www.chnmus.net/", "note": "官网首页写明"},
        )

        assert response.status_code == 200
        assert response.json()["evidence_url"] == "https://www.chnmus.net/"

    def test_revoke_stops_showing_it(self, api_client) -> None:
        """规则会变，发现不对时要能立刻停止展示，而不是只能删掉重来。"""
        _seed(api_client, poi_id="B_HN", name="河南博物院")
        api_client.post("/api/workbench/booking-rules/B_HN/review", json={})

        response = api_client.post(
            "/api/workbench/booking-rules/B_HN/review", json={"revoke": True}
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "draft"
        assert payload["reviewed_at"] is None
        # 材料留着：撤回的是签字，不是证据
        assert payload["evidence_url"] == "https://www.chnmus.net/"

    def test_missing_rule_is_404(self, api_client) -> None:
        assert (
            api_client.post("/api/workbench/booking-rules/B_NOPE/review", json={}).status_code
            == 404
        )


class TestReviewedRuleReachesTheTrip:
    """从复核到行程提醒，走一遍完整链路。"""

    def test_rule_must_be_reviewed_before_it_can_alert(
        self,
        api_client,
        stub_cities,  # noqa: F811 — 模块级 import 进来的夹具，pytest 按名字找它
    ) -> None:
        created = api_client.post(
            "/api/trips",
            json={"start_date": "2026-10-01", "cities": [{"name": "北京", "days": 1}]},
        ).json()
        trip_id = created["id"]

        from lushu.config import DB_PATH
        from lushu.store import connect

        conn = connect(DB_PATH)
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO poi (amap_poi_id, name, city_adcode, adcode, "
                    "typecode, lat_gcj02, lng_gcj02, fetched_at) VALUES "
                    "('B_GUGONG', '故宫博物院', '110100', '110101', '110201', 39.918, "
                    "116.397, '2026-09-13')"
                )
                day = conn.execute(
                    "SELECT id FROM day WHERE trip_id = ? LIMIT 1", (trip_id,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO day_item (id, day_id, seq, kind, poi_id, title, origin) "
                    "VALUES ('di_g', ?, 0, 'poi', 'B_GUGONG', '故宫博物院', 'ai')",
                    (day["id"],),
                )
                bs.write_rule(
                    conn=conn,
                    rule=BookingRule(
                        poi_id="B_GUGONG",
                        booking_required=True,
                        status=RuleStatus.DRAFT,
                        advance_days=7,
                        release_time="20:00",
                        channels=(Channel("官方小程序", ChannelKind.MINIAPP),),
                        evidence_url="https://www.dpm.org.cn/",
                    ),
                    now="2026-09-13T10:00:00",
                )
        finally:
            conn.close()

        # 草案阶段：清单里没有，但要说得出「为什么没有」
        before = api_client.get(f"/api/trips/{trip_id}/booking").json()
        assert before["alerts"] == []
        assert before["pending_review"] == ["故宫博物院"]

        # 复核之后，提醒就出现了
        api_client.post("/api/workbench/booking-rules/B_GUGONG/review", json={})

        after = api_client.get(
            f"/api/trips/{trip_id}/booking?today=2026-09-20"
        ).json()
        assert len(after["alerts"]) == 1
        assert after["alerts"][0]["poi_name"] == "故宫博物院"
        assert after["pending_review"] == []
