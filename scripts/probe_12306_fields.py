"""把 12306 余票响应的字段结构摸清楚。

按固定下标解析是最容易静默出错的做法——上游挪一个字段，解析器就会把历时
当成票价读出来。这里把完整的字段列表连下标一起打出来，解析器照着写。
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


def main() -> None:
    train_date = (date.today() + timedelta(days=7)).isoformat()
    params = {
        "leftTicketDTO.train_date": train_date,
        "leftTicketDTO.from_station": "NKH",
        "leftTicketDTO.to_station": "EAY",
        "purpose_codes": "ADULT",
    }

    with httpx.Client(timeout=25, headers=HEADERS, follow_redirects=True) as client:
        client.get("https://kyfw.12306.cn/otn/leftTicket/init")
        data = client.get("https://kyfw.12306.cn/otn/leftTicket/queryG", params=params).json()

    payload = data.get("data") or {}
    print("data 的键:", sorted(payload.keys()))
    print()

    station_map = payload.get("map") or {}
    print(f"车站电报码映射 {len(station_map)} 条，示例:", list(station_map.items())[:4])
    print()

    result = payload.get("result") or []
    print(f"车次 {len(result)} 条")
    print()

    # 挑一个高铁与一个普速，把字段全打出来
    picked: list[str] = []
    for raw in result:
        parts = raw.split("|")
        code = parts[3]
        if code.startswith("G") and not any(c.startswith("G") for c in picked):
            picked.append(raw)
        elif code.startswith(("K", "T", "Z")) and len(picked) == 1:
            picked.append(raw)
        if len(picked) >= 2:
            break
    if not picked:
        picked = result[:1]

    for raw in picked:
        parts = raw.split("|")
        print(f"── {parts[3]}（{len(parts)} 个字段）──")
        for index, value in enumerate(parts):
            shown = value if value else "（空）"
            print(f"  [{index:>2}] {shown}")
        print()

    # 票价是独立接口，确认它是否还需要单独查
    print("── 试着从余票响应里直接取价（若能取到就不必再查一次）──")
    parts = picked[0].split("|")
    for label, index in (("二等座", 30), ("一等座", 31), ("商务座", 32), ("无座", 26)):
        if index < len(parts):
            print(f"  [{index}] {label}: {parts[index] or '（空）'}")


if __name__ == "__main__":
    main()
