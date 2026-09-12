"""探测 M2 需要的两个外部数据源是否可用，以及现有表结构。

M2 的城际转移与天气面板都依赖外部接口。在写适配器之前先确认它们真的能通，
免得按想象写完才发现要用不上。
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse

import httpx

DB = "data/lushu.db"

OUR_TABLES = {
    "city", "poi", "source_group", "source_document", "claim", "claim_evidence",
    "alignment_task", "booking_rule", "trip", "city_stay", "day", "day_item",
    "intercity_transfer", "leg_option", "budget_item", "workbench_task", "gold_label",
}

# 12306 的查询接口对 UA 与 Referer 挑剔
RAIL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
    "Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


def probe_tables() -> None:
    conn = sqlite3.connect(DB)
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    print("  本项目表数:", len([n for n in names if n in OUR_TABLES]))
    for table in ("intercity_transfer", "budget_item", "leg_option"):
        if table not in names:
            print(f"  {table}: 不存在")
            continue
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        print(f"  {table}: {', '.join(cols)}")
    conn.close()


def probe_open_meteo() -> None:
    """Open-Meteo：免 key，按经纬度查 16 天预报。"""
    print("  ── Open-Meteo ──")
    params = {
        "latitude": 32.060255,
        "longitude": 118.796877,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "Asia/Shanghai",
        "forecast_days": 16,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    try:
        response = httpx.get(url, timeout=20)
        print(f"    HTTP {response.status_code}")
        if response.status_code != 200:
            print("    " + response.text[:200])
            return
        data = response.json()
        daily = data.get("daily") or {}
        times = daily.get("time") or []
        print(f"    预报天数: {len(times)}  首日 {times[0] if times else '—'}  末日 {times[-1] if times else '—'}")
        if times:
            i = 0
            print(
                f"    首日示例: code={daily.get('weather_code', [None])[i]} "
                f"最高={daily.get('temperature_2m_max', [None])[i]}℃ "
                f"最低={daily.get('temperature_2m_min', [None])[i]}℃ "
                f"降水概率={daily.get('precipitation_probability_max', [None])[i]}%"
            )
    except Exception as exc:
        print(f"    失败: {type(exc).__name__}: {exc}")


def probe_amap_weather() -> None:
    """高德天气：与 POI 同一把 key，只覆盖约 4 天。"""
    print("  ── 高德天气 ──")
    from lushu import config

    key = config.amap_api_key()
    if not key:
        print("    跳过：没有 AMAP_API_KEY")
        return
    url = "https://restapi.amap.com/v3/weather/weatherInfo?" + urllib.parse.urlencode(
        {"key": key, "city": "320100", "extensions": "all", "output": "json"}
    )
    try:
        data = httpx.get(url, timeout=20).json()
        print(f"    status={data.get('status')} info={data.get('info')}")
        casts = (data.get("forecasts") or [{}])[0].get("casts") or []
        print(f"    预报天数: {len(casts)}")
        if casts:
            print(f"    首日示例: {casts[0].get('date')} {casts[0].get('dayweather')} "
                  f"{casts[0].get('nighttemp')}~{casts[0].get('daytemp')}℃")
    except Exception as exc:
        print(f"    失败: {type(exc).__name__}: {exc}")


def probe_12306_stations() -> None:
    """12306 车站名表：城市名 → 电报码。这是查车次的前提。"""
    print("  ── 12306 车站名表 ──")
    url = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
    try:
        response = httpx.get(url, timeout=25, headers=RAIL_HEADERS)
        print(f"    HTTP {response.status_code}  长度 {len(response.text)}")
        text = response.text
        marker = text.find("@")
        if marker < 0:
            print("    未找到条目分隔符，格式可能变了")
            return
        entries = [e for e in text[marker:].split("@") if e]
        print(f"    车站条目数: {len(entries)}")
        sample = entries[0].split("|")
        print(f"    单条字段数: {len(sample)}")
        nanjing = [e.split("|") for e in entries if e.split("|")[1].startswith("南京")]
        print(f"    南京相关车站: {[(p[1], p[2]) for p in nanjing][:6]}")
    except Exception as exc:
        print(f"    失败: {type(exc).__name__}: {exc}")


def probe_12306_tickets() -> None:
    """12306 余票查询本身。用南京南(NKH) → 西安北(EAY) 试。"""
    print("  ── 12306 余票查询 ──")
    base = "https://kyfw.12306.cn/otn/leftTicket/queryG"
    params = {
        "leftTicketDTO.train_date": "2026-10-01",
        "leftTicketDTO.from_station": "NKH",
        "leftTicketDTO.to_station": "EAY",
        "purpose_codes": "ADULT",
    }
    try:
        with httpx.Client(timeout=25, headers=RAIL_HEADERS, follow_redirects=True) as client:
            # 先访问一次 init 页拿到会话 cookie，12306 对此很敏感
            client.get("https://kyfw.12306.cn/otn/leftTicket/init")
            response = client.get(base, params=params)
            print(f"    HTTP {response.status_code}  content-type={response.headers.get('content-type')}")
            if response.status_code != 200:
                print("    " + response.text[:300])
                return
            try:
                data = response.json()
            except json.JSONDecodeError:
                print("    返回的不是 JSON：" + response.text[:300])
                return
            print(f"    httpstatus={data.get('httpstatus')}  status={data.get('status')}")
            if data.get("messages"):
                print(f"    messages={data['messages']}")
            result = data.get("data", {}).get("result") or []
            print(f"    车次数量: {len(result)}")
            if result:
                parts = result[0].split("|")
                print(f"    首个车次: {parts[3]}  {parts[8]}→{parts[9]}  历时 {parts[10]}")
                print(f"    字段总数: {len(parts)}")
    except Exception as exc:
        print(f"    失败: {type(exc).__name__}: {exc}")


def main() -> None:
    print("=== 现有表结构 ===")
    probe_tables()
    print()
    print("=== 天气数据源 ===")
    probe_open_meteo()
    probe_amap_weather()
    print()
    print("=== 铁路数据源 ===")
    probe_12306_stations()
    probe_12306_tickets()


if __name__ == "__main__":
    main()
