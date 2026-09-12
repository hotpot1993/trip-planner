"""把 12306 余票查询的问题查清楚。

上一次探测用的是 19 天后的日期，而 12306 的预售期只有约 15 天——超期的请求
很可能被重定向回首页。这里换近日期、换接口变体，并跟踪重定向，把原因定下来。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
    "Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 南京南 → 西安北
FROM_STATION = "NKH"
TO_STATION = "EAY"

ENDPOINTS = (
    "https://kyfw.12306.cn/otn/leftTicket/queryG",
    "https://kyfw.12306.cn/otn/leftTicket/query",
    "https://kyfw.12306.cn/otn/leftTicket/queryA",
    "https://kyfw.12306.cn/otn/leftTicket/queryZ",
)


def try_query(client: httpx.Client, url: str, train_date: str) -> None:
    params = {
        "leftTicketDTO.train_date": train_date,
        "leftTicketDTO.from_station": FROM_STATION,
        "leftTicketDTO.to_station": TO_STATION,
        "purpose_codes": "ADULT",
    }
    try:
        response = client.get(url, params=params)
    except Exception as exc:
        print(f"    {url.rsplit('/', 1)[-1]:<8} 请求失败 {type(exc).__name__}: {exc}")
        return

    name = url.rsplit("/", 1)[-1]
    ctype = response.headers.get("content-type", "")
    final = response.url.path

    if "json" in ctype or response.text.lstrip().startswith("{"):
        try:
            data = response.json()
        except json.JSONDecodeError:
            print(f"    {name:<8} HTTP {response.status_code} 声称 JSON 但解析失败")
            return
        result = (data.get("data") or {}).get("result") or []
        print(
            f"    {name:<8} HTTP {response.status_code}  JSON  "
            f"httpstatus={data.get('httpstatus')} status={data.get('status')} "
            f"车次 {len(result)}"
        )
        if result:
            parts = result[0].split("|")
            print(f"             示例 {parts[3]}  {parts[8]}→{parts[9]}  历时 {parts[10]}")
        return

    print(
        f"    {name:<8} HTTP {response.status_code}  {ctype.split(';')[0]:<10} "
        f"最终路径 {final}  长度 {len(response.text)}"
    )


def main() -> None:
    today = date.today()
    print(f"今天 {today}")
    print()

    with httpx.Client(timeout=25, headers=HEADERS, follow_redirects=True) as client:
        print("  先访问 init 页拿会话 cookie")
        init = client.get("https://kyfw.12306.cn/otn/leftTicket/init")
        print(f"    HTTP {init.status_code}  cookie 数 {len(client.cookies)}")
        print()

        for offset in (1, 3, 7, 13, 14, 15, 20):
            train_date = (today + timedelta(days=offset)).isoformat()
            print(f"  ── {train_date}（T+{offset}）──")
            for url in ENDPOINTS[:2]:
                try_query(client, url, train_date)
            print()


if __name__ == "__main__":
    main()
