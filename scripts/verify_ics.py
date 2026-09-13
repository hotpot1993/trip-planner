"""用真实日历客户端同款的规则核对生成的 .ics。

单元测试断的是「我们写出的字符串长什么样」，这个脚本断的是
**「别人解出来的东西对不对」**——两者会分歧的地方正是 iCalendar 的坑：
折行、转义、时区。这里的做法是自己按 RFC 5545 反向解析一遍
（展开折行、反转义、取出属性），而不是再断言一遍字符串。

用法：
    python scripts/verify_ics.py <trip_id>
    python scripts/verify_ics.py --sample        # 不依赖行程，用样例数据
"""

from __future__ import annotations

import sys
from datetime import date, datetime

from _bootstrap import setup

setup()

from lushu.domain.booking import BookingAlert, Channel, ChannelKind, Urgency  # noqa: E402
from lushu.services import ics  # noqa: E402


def unfold(text: str) -> list[str]:
    """展开折行：CRLF 后面跟一个空格表示这一行是上一行的续行。"""
    lines: list[str] = []
    for raw in text.split("\r\n"):
        if raw.startswith(" ") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def unescape(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            out.append("\n" if nxt in "nN" else nxt)
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def parse(text: str) -> dict:
    """极简解析：只取我们真正会用的那几样。"""
    lines = unfold(text)
    events: list[dict] = []
    current: dict | None = None
    stack: list[str] = []
    for line in lines:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        name = key.split(";")[0].upper()
        params = key.split(";")[1:]
        if name == "BEGIN":
            stack.append(value.upper())
            if value.upper() == "VEVENT":
                current = {"alarms": []}
            continue
        if name == "END":
            if value.upper() == "VEVENT" and current is not None:
                events.append(current)
                current = None
            stack.pop() if stack else None
            continue
        if current is None:
            continue
        if name == "BEGIN" or (stack and stack[-1] == "VALARM"):
            if name == "TRIGGER":
                current["alarms"].append(value)
            continue
        current[name] = {"value": value, "params": params}

    return {
        "lines": lines,
        "events": events,
        "has_vtimezone": any(line.startswith("BEGIN:VTIMEZONE") for line in lines),
        "raw": text,
    }


def check(parsed: dict, expected_events: int) -> list[str]:
    problems: list[str] = []
    text = parsed["raw"]

    if not text.endswith("\r\n"):
        problems.append("文件末尾没有 CRLF")
    if text.replace("\r\n", "").count("\n"):
        problems.append("出现了裸 LF，RFC 5545 要求 CRLF")
    if not parsed["has_vtimezone"]:
        problems.append("没有 VTIMEZONE，带 TZID 的时刻会被客户端当成未知时区")
    if len(parsed["events"]) != expected_events:
        problems.append(f"解出 {len(parsed['events'])} 个事件，期望 {expected_events}")

    for event in parsed["events"]:
        for field in ("UID", "DTSTAMP", "DTSTART", "SUMMARY"):
            if field not in event:
                problems.append(f"事件缺 {field}")
        summary = event.get("SUMMARY", {}).get("value", "")
        if "\\" in summary and not any(ch in summary for ch in ",;"):
            problems.append(f"SUMMARY 里有可疑的转义：{summary}")
        start = event.get("DTSTART", {})
        if "TZID=Asia/Shanghai" not in ";".join(start.get("params", [])):
            problems.append("DTSTART 没有带上 Asia/Shanghai")

    return problems


def sample_alert(name: str = "故宫博物院", release: date = date(2026, 9, 24)) -> BookingAlert:
    return BookingAlert(
        poi_id="B000A8UIN8",
        poi_name=name,
        visit_date=date(2026, 10, 1),
        release_date=release,
        days_until_release=(release - date(2026, 9, 12)).days,
        urgency=Urgency.LATER,
        release_time="20:00",
        channels=(Channel("故宫博物院观众服务", ChannelKind.OFFICIAL_ACCOUNT, "https://gugong.ktmtech.cn/"),),
        requires_real_name=True,
        city_name="北京",
    )


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--sample":
        alerts = [
            sample_alert(),
            # 名字长到必须折行，且正文里有全角标点与英文逗号
            sample_alert(
                name="陕西历史博物馆（周一闭馆，需提前 3 天预约，免费不免票）",
                release=date(2026, 9, 28),
            ),
        ]
        text, skipped = ics.build_calendar(
            alerts, trip_name="北京西安七日", now=datetime(2026, 9, 12, 9, 0)
        )
        expected = 2
    elif argv:
        from lushu.services import ics as ics_service

        text, skipped, _name = ics_service.calendar_for_trip(argv[0])
        expected = text.count("BEGIN:VEVENT")
    else:
        print(__doc__)
        return 2

    parsed = parse(text)
    problems = check(parsed, expected)

    longest = max((len(line.encode("utf-8")) for line in text.split("\r\n")), default=0)
    print(f"事件 {len(parsed['events'])} 个，跳过的景点 {len(skipped)} 个")
    print(f"最长一行 {longest} 字节（RFC 5545 上限 75，折行后不应超过）")
    for event in parsed["events"]:
        summary = unescape(event["SUMMARY"]["value"])
        start = event["DTSTART"]["value"]
        alarms = len(event.get("alarms", []))
        print(f"  {start}  {summary}  提醒 {alarms} 个")

    if longest > 75:
        problems.append(f"有一行 {longest} 字节，超过上限——折行没生效")

    if problems:
        print("\n不通过：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("\n通过：折行、转义、时区都经得起反向解析。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
