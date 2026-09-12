"""探测 12306 的票价接口。

余票响应里的 [30]/[31]/[32] 是余票数量（「有」或数字），不是价格。
真价要单独问 /otn/leftTicketPrice/queryAllPublicPrice。

这个接口的参数比余票查询多（需要 train_no 与上下车站序号），先确认它到底
能不能用——拿不到真价就按设计降级为估价并标注参考价。
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
}

PRICE_URL = "https://kyfw.12306.cn/otn/leftTicketPrice/queryAllPublicPrice"


def main() -> None:
    train_date = (date.today() + timedelta(days=7)).isoformat()
    ticket_params = {
        "leftTicketDTO.train_date": train_date,
        "leftTicketDTO.from_station": "NKH",
        "leftTicketDTO.to_station": "EAY",
        "purpose_codes": "ADULT",
    }

    with httpx.Client(timeout=25, headers=HEADERS, follow_redirects=True) as client:
        client.get("https://kyfw.12306.cn/otn/leftTicket/init")
        data = client.get("https://kyfw.12306.cn/otn/leftTicket/queryG", params=ticket_params).json()

        result = (data.get("data") or {}).get("result") or []
        if not result:
            print("余票查询没有结果，无法继续")
            return

        parts = result[0].split("|")
        train_no = parts[2]          # 5l000G1970A3
        station_train_code = parts[3]  # G1970
        from_no = parts[16]          # 上车站序号
        to_no = parts[17]            # 下车站序号
        seat_types = parts[35]       # 座位类型串，如 9MOO

        print("拿到的参数：")
        print(f"  train_no           = {train_no}")
        print(f"  station_train_code = {station_train_code}")
        print(f"  from_station_no    = {from_no}")
        print(f"  to_station_no      = {to_no}")
        print(f"  seat_types         = {seat_types}")
        print(f"  train_date         = {train_date}")
        print()

        attempts: list[tuple[str, dict]] = [
            (
                "完整参数",
                {
                    "train_no": train_no,
                    "from_station_no": from_no,
                    "to_station_no": to_no,
                    "seat_types": seat_types,
                    "train_date": train_date,
                },
            ),
            (
                "带 station_train_code",
                {
                    "train_no": train_no,
                    "from_station_no": from_no,
                    "to_station_no": to_no,
                    "seat_types": seat_types,
                    "train_date": train_date,
                    "station_train_code": station_train_code,
                },
            ),
        ]

        for label, params in attempts:
            print(f"── {label} ──")
            try:
                response = client.get(PRICE_URL, params=params)
            except Exception as exc:
                print(f"  请求失败 {type(exc).__name__}: {exc}")
                print()
                continue

            ctype = response.headers.get("content-type", "").split(";")[0]
            print(f"  HTTP {response.status_code}  {ctype}  长度 {len(response.text)}")

            text = response.text.lstrip()
            if not text.startswith("{"):
                print(f"  不是 JSON，最终路径 {response.url.path}")
                print(f"  片段：{response.text[:160]}")
                print()
                continue

            try:
                body = response.json()
            except json.JSONDecodeError:
                print("  声称 JSON 但解析失败")
                print()
                continue

            print(f"  httpstatus={body.get('httpstatus')} status={body.get('status')}")
            payload = body.get("data")
            if payload is None:
                print(f"  data 为空，messages={body.get('messages')}")
                print()
                continue

            if isinstance(payload, dict):
                print(f"  data 键: {sorted(payload.keys())}")
                for key, value in payload.items():
                    if isinstance(value, (list, dict)):
                        print(f"    {key}: {type(value).__name__} 长度 {len(value)}")
                    else:
                        print(f"    {key}: {value}")
                prices = payload.get("prices") or payload.get("price")
                if prices:
                    print(f"  票价结构示例: {json.dumps(prices[0], ensure_ascii=False)[:300]}")
            else:
                print(f"  data 类型 {type(payload).__name__}: {str(payload)[:300]}")
            print()


if __name__ == "__main__":
    main()
