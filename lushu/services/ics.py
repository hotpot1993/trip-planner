"""预约清单导出成 .ics 日历。

设计里说得很清楚：**把提醒责任交给手机系统日历**（第 6.2 节）。这不是
「顺便做个导出」——预约这件事的要害是**时点**，而本项目是个本地工具，
用户不会一直开着它。日历是唯一能把提醒送到用户手上的通道。

生成的是 RFC 5545 的 iCalendar。几处必须守对的细节，错了客户端会静默
不认（不报错，只是事件不出现，这比报错更难查）：

1. 换行必须是 CRLF。
2. 一行超过 75 个八位组要折行，续行以一个空格开头。中文按 UTF-8 算字节数，
   不能按字符数——一个汉字是三字节，按字符折会超。
3. 文本值里的 `\\` `;` `,` 与换行要转义。
4. 时刻带上 Asia/Shanghai 的 VTIMEZONE。放票时刻是北京时间，
   给「浮动时间」的话用户在国外时区会收到错点的提醒。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta

from lushu.domain.booking import BookingAlert, Urgency

PRODID = "-//路书//预约清单//CN"
TZID = "Asia/Shanghai"
DOMAIN = "lushu.local"

# 提前多久提醒。放票多半是「到点就得点」，留十分钟准备足够，
# 再早反而会被忽略掉。
ALARM_MINUTES_BEFORE = 10

# 放票是个「到点点一下」的动作，给一刻钟的时长，日历上才画得出来
EVENT_MINUTES = 15

# RFC 5545 规定的一行上限（八位组），续行以空格开头
MAX_LINE_OCTETS = 75

# 中国自 1991 年起不再使用夏令时，所以一个 STANDARD 块就够，
# 不需要按年份列转换规则。
#
# **必须是「一行一项」的序列，不能先拼成一个含 \r\n 的整块。** 折行是按
# 逻辑行做的，整块交给 `fold` 会被当成一个逻辑行，在累计第 75 字节处硬切
# ——切出来的就是这个样子（真机实测，见 M7 的状态文档）：
#
#     DTSTART:19700101T00000
#      0
#     ...
#     END:VTI
#      MEZONE
#
# 一份这样的 .ics 是坏的，而按本模块开头那句话，客户端**静默不认**：
# 不报错，只是事件不出现。日历是设计 6.2 里唯一把提醒送到手机上的通道，
# 所以这个错等于提醒一直没送到，而这边一点异常都看不到。
_VTIMEZONE_LINES: tuple[str, ...] = (
    "BEGIN:VTIMEZONE",
    f"TZID:{TZID}",
    "BEGIN:STANDARD",
    "DTSTART:19700101T000000",
    "TZOFFSETFROM:+0800",
    "TZOFFSETTO:+0800",
    "TZNAME:CST",
    "END:STANDARD",
    "END:VTIMEZONE",
)


def escape(value: str) -> str:
    """iCalendar 文本值转义。

    反斜杠必须先换，否则后面插进去的反斜杠会被再转义一次。
    """
    text = value.replace("\\", "\\\\")
    text = text.replace(";", "\\;").replace(",", "\\,")
    return text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def fold(line: str) -> str:
    """按**字节**折行，续行以空格开头。

    中文字符是三字节，所以「75 个字符」是错的折法，会写出超长的行。
    折的时候要小心不要把一个多字节字符切成两半，所以按字符逐个累加、
    逐个判断字节数。

    **只接受一个逻辑行。** 传进来一段含换行的文本会被当成一个逻辑行来折，
    在第 75 字节处硬切——`.ics` 里那个切点会落在一行中间，把
    `DTSTART:19700101T000000` 切成 `DTSTART:19700101T00000` 与 `0`。
    这种事必须当场炸，不能"折出来看着也像那么回事"：客户端对坏掉的日历
    是静默不认的，而这一头看不到任何异常。
    """
    if "\r" in line or "\n" in line:
        raise ValueError(
            "fold 只处理一个逻辑行；含换行的文本要先拆成多行再逐行折"
            "（折行是按行做的，整块折会在行中间切开）"
        )

    encoded = line.encode("utf-8")
    if len(encoded) <= MAX_LINE_OCTETS:
        return line

    pieces: list[str] = []
    current = ""
    budget = MAX_LINE_OCTETS
    for char in line:
        size = len(char.encode("utf-8"))
        if len(current.encode("utf-8")) + size > budget:
            pieces.append(current)
            current = char
            # 续行开头那个空格也占一个字节
            budget = MAX_LINE_OCTETS - 1
        else:
            current += char
    if current:
        pieces.append(current)
    return "\r\n ".join(pieces)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%S")


def release_moment(alert: BookingAlert) -> datetime:
    """放票日那天的起点。规则没写时刻时就是 00:00，而调用方会用全天事件
    表达它（`build_event`），不会把这个 00:00 当成真实时刻。

    调用方只对算得出放票日的条目调它（`build_calendar` 已经把算不出的滤掉了）。
    """
    if alert.release_date is None:
        raise ValueError(f"「{alert.poi_name}」算不出放票日，不该走到这一步")
    at = time.fromisoformat(alert.release_time) if alert.release_time else time(0, 0)
    return datetime.combine(alert.release_date, at)


def _summary(alert: BookingAlert) -> str:
    if alert.urgency is Urgency.OVERDUE:
        return f"确认余票｜{alert.poi_name}"
    if not alert.has_fixed_time:
        # 没有固定时刻的只是「窗口打开了」，说「抢票」会让人以为要掐点
        return f"可以预约了｜{alert.poi_name}"
    return f"抢票｜{alert.poi_name}"


def _description(alert: BookingAlert, *, trip_name: str | None) -> str:
    lines: list[str] = []
    if trip_name:
        lines.append(f"行程：{trip_name}")
    lines.append(f"计划游览：{alert.visit_date.isoformat()}")
    if alert.days_until_release is not None and alert.days_until_release >= 0:
        verb = "放票" if alert.has_fixed_time else "可以开始预约"
        lines.append(f"距今 {alert.days_until_release} 天{verb}")
    lines.append(alert.headline)

    if alert.channels:
        lines.append("")
        lines.append("预约渠道：")
        for channel in alert.channels:
            suffix = f" {channel.url}" if channel.url else ""
            lines.append(f"· {channel.name}{suffix}")

    if alert.requires_real_name:
        lines.append("")
        lines.append("需要实名：提前把同行人的证件信息填好，放票时直接选人。")
    if alert.id_required_note:
        lines.append(f"证件要求：{alert.id_required_note}")
    if alert.note:
        lines.append("")
        lines.append("注意：")
        lines.append(alert.note)

    lines.append("")
    lines.append("规则来自路书的预约规则库，出发前请以官方渠道为准。")
    return "\n".join(lines)


def build_event(
    alert: BookingAlert,
    *,
    trip_name: str | None = None,
    now: datetime,
    sequence: int = 0,
) -> list[str]:
    """一个预约事件。`alert` 必须算得出放票日。

    两种形状，取决于规则有没有给出**具体时刻**：

    - 有时刻（故宫 20:00）：`DTSTART` 是那个时刻，带一条提前十分钟的提醒。
      这是一场要掐点的抢票。
    - 没时刻（上博提前 14 日放号）：**全天事件**，也不带提醒。
      写成 00:00 的话等于凭空造了一个精度，用户会照着零点去等——
      而他等的那一刻官方从没那么说过。
    """
    start = release_moment(alert)
    overdue = alert.urgency is Urgency.OVERDUE

    lines = [
        "BEGIN:VEVENT",
        f"UID:{alert.poi_id}-{alert.visit_date.isoformat()}@{DOMAIN}",
        f"DTSTAMP:{_stamp(now)}",
    ]

    if alert.has_fixed_time:
        lines.append(f"DTSTART;TZID={TZID}:{_stamp(start)}")
        lines.append(f"DTEND;TZID={TZID}:{_stamp(start + timedelta(minutes=EVENT_MINUTES))}")
    else:
        # 全天事件：DTEND 是**次日**，iCalendar 的 DTEND 是排他的
        lines.append(f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}")
        lines.append(f"DTEND;VALUE=DATE:{(start + timedelta(days=1)).strftime('%Y%m%d')}")

    lines.extend(
        [
            f"SUMMARY:{escape(_summary(alert))}",
            f"DESCRIPTION:{escape(_description(alert, trip_name=trip_name))}",
            "TRANSP:TRANSPARENT",
            f"SEQUENCE:{sequence}",
        ]
    )
    if alert.city_name:
        lines.append(f"LOCATION:{escape(alert.city_name)}")

    # 提醒只在两种情况下有意义：这是一个要掐点的时刻，而且它还没过去。
    # 已经过了放票日的条目不设提醒：过去的提醒弹不出来，
    # 只会让人以为「日历没同步」。
    if alert.has_fixed_time and not overdue:
        lines.extend(
            [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"TRIGGER:-PT{ALARM_MINUTES_BEFORE}M",
                f"DESCRIPTION:{escape(_summary(alert))}",
                "END:VALARM",
            ]
        )
    lines.append("END:VEVENT")
    return lines


def build_calendar(
    alerts: Sequence[BookingAlert],
    *,
    trip_name: str | None = None,
    now: datetime | None = None,
) -> tuple[str, list[str]]:
    """生成 .ics 文本。

    返回（日历文本，跳过的景点名）。**跳过的要报出来**：算不出放票日的规则
    生不成事件，静默少一条会让用户以为那个景点不用预约——这恰恰是设计里
    最怕的失败模式。
    """
    stamp = now or datetime.now()
    skipped: list[str] = []
    events: list[list[str]] = []
    for alert in alerts:
        if alert.release_date is None:
            skipped.append(alert.poi_name)
            continue
        events.append(build_event(alert, trip_name=trip_name, now=stamp))

    raw: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape(trip_name + ' 预约提醒' if trip_name else '预约提醒')}",
        f"X-WR-TIMEZONE:{TZID}",
    ]
    raw.extend(_VTIMEZONE_LINES)
    for event in events:
        raw.extend(event)
    raw.append("END:VCALENDAR")

    # 折行在最后统一做：先拼好逻辑行，再按字节折，免得在拼的过程中
    # 把已经折好的行又拼回一行。**`raw` 里每一项都必须是一个逻辑行**——
    # 多行的东西（比如时区块）要先 `extend` 进来，不能整块塞成一项。
    body = "\r\n".join(fold(line) for line in raw) + "\r\n"
    return body, skipped


def calendar_for_trip(
    trip_id: str, *, conn=None, today: date | None = None, now: datetime | None = None
) -> tuple[str, list[str], str]:
    """某份行程的预约日历。返回（文本，跳过的景点，行程名）。"""
    from lushu.services.booking_store import alerts_for_trip
    from lushu.store.connection import connect

    owned = conn is None
    active = conn or connect()
    try:
        alerts = alerts_for_trip(trip_id, conn=active, today=today)
        row = active.execute("SELECT name FROM trip WHERE id = ?", (trip_id,)).fetchone()
    finally:
        if owned:
            active.close()

    name = row["name"] if row is not None else None
    text, skipped = build_calendar(alerts, trip_name=name, now=now)
    return text, skipped, name or trip_id
