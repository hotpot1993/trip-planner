"""把路书渲染成单个 HTML 文件。

设计第八节：**单个 HTML 文件，手机优先、离线可读**，旅行途中在手机上查阅。

这个渲染器的形状由三条约束决定，每一条都写进了契约（`domain/roadbook.py`），
错了不会抛异常，只会让产物在真正要用的时候不能用：

1. **零外部请求。** 没有字体、没有 CDN、没有图片链接。样式全部内联。
   旅行途中信号不好是常态，而「大理到丽江的山路上打不开」是没有补救的。
2. **没有地图，靠文字。** 地图被砍掉了（ADR-0006），所以每一天都要读得懂：
   几点、去哪儿、怎么去、多远、多久。路段那段文字是空间关系的全部载体。
3. **手机优先。** 窄屏单列、触控目标够大、字号按手机阅读定。
   这份东西只有一种使用场景：站在路边低头看。

配色沿用主项目的青绿山水：石青是结构色，石绿是打卡，**朱砂只标三类
「会让你的计划落空」的信息**（需要预约而没约、景点没对上实体、当天不宜户外）。
"""

from __future__ import annotations

from html import escape

from lushu.domain.roadbook import (
    Roadbook,
    RoadbookBooking,
    RoadbookDay,
    RoadbookItem,
    RoadbookLeg,
)

# 全部内联。刻意没用任何设计系统——这份文件要能脱离项目独立存在，
# 在飞机上、在没有网的民宿里打开。
STYLE = """
:root{
  --paper:#e8ecee; --card:#f7f8f6; --sunk:#dde2e4;
  --ink:#1f2422; --ink2:#525b57; --ink3:#838b86; --rule:#c3cac6;
  --azurite:#2c5b7a; --azurite-soft:#d3e0e8;
  --malachite:#4e7a5c; --malachite-soft:#d8e4da;
  --cinnabar:#b23a2c; --cinnabar-soft:#f2ddd8;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font:15px/1.7 "PingFang SC","HarmonyOS Sans SC","Microsoft YaHei",system-ui,sans-serif;
  -webkit-text-size-adjust:100%}
.wrap{max-width:44rem;margin:0 auto;padding:1rem .9rem 3rem}
h1{font-size:1.4rem;margin:.2rem 0 .3rem}
h2{font-size:1.05rem;margin:0}
h3{font-size:.8rem;font-weight:600;margin:.7rem 0 .3rem}
.meta{color:var(--ink3);font-size:.78rem}
.data{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-variant-numeric:tabular-nums}
.card{background:var(--card);border:1px solid var(--rule);border-radius:6px;
  padding:.85rem .9rem;margin:.7rem 0}
.card.sunk{background:var(--sunk)}
.card.cinnabar{border-color:var(--cinnabar);background:var(--cinnabar-soft)}
.card.azurite{border-color:var(--azurite);background:var(--azurite-soft)}
.day-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:.5rem;
  border-bottom:1px solid var(--rule);padding-bottom:.4rem;margin-bottom:.5rem}
.day-head .date{font-size:1rem}
.theme{color:var(--ink2);font-size:.85rem}
.item{display:flex;gap:.6rem;padding:.4rem 0;border-top:1px solid var(--rule)}
.item:first-of-type{border-top:none}
.item .time{flex:0 0 3rem;color:var(--ink3);font-size:.8rem;padding-top:.15rem}
.item .body{flex:1 1 auto;min-width:0}
.item .title{font-weight:600}
.item .addr{color:var(--ink3);font-size:.78rem}
.badge{display:inline-block;border-radius:3px;padding:.05rem .35rem;font-size:.7rem;
  margin-left:.35rem;vertical-align:middle}
.badge.booking{background:var(--azurite);color:#fff}
.badge.pending{background:var(--cinnabar-soft);color:var(--cinnabar)}
.claims{margin:.3rem 0 0;padding:0;list-style:none}
.claims li{border-left:2px solid var(--rule);padding-left:.5rem;margin:.25rem 0;
  font-size:.82rem;color:var(--ink2)}
.claims li.hl{border-color:var(--malachite)}
.claims li.av{border-color:var(--azurite)}
.leg{margin:.35rem 0 .35rem 3.6rem;font-size:.8rem;color:var(--ink2)}
.leg a{color:var(--azurite)}
.leg .est{color:var(--ink3)}
.transfer{background:var(--azurite-soft);border:1px solid var(--azurite);border-radius:6px;
  padding:.6rem .7rem;margin:.5rem 0;font-size:.85rem}
.book{display:flex;flex-wrap:wrap;gap:.4rem;align-items:baseline}
.book .head{color:var(--azurite);font-weight:600}
.book a{color:var(--azurite);word-break:break-all}
table.budget{width:100%;border-collapse:collapse;font-size:.85rem}
table.budget td{padding:.2rem 0;border-bottom:1px solid var(--rule)}
table.budget td:last-child{text-align:right}
footer{color:var(--ink3);font-size:.75rem;margin-top:1.5rem;line-height:1.6}
@media (prefers-color-scheme:dark){
  :root{--paper:#171b1a;--card:#1f2422;--sunk:#242a28;--ink:#e6eae7;--ink2:#b3bcb6;
    --ink3:#8a938d;--rule:#39413d;--azurite-soft:#1e3a4d;--malachite-soft:#22362a;
    --cinnabar-soft:#3d201c}
}
"""

