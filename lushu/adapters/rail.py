"""铁路数据适配器：12306 官方接口。

三个事实，全部来自实测（脚本见 `scripts/probe_12306*.py`）：

1. **预售期约 14 天。** T+14 仍可查，T+15 起 12306 会把请求重定向到
   `/mormhweb/logFiles/error.html`。多城市行程常常提前几周规划，所以
   「查不到车次」通常不是出错，而是还没到放票期——这一点必须如实告诉用户，
   而不是让它看起来像一次失败。
2. **余票响应里没有票价。** 下标 30/31/32 是余票数量（「有」或数字）。
3. **票价接口当前不可用。** `queryAllPublicPrice` 与 `query` 的所有参数变体
   （日期格式、补电报码、换最早可售日）都返回 `status=False` 与
   「系统忙，请稍后重试」；经停站接口可用但不含票价字段。因此票价按里程估价
   并标注为参考价——这正是设计里预留的降级路径。

**不关闭 TLS 校验。** 上游参考项目这么做了，本模块不照抄。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import httpx

from lushu import config
from lushu.domain.transfer import (
    PriceSource,
    TrainClass,
    TransferMode,
    TransferOption,
    estimate_rail_fare,
)

STATION_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
TICKET_URL = "https://kyfw.12306.cn/otn/leftTicket/queryG"
INIT_URL = "https://kyfw.12306.cn/otn/leftTicket/init"

# 12306 预售期。超过它就别去问了，直接告诉用户还没到放票期。
SALE_WINDOW_DAYS = 14
# 车站名表很少变，缓存一个月
STATION_CACHE_DAYS = 30

# 12306 对 UA 与 Referer 挑剔；缺了会被重定向到错误页
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
    "Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# ─── 已验证的余票字段下标 ──────────────────────────────────────
#
# 2026-09 实测，用 G / D / K / T 四类车交叉验证。**不要凭印象改这里**：
# 挪一位就会把「无座」的数量当成「商务座」报给用户。
#
#   26      无座，各类车都有
#   30/31/32 二等座 / 一等座 / 商务座，仅高铁与动车
#   23/28/29 在普速与部分动车上出现，但**无法从实测确定确切席别名**，
#            因此只用于判断「有没有票」，不对外声称席别
_SEAT_COLUMNS: dict[int, str] = {
    26: "无座",
    30: "二等座",
    31: "一等座",
    32: "商务座",
}
# 只用来判断有没有票，不对外声称席别
_ANY_TICKET_COLUMNS = (23, 26, 28, 29, 30, 31, 32)

# 「无」表示该席别没有票；空字符串表示这趟车没有这种席别
_NO_TICKET = "无"


class RailUnavailableError(RuntimeError):
    """12306 返回了无法解析的内容。"""


@dataclass(frozen=True)
class Station:
    name: str
    telecode: str
    pinyin: str = ""


@dataclass(frozen=True)
class RailQuery:
    """一次城际铁路查询的结果。"""

    travel_date: date
    within_sale_window: bool
    from_station: Station | None = None
    to_station: Station | None = None
    options: tuple[TransferOption, ...] = ()
    note: str | None = None

    @property
    def best(self) -> TransferOption | None:
        """最快的方案。options 已按耗时升序排好。"""
        return self.options[0] if self.options else None

    @property
    def best_minutes(self) -> int | None:
        best = self.best
        return best.duration_min if best else None


# ─── 车站名表 ────────────────────────────────────────────────


def parse_station_table(text: str) -> list[Station]:
    """解析 12306 的 station_name.js。

    格式是 `@简拼|中文名|电报码|拼音|简拼|序号|...`，条目用 @ 分隔。
    """
    start = text.find("@")
    if start < 0:
        return []

    stations: list[Station] = []
    for entry in text[start:].split("@"):
        if not entry:
            continue
        parts = entry.split("|")
        if len(parts) < 3:
            continue
        name = parts[1].strip()
        telecode = parts[2].strip()
        if not name or not telecode:
            continue
        stations.append(
            Station(name=name, telecode=telecode, pinyin=parts[3].strip() if len(parts) > 3 else "")
        )
    return stations


def _station_cache_path() -> Path:
    return config.DATA_DIR / "rail_stations.json"


def load_stations(
    *,
    client: httpx.Client | None = None,
    cache_path: Path | None = None,
    today: date | None = None,
) -> list[Station]:
    """取车站名表，优先用本地缓存。

    这份表有一万多条、一百多 KB，而它几乎不变，所以缓存一个月。
    """
    path = cache_path or _station_cache_path()
    today = today or date.today()

    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            fetched = date.fromisoformat(payload.get("fetched_at", ""))
            if (today - fetched).days < STATION_CACHE_DAYS:
                return [Station(**item) for item in payload.get("stations", [])]
        except (OSError, ValueError, TypeError):
            # 缓存坏了就当没有，重新拉
            pass

    owned = client is None
    active = client or _new_client()
    try:
        response = active.get(STATION_URL)
        response.raise_for_status()
        stations = parse_station_table(response.text)
    finally:
        if owned:
            active.close()

    if not stations:
        raise RailUnavailableError("12306 车站名表解析为空，接口格式可能变了")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "fetched_at": today.isoformat(),
                    "stations": [
                        {"name": s.name, "telecode": s.telecode, "pinyin": s.pinyin}
                        for s in stations
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        # 写不进缓存不影响本次查询
        pass

    return stations


def station_candidates(city_name: str, stations: list[Station]) -> list[Station]:
    """给出这座城市可能的主站，按「更像主站」排序。

    排序依据是站名本身：先市名本体，再按常见的方位后缀。同一长度下
    南、东优先——多数城市的高铁主站是这两个方向。这只是个排序，
    真正的取舍交给调用方：它会按顺序试几组站对，取查得到车次的那组。
    """
    city = city_name.strip().removesuffix("市")
    if not city:
        return []

    suffix_priority = {"南": 0, "东": 1, "北": 2, "西": 3}
    matched = [
        station
        for station in stations
        if station.name == city or station.name.startswith(city)
    ]

    def sort_key(station: Station) -> tuple[int, int, str]:
        suffix = station.name[len(city) :]
        return (len(suffix), suffix_priority.get(suffix, 9), station.name)

    return sorted(matched, key=sort_key)


def pick_station(city_name: str, stations: list[Station]) -> Station | None:
    """取这座城市排在最前的主站。"""
    candidates = station_candidates(city_name, stations)
    return candidates[0] if candidates else None


# ─── 预售期 ──────────────────────────────────────────────────


def within_sale_window(travel_date: date, *, today: date | None = None) -> bool:
    """是否落在 12306 的预售期内。

    只卖当天起的 14 天内。提前一个月的行程查不到车次是正常的，
    不是接口出错——调用方据此给出不同的提示。
    """
    today = today or date.today()
    offset = (travel_date - today).days
    return 0 <= offset <= SALE_WINDOW_DAYS


# ─── 余票查询 ────────────────────────────────────────────────


def parse_ticket_rows(
    rows: list[str],
    station_names: dict[str, str],
    *,
    distance_km: float | None = None,
) -> list[TransferOption]:
    """解析余票响应的每一行。

    按已验证的下标取字段（见 `_SEAT_COLUMNS` 的说明）。票价不在响应里，
    因此按里程估价并标注为参考价。
    """
    options: list[TransferOption] = []

    for raw in rows:
        parts = raw.split("|")
        if len(parts) < 33:
            continue

        service_no = parts[3].strip()
        dep_time = parts[8].strip()
        arr_time = parts[9].strip()
        duration_text = parts[10].strip()
        if not service_no or not duration_text:
            continue

        duration_min = _parse_duration(duration_text)
        if duration_min is None:
            continue

        train_class = TrainClass.of(service_no)
        seats = {
            label: parts[index].strip()
            for index, label in _SEAT_COLUMNS.items()
            if index < len(parts) and parts[index].strip()
        }
        has_tickets = any(
            index < len(parts) and parts[index].strip() not in ("", _NO_TICKET)
            for index in _ANY_TICKET_COLUMNS
        )

        price: float | None = None
        source = PriceSource.ESTIMATE
        reference = True
        note: str | None = "票价是参考价：12306 的票价接口当前不可用"
        if distance_km is not None and distance_km > 0:
            price = estimate_rail_fare(distance_km, train_class)

        options.append(
            TransferOption(
                mode=TransferMode.RAIL,
                service_no=service_no,
                from_station=station_names.get(parts[6].strip(), parts[6].strip()),
                to_station=station_names.get(parts[7].strip(), parts[7].strip()),
                dep_time=dep_time,
                arr_time=arr_time,
                duration_min=duration_min,
                price=price,
                price_source=source,
                is_reference_price=reference,
                has_tickets=has_tickets,
                seats=seats,
                note=note,
            )
        )

    options.sort(key=lambda option: option.duration_min)
    return options


def query_rail(
    from_city: str,
    to_city: str,
    travel_date: date,
    *,
    distance_km: float | None = None,
    today: date | None = None,
    client: httpx.Client | None = None,
    stations: list[Station] | None = None,
) -> RailQuery:
    """查两座城市之间的铁路方案。

    **实测结论：站代码只用来定位城市。** 同城不同站（南京 / 南京南，
    西安 / 西安北 / 西安东）返回的车次集合完全相同，响应里还带着每个车次
    真正停靠的站。证据在 `scripts/probe_12306_station_scope.py`。
    所以按站对枚举是多余的四倍请求——一次就够。

    仍然留一次兜底：万一主站代码查不到，换这座城市的下一个候选站再问一次。
    """
    today = today or date.today()

    if not within_sale_window(travel_date, today=today):
        return RailQuery(
            travel_date=travel_date,
            within_sale_window=False,
            note=(
                f"12306 的预售期只有 {SALE_WINDOW_DAYS} 天，"
                f"{travel_date.isoformat()} 还没到放票期，暂时查不到车次与票价。"
            ),
        )

    table = stations if stations is not None else load_stations(client=client, today=today)
    from_candidates = station_candidates(from_city, table)
    to_candidates = station_candidates(to_city, table)

    if not from_candidates or not to_candidates:
        return RailQuery(
            travel_date=travel_date,
            within_sale_window=True,
            note=f"没能从车站名表里找到「{from_city}」或「{to_city}」的车站",
        )

    owned = client is None
    active = client or _new_client()
    options: list[TransferOption] = []
    note: str | None = None

    try:
        # 12306 对会话敏感，先访问一次 init 页
        try:
            active.get(INIT_URL)
        except httpx.HTTPError:
            pass

        attempts = [(from_candidates[0], to_candidates[0])]
        if len(from_candidates) > 1:
            attempts.append((from_candidates[1], to_candidates[0]))

        for from_station, to_station in attempts:
            try:
                payload = _request_tickets(active, from_station, to_station, travel_date)
            except RailUnavailableError as exc:
                note = str(exc)
                continue

            options = parse_ticket_rows(
                payload.get("result") or [],
                payload.get("map") or {},
                distance_km=distance_km,
            )
            if options:
                break
    finally:
        if owned:
            active.close()

    if not options and note is None:
        note = f"{from_city} 到 {to_city} 在 {travel_date.isoformat()} 没有查到直达车次"

    return RailQuery(
        travel_date=travel_date,
        within_sale_window=True,
        from_station=from_candidates[0],
        to_station=to_candidates[0],
        options=tuple(options),
        note=note,
    )


def _request_tickets(
    client: httpx.Client, from_station: Station, to_station: Station, travel_date: date
) -> dict:
    """发一次余票请求并解析成 12306 的 data 结构。"""
    params = {
        "leftTicketDTO.train_date": travel_date.isoformat(),
        "leftTicketDTO.from_station": from_station.telecode,
        "leftTicketDTO.to_station": to_station.telecode,
        "purpose_codes": "ADULT",
    }

    try:
        response = client.get(TICKET_URL, params=params)
    except httpx.HTTPError as exc:
        raise RailUnavailableError(f"12306 请求失败：{type(exc).__name__}") from exc

    text = response.text.lstrip()
    if not text.startswith("{"):
        # 超出售票期或风控触发时会返回 HTML 错误页
        raise RailUnavailableError(
            f"12306 返回了非 JSON 内容（最终地址 {response.url.path}），通常是超出预售期或触发了风控"
        )

    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise RailUnavailableError("12306 返回的内容不是合法 JSON") from exc

    if body.get("status") is not True:
        raise RailUnavailableError(f"12306 查询失败：{body.get('messages') or body.get('httpstatus')}")

    return body.get("data") or {}


def _new_client() -> httpx.Client:
    # 不关 TLS 校验。参考项目关掉了，那是把整条链路降级成可中间人篡改。
    return httpx.Client(timeout=25, headers=_HEADERS, follow_redirects=True)


def _parse_duration(text: str) -> int | None:
    """把「05:42」解析成分钟数。"""
    match = re.fullmatch(r"(\d{1,3}):(\d{2})", text.strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


# ─── 地理距离 ────────────────────────────────────────────────


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两点间的大圆距离。

    这是直线距离，实际铁路里程通常长 15% 到 30%，所以估价前要乘一个系数。
    这里是纯几何，不涉及任何引擎代码。
    """
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


# 铁路里程相对直线距离的放大系数。经验值，用于估价而非精确计算。
RAIL_DETOUR_FACTOR = 1.25


def rail_distance_km(
    from_point: tuple[float, float], to_point: tuple[float, float]
) -> float:
    """估算铁路里程。入参是 (纬度, 经度)，GCJ-02。"""
    straight = haversine_km(from_point[0], from_point[1], to_point[0], to_point[1])
    return straight * RAIL_DETOUR_FACTOR


def sale_window_end(today: date | None = None) -> date:
    """当前可售的最后一天。用于给用户一个明确的提示。"""
    return (today or date.today()) + timedelta(days=SALE_WINDOW_DAYS)
