"""把 12306 票价接口的各种调用方式试一遍，确认它到底能不能用。

结论会决定适配器的形状：拿得到真价就存真价，拿不到就按设计降级为估价并标注
参考价——但必须先确认，不能凭猜测写。
"""

from __future__ import annotations

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
}


def attempt(client: httpx.Client, label: str, url: str, params: dict) -> dict | None:
    try:
        response = client.get(url, params=params)
    except Exception as exc:
        print(f"  {label:<34} 请求失败 {type(exc).__name__}: {exc}")
        return None

    text = response.text.lstrip()
    if not text.startswith("{"):
        print(f"  {label:<34} 非 JSON（最终路径 {response.url.path}）")
        return None

    body = response.json()
    payload = body.get("data")
    ok = body.get("status") is True or bool(payload)
    messages = body.get("messages")
    detail = ""
    if isinstance(payload, dict):
        keys = sorted(payload.keys())
        detail = f"data 键={keys}"
    elif payload:
        detail = f"data={str(payload)[:80]}"
    print(f"  {label:<34} status={body.get('status')} {detail}{'' if ok else '  ' + str(messages)}")
    return payload if ok else None


def main() -> None:
    today = date.today()
    dashed = (today + timedelta(days=7)).isoformat()
    compact = dashed.replace("-", "")
    dashed_1 = (today + timedelta(days=1)).isoformat()

    with httpx.Client(timeout=25, headers=HEADERS, follow_redirects=True) as client:
        client.get("https://kyfw.12306.cn/otn/leftTicket/init")

        tickets = client.get(
            "https://kyfw.12306.cn/otn/leftTicket/queryG",
            params={
                "leftTicketDTO.train_date": dashed,
                "leftTicketDTO.from_station": "NKH",
                "leftTicketDTO.to_station": "EAY",
                "purpose_codes": "ADULT",
            },
        ).json()
        result = (tickets.get("data") or {}).get("result") or []
        parts = result[0].split("|")
        base = {
            "train_no": parts[2],
            "from_station_no": parts[16],
            "to_station_no": parts[17],
            "seat_types": parts[35],
        }
        print(f"基准参数：{base}")
        print()

        price_urls = (
            "https://kyfw.12306.cn/otn/leftTicketPrice/queryAllPublicPrice",
            "https://kyfw.12306.cn/otn/leftTicketPrice/query",
        )

        print("── 日期格式 ──")
        for label, day in (("train_date=YYYY-MM-DD", dashed), ("train_date=YYYYMMDD", compact)):
            for url in price_urls:
                got = attempt(
                    client,
                    f"{label} · {url.rsplit('/', 1)[-1]}",
                    url,
                    {**base, "train_date": day},
                )
                if got:
                    print(f"    ✓ 成功：{str(got)[:200]}")
                    return

        print()
        print("── 补上车站电报码 ──")
        for url in price_urls:
            attempt(
                client,
                url.rsplit("/", 1)[-1],
                url,
                {**base, "train_date": compact, "from_station": "NKH", "to_station": "EAY"},
            )

        print()
        print("── 改用最早可售日期（T+1）──")
        for url in price_urls:
            attempt(client, url.rsplit("/", 1)[-1], url, {**base, "train_date": dashed_1})

        print()
        print("── 经停站接口（有时带票价）──")
        try:
            stops = client.get(
                "https://kyfw.12306.cn/otn/czxx/queryByTrainNo",
                params={
                    "train_no": parts[2],
                    "from_station_telecode": "NKH",
                    "to_station_telecode": "EAY",
                    "depart_date": dashed,
                },
            )
            text = stops.text.lstrip()
            kind = "JSON" if text.startswith("{") else stops.headers.get("content-type")
            print(f"  HTTP {stops.status_code}  {kind}")
            if text.startswith("{"):
                body = stops.json()
                payload = body.get("data") or {}
                items = payload.get("data") if isinstance(payload, dict) else payload
                if items:
                    print(f"  经停 {len(items)} 站，首站字段: {sorted(items[0].keys())}")
                    print(f"  首站: {items[0]}")
        except Exception as exc:
            print(f"  失败 {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