MODE_LABEL = {
    "walk": "步行",
    "bike": "骑行",
    "metro": "地铁/公交",
    "bus": "公交",
    "taxi": "打车",
    "drive": "自驾",
    "other": "其他",
}

# 页面**自己会去加载**的东西。这些是「离线可读」的反面：
# 少了它们，页面在大理到丽江的山路上会缺字体、缺样式、缺图。
#
# **`<a href>` 不算。** 导航深链是用户主动点才会跳转的（ADR-0006 保留的
# 就是这部分），它不加载任何东西，断网时最多是点了没反应。
# 一开始我把两者混在一起查，结果把自家的导航链接判成了「不能离线打开」——
# 「页面自动加载的」与「用户主动点的」是两件事。
_AUTO_LOAD_PATTERNS = (
    r"""<script[^>]+src=["']([^"']+)""",
    r"""<link[^>]+href=["']([^"']+)""",
    r"""<img[^>]+src=["']([^"']+)""",
    r"""@import\s+(?:url\()?["']([^"']+)""",
    r"""url\(["']?(https?://[^)"']+)""",
)


def external_resources(html: str) -> list[str]:
    """产物里会被自动加载的外部资源。

    契约要求这个列表为空（`Roadbook.external_refs`）。
    """
    import re

    found: list[str] = []
    for pattern in _AUTO_LOAD_PATTERNS:
        found.extend(re.findall(pattern, html, flags=re.IGNORECASE))
    return [item for item in found if item.startswith(("http://", "https://", "//"))]


def render(roadbook: Roadbook) -> str:
    """渲染成一份完整的 HTML。"""
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{escape(roadbook.name)} · 路书</title>",
        f"<style>{STYLE}</style>",
        "</head><body><div class='wrap'>",
        f"<h1>{escape(roadbook.name)}</h1>",
        "<p class='meta'>"
        f"{roadbook.start_date} 至 {roadbook.end_date} · 共 {roadbook.total_days} 天 · "
        f"生成于 {escape(roadbook.generated_at)}</p>",
    ]

    if roadbook.bookings:
        parts.append(_bookings(roadbook.bookings))

    for day in roadbook.days:
        parts.append(_day(day))

    if roadbook.budget:
        parts.append(_budget(roadbook.budget))

    parts.append(
        "<footer>这份文件是离线生成的，不联网也能看。<br>"
        "攻略结论来自网友的公开分享，路书只带片段、不带全文；"
        "预约与开放时间以官方渠道为准。<br>"
        "路段的距离与耗时是按坐标估算的，实际路程会更长。</footer>"
    )
    parts.append("</div></body></html>")
    return "\n".join(parts)


