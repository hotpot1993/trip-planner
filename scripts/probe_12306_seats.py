"""验证普速列车的席别下标。

高铁（G）车的 26=无座、30=二等、31=一等、32=商务 已经验证过。
普速车的硬座/硬卧/软卧下标没有验证过，而按猜测的下标写解析器正是最容易
静默出错的地方——挪一位就会把「硬卧」当成「二等座」报给用户。
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

# 常见席别与其「应该有值」的车次类型
EXPECTED = {
    "G": ("商务座", "一等座", "二等座"),
    "D": ("一等座", "二等座"),
    "K": ("硬座", "硬卧", "软卧"),
    "T": ("硬座", "硬卧", "软卧"),
    "Z": ("硬座", "硬卧", "软卧"),
}

PAIRS = (
    ("南京", "西安", "NKH", "EAY"),
    ("北京", "上海", "VNP", "AOH"),
)


def main() -> None:
    train_date = (date.today() + timedelta(days=5)).isoformat()
    collected: dict[str, list[str]] = {}

    with httpx.Client(timeout=25, headers=HEADERS, follow_redirects=True) as client:
        client.get("https://kyfw.12306.cn/otn/leftTicket/init")

        for _, _, frm, to in PAIRS:
            data = client.get(
                "https://kyfw.12306.cn/otn/leftTicket/queryG",
                params={
                    "leftTicketDTO.train_date": train_date,
                    "leftTicketDTO.from_station": frm,
                    "leftTicketDTO.to_station": to,
                    "purpose_codes": "ADULT",
                },
            ).json()
            for raw in (data.get("data") or {}).get("result") or []:
                parts = raw.split("|")
                prefix = parts[3][:1]
                if prefix in EXPECTED and prefix not in collected and len(parts) >= 33:
                    collected[prefix] = parts

    print(f"采到的车次类型: {sorted(collected)}")
    print()

    for prefix in sorted(collected):
        parts = collected[prefix]
        print(f"── {parts[3]}（{prefix} 字头，{len(parts)} 字段）──")
        print(f"  期望有值的席别: {EXPECTED[prefix]}")
        print("  第 21 到 34 号字段：")
        for index in range(21, min(35, len(parts))):
            mark = "  ←" if parts[index] else ""
            print(f"    [{index:>2}] {parts[index] or '（空）'}{mark}")
        print()

    print("── 交叉判断 ──")
    print("  同一下标在不同车次类型下应当对应同一种席别。")
    print("  若某下标只在普速车有值、在高铁车为空，它就是普速席别。")


if __name__ == "__main__":
    main()
