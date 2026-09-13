"""预约清单导出成 .ics。

这一层要守的是**客户端会不会静默不认**。iCalendar 的坑都在细节上：
换行、折行、转义、时区——错了不会报错，只是事件不出现，而「日历里没有」
和「这个景点不用预约」在用户眼里是同一件事。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from lushu.domain.booking import (
    BookingAlert,
    Channel,
    ChannelKind,
    Urgency,
)
from lushu.services import ics

TRIP_DATE = date(2026, 10, 1)


def _alert(
    *,
    poi_id: str = "B000A8UIN8",
    name: str = "故宫博物院",
    release_date: date | None = date(2026, 9, 24),
    release_time: str | None = "20:00",
    urgency: Urgency = Urgency.LATER,
) -> BookingAlert:
    days = (release_date - date(2026, 9, 12)).days if release_date else None
    return BookingAlert(
        poi_id=poi_id,
        poi_name=name,
        visit_date=TRIP_DATE,
        release_date=release_date,
        days_until_release=days,
        urgency=urgency,
        release_time=release_time,
        channels=(
            Channel("故宫博物院观众服务", ChannelKind.OFFICIAL_ACCOUNT, "https://gugong.ktmtech.cn/"),
        ),
        requires_real_name=True,
        city_name="北京",
    )


class TestEscape:
    def test_commas_and_semicolons_are_escaped(self) -> None:
        assert ics.escape("a,b;c") == "a\\,b\\;c"

    def test_fullwidth_punctuation_needs_no_escape(self) -> None:
        """RFC 5545 要转义的是 ASCII 的逗号与分号。

        中文正文里几乎全是全角标点，它们就是普通字符——顺手一起转义的话，
        中文描述里会到处冒出多余的反斜杠。
        """
        assert ics.escape("故宫，周一闭馆；北门只出不进") == "故宫，周一闭馆；北门只出不进"

    def test_backslash_goes_first(self) -> None:
        """反斜杠必须先换，否则后面插进去的反斜杠会被再转义一次。"""
        assert ics.escape("a\\b") == "a\\\\b"

    def test_newlines_become_literal_n(self) -> None:
        assert ics.escape("第一行\n第二行") == "第一行\\n第二行"


class TestFold:
    def test_short_lines_are_left_alone(self) -> None:
        assert ics.fold("SUMMARY:抢票") == "SUMMARY:抢票"

    def test_long_line_is_folded_at_75_octets(self) -> None:
        line = "DESCRIPTION:" + "故" * 60  # 远超 75 字节
        folded = ics.fold(line)
        assert "\r\n " in folded
        for piece in folded.split("\r\n"):
            assert len(piece.encode("utf-8")) <= ics.MAX_LINE_OCTETS

    def test_chinese_counts_by_bytes_not_characters(self) -> None:
        """一个汉字三字节，按字符折会写出超长的行，客户端可能直接不认。"""
        line = "X" * 20 + "宫" * 30
        folded = ics.fold(line)
        assert folded != line
        for piece in folded.split("\r\n"):
            assert len(piece.encode("utf-8")) <= ics.MAX_LINE_OCTETS

    def test_no_character_is_split_in_half(self) -> None:
        """按字节硬切会把汉字切成两半，解出来是乱码。"""
        line = "描" * 40
        folded = ics.fold(line)
        rejoined = folded.replace("\r\n ", "")
        assert rejoined == line


class TestBuildCalendar:
    def test_crlf_line_endings(self) -> None:
        text, _ = ics.build_calendar([_alert()], trip_name="北京三日")
        assert "\r\n" in text
        # 不能出现裸 LF
        assert text.replace("\r\n", "").count("\n") == 0

    def test_has_the_required_envelope(self) -> None:
        text, _ = ics.build_calendar([_alert()], trip_name="北京三日")
        for required in (
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:",
            "BEGIN:VTIMEZONE",
            "TZID:Asia/Shanghai",
            "END:VCALENDAR",
        ):
            assert required in text

    def test_event_carries_the_release_moment(self) -> None:
        text, _ = ics.build_calendar([_alert()], trip_name="北京三日")
        assert "DTSTART;TZID=Asia/Shanghai:20260924T200000" in text
        assert "DTEND;TZID=Asia/Shanghai:20260924T201500" in text

    def test_uid_is_stable_across_runs(self) -> None:
        """UID 变了日历客户端会当成新事件，同一件事堆两条。"""
        first, _ = ics.build_calendar([_alert()], now=datetime(2026, 9, 12, 9, 0))
        second, _ = ics.build_calendar([_alert()], now=datetime(2026, 9, 13, 9, 0))
        uid_lines = [line for line in first.split("\r\n") if line.startswith("UID:")]
        assert uid_lines == [line for line in second.split("\r\n") if line.startswith("UID:")]

    def test_alarm_is_set_for_future_release(self) -> None:
        text, _ = ics.build_calendar([_alert()], trip_name="北京三日")
        assert "BEGIN:VALARM" in text
        assert "TRIGGER:-PT10M" in text

    def test_overdue_alert_has_no_alarm(self) -> None:
        """过去的提醒弹不出来，留着只会让人以为「日历没同步」。"""
        text, _ = ics.build_calendar([_alert(urgency=Urgency.OVERDUE)])
        assert "BEGIN:VALARM" not in text
        assert "确认余票" in text

    def test_missing_release_date_is_skipped_and_reported(self) -> None:
        """算不出放票日的生不成事件，但**必须报出来**。

        静默少一条会让用户以为那个景点不用预约——恰恰是最怕的失败模式。
        """
        text, skipped = ics.build_calendar(
            [_alert(poi_id="B1"), _alert(poi_id="B2", name="布达拉宫", release_date=None)]
        )
        assert skipped == ["布达拉宫"]
        assert "布达拉宫" not in text

    def test_release_without_time_becomes_an_all_day_event(self) -> None:
        """没有具体时刻时是「那天起可以约了」，不是「零点开抢」。

        写成 00:00 等于凭空造了一个精度——用户会照着零点去等，
        而官方从没那么说过。
        """
        text, _ = ics.build_calendar([_alert(release_time=None)])
        assert "DTSTART;VALUE=DATE:20260924" in text
        assert "DTEND;VALUE=DATE:20260925" in text
        assert "T000000" not in text

    def test_all_day_event_has_no_timed_alarm(self) -> None:
        text, _ = ics.build_calendar([_alert(release_time=None)])
        assert "BEGIN:VALARM" not in text

    def test_all_day_event_is_not_called_a_ticket_grab(self) -> None:
        text, _ = ics.build_calendar([_alert(release_time=None)])
        assert "可以预约了" in text
        assert "抢票" not in text

    def test_description_lists_channels_and_visit_date(self) -> None:
        text, _ = ics.build_calendar([_alert()], trip_name="北京三日")
        flat = text.replace("\r\n ", "")
        assert "计划游览：2026-10-01" in flat
        assert "故宫博物院观众服务" in flat
        assert "需要实名" in flat

    def test_description_points_back_to_official_channels(self) -> None:
        """规则会过期，导出的日历会在手机上躺很久，这句必须在。"""
        text, _ = ics.build_calendar([_alert()])
        assert "以官方渠道为准" in text.replace("\r\n ", "")

    def test_no_alerts_still_produces_a_valid_calendar(self) -> None:
        text, skipped = ics.build_calendar([])
        assert text.startswith("BEGIN:VCALENDAR")
        assert text.rstrip().endswith("END:VCALENDAR")
        assert skipped == []

    def test_calendar_name_uses_the_trip(self) -> None:
        text, _ = ics.build_calendar([_alert()], trip_name="北京三日")
        assert "X-WR-CALNAME:北京三日 预约提醒" in text

    def test_multiple_events(self) -> None:
        text, _ = ics.build_calendar(
            [_alert(poi_id="B1"), _alert(poi_id="B2", name="陕西历史博物馆")]
        )
        assert text.count("BEGIN:VEVENT") == 2
        assert text.count("END:VEVENT") == 2


class TestReleaseMoment:
    def test_combines_date_and_time(self) -> None:
        assert ics.release_moment(_alert()) == datetime(2026, 9, 24, 20, 0)

    def test_raises_when_undecidable(self) -> None:
        with pytest.raises(ValueError, match="算不出放票日"):
            ics.release_moment(_alert(release_date=None))