def _bookings(bookings: tuple[RoadbookBooking, ...]) -> str:
    rows = ["<section class='card azurite'>", "<h2>出发前要预约</h2>"]
    for booking in bookings:
        channels = "".join(
            f"<div><a href='{escape(channel)}'>{escape(channel)}</a></div>"
            if channel.startswith("http")
            else f"<div>{escape(channel)}</div>"
            for channel in (booking.channels or ("渠道待补",))
        )
        release = (
            f"<span class='data'>放票 {booking.release_date}</span>"
            if booking.release_date
            else ""
        )
        rows.append(
            "<div class='card sunk'>"
            f"<div class='book'><span class='title'>{escape(booking.poi_name)}</span>"
            f"<span class='head'>{escape(booking.headline)}</span>{release}</div>"
            f"<div class='meta'>计划游览 {booking.visit_date}</div>"
            f"<div style='margin-top:.3rem;font-size:.82rem'>{channels}</div>"
            "</div>"
        )
    rows.append("</section>")
    return "".join(rows)


def _day(day: RoadbookDay) -> str:
    parts = [
        "<section class='card'>",
        "<div class='day-head'>",
        f"<span class='date data'>{day.date}</span>",
        f"<span class='meta'>{escape(day.city_name)} · 第 {day.seq_in_stay + 1} 天</span>",
        f"<span class='theme'>{escape(day.theme)}</span>" if day.theme else "",
        "</div>",
    ]

    if day.transfer:
        parts.append(f"<div class='transfer'>{escape(day.transfer)}</div>")

    if not day.items:
        parts.append("<p class='meta'>这一天还没有安排。</p>")

    for index, item in enumerate(day.items):
        parts.append(_item(item))
        # 路段排在它所连接的两项之间——顺读下来就是「到这儿、然后怎么走、再到那儿」
        if index < len(day.legs):
            parts.append(_leg(day.legs[index]))

    parts.append("</section>")
    return "".join(parts)


def _item(item: RoadbookItem) -> str:
    badges: list[str] = []
    if item.booking:
        badges.append(f"<span class='badge booking'>{escape(item.booking)}</span>")
    if item.kind == "poi" and not item.address:
        # 朱砂三类之一：景点没对上实体真源 / 没有地址，到了当地找不到
        badges.append("<span class='badge pending'>没地址</span>")

    claims: list[str] = []
    for text in item.avoids:
        claims.append(f"<li class='av'>避坑 · {escape(text)}</li>")
    for text in item.highlights:
        claims.append(f"<li class='hl'>打卡 · {escape(text)}</li>")

    return (
        "<div class='item'>"
        f"<div class='time data'>{escape(item.start_time or '——')}</div>"
        "<div class='body'>"
        f"<div class='title'>{escape(item.title)}{''.join(badges)}</div>"
        + (f"<div class='addr'>{escape(item.address)}</div>" if item.address else "")
        + (f"<div class='addr'>{escape(item.note)}</div>" if item.note else "")
        + (f"<ul class='claims'>{''.join(claims)}</ul>" if claims else "")
        + "</div></div>"
    )


def _leg(leg: RoadbookLeg | None) -> str:
    if leg is None:
        # 没有坐标就没有路段。**如实说**，不编一段看起来合理的文字——
        # 在陌生城市里按编出来的路线走，比知道「这段没算出来」糟得多。
        return "<div class='leg'>这段路没有坐标，到当地问一下</div>"

    bits: list[str] = [MODE_LABEL.get(leg.mode, leg.mode)]
    if leg.distance_m is not None:
        bits.append(f"约 {leg.distance_m} 米")
    if leg.duration_min is not None:
        bits.append(f"约 {leg.duration_min} 分钟")
    text = " · ".join(bits)
    est = f"<span class='est'>（{escape(leg.note)}）</span>" if leg.note else ""
    nav = f"　<a href='{escape(leg.nav_url)}'>导航</a>" if leg.nav_url else ""
    return f"<div class='leg'>↓ {escape(text)}{est}{nav}</div>"


def _budget(budget: tuple[tuple[str, str], ...]) -> str:
    rows = "".join(
        f"<tr><td>{escape(label)}</td><td class='data'>{escape(amount)}</td></tr>"
        for label, amount in budget
    )
    return f"<section class='card'><h2>预算</h2><table class='budget'>{rows}</table></section>"
