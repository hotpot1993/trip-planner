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

    def test_folding_a_multi_line_block_is_rejected(self) -> None:
        """折行是按**逻辑行**做的，整块含换行的文本必须当场炸。

        原先 `build_calendar` 把整个时区块当成 `raw` 里的**一项**交给 `fold`，
        于是那九行被当成一个逻辑行，在累计第 75 字节处硬切：

            DTSTART:19700101T00000
             0
            ...
            END:VTI
             MEZONE

        一份这样的 .ics 是坏的，而客户端对它**静默不认**——不报错，只是事件
        不出现。日历是唯一把预约提醒送到手机上的通道，所以这个错的表现是
        「提醒从来没到过」，而服务端一点异常都看不到。
        """
        with pytest.raises(ValueError, match="逻辑行"):
            ics.fold("BEGIN:VTIMEZONE\r\nTZID:Asia/Shanghai\r\nEND:VTIMEZONE")


def _unfold(text: str) -> list[str]:
    """把折好的 .ics 展开回逻辑行（续行以空格开头）。"""
    lines: list[str] = []
    for line in text.split("\r\n"):
        if line.startswith(" ") and lines:
            lines[-1] += line[1:]
        elif line:
            lines.append(line)
    return lines


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

    def test_the_timezone_block_survives_whole(self) -> None:
        """时区块必须原样出现在产物里，一行都不能被切开。

        **上一版这里漏得刚好：** `END:VTIMEZONE` 不在上面那个必需清单里，
        而被切坏的第一处正好落在 `DTSTART:19700101T000000` 的中间、第二处
        落在 `END:VTIMEZONE` 的中间。清单里留下的几个字符串都在切点之前，
        于是测试全绿，产物却是坏的。
        """
        text, _ = ics.build_calendar([])
        lines = _unfold(text)

        for line in ics._VTIMEZONE_LINES:
            assert line in lines, f"时区块这一行不在产物里：{line}"

    def test_no_structural_line_is_pushed_onto_a_continuation(self) -> None:
        """续行只能是长值折出来的，不能是结构行的一部分。

        结构行的长度都是固定的短串；它们出现在续行里，就说明折行切错了位置。
        """
        text, _ = ics.build_calendar([_alert()])
        continuations = [line for line in text.split("\r\n") if line.startswith(" ")]

        for line in continuations:
            assert not line[1:].startswith("TZOFFSET"), f"结构行被折了：{line!r}"
            assert "END:VTI" not in line[1:], f"结构行被折了：{line!r}"

    def test_every_octet_limit_holds(self) -> None:
        """折行之后每一行的字节数都不超过 75——中文按 UTF-8 算。"""
        text, _ = ics.build_calendar([_alert()], trip_name="北京三日")
        for line in text.split("\r\n"):
            assert len(line.encode("utf-8")) <= ics.MAX_LINE_OCTETS

    def test_unfolding_recovers_the_logical_lines(self) -> None:
        """展开之后能拿回原始逻辑行，且描述那一段一个字节没丢。"""
        text, _ = ics.build_calendar([_alert()], trip_name="北京三日")
        lines = _unfold(text)
        description = next(line for line in lines if line.startswith("DESCRIPTION:"))

        assert description.endswith("以官方渠道为准。")
        assert "故宫博物院观众服务" in description

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

        断言要**只看这个事件**：原先这里写的是「全文里不含 `T000000`」，
        而它当时能通过，靠的正是时区块被折行切坏了——`DTSTART:19700101T000000`
        被切成 `…T00000` 与 `0`，全文里于是真的没有这串。修好折行之后这句
        立刻变红：一条测试靠另一个 bug 才通过，是这两处互相掩护的典型。
        """
        text, _ = ics.build_calendar([_alert(release_time=None)])
        event = text.split("BEGIN:VEVENT")[1].split("END:VEVENT")[0]

        assert "DTSTART;VALUE=DATE:20260924" in text
        assert "DTEND;VALUE=DATE:20260925" in text
        assert "T000000" not in event, "事件本身不该出现时刻"

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
