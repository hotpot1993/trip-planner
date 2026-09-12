"""判定 12306 的余票查询到底是按站还是按城市。

上一轮探测里，每一组站对都返回 30 个车次，这有两种可能：
  A. 12306 按城市返回，站代码只用来定位城市 —— 那么候选站排序与多站对查询
     都是多余的复杂度，应当删掉。
  B. 探测有 bug。

判据：比较不同站对返回的车次集合。相同则是 A，不同则是 B。
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from lushu.adapters import rail

PAIRS = (
    ("南京", "NJH", "西安", "XAY", "南京站 → 西安站"),
    ("南京南", "NKH", "西安北", "EAY", "南京南 → 西安北"),
    ("南京", "NJH", "西安北", "EAY", "南京站 → 西安北"),
    ("南京南", "NKH", "西安", "XAY", "南京南 → 西安站"),
)


def main() -> None:
    travel_date = date.today() + timedelta(days=5)
    client = httpx.Client(timeout=25, headers=rail._HEADERS, follow_redirects=True)
    try:
        client.get(rail.INIT_URL)
        sets: dict[str, set[str]] = {}
        detail: dict[str, list[tuple[str, str, str]]] = {}

        for _from_name, from_code, _to_name, to_code, label in PAIRS:
            params = {
                "leftTicketDTO.train_date": travel_date.isoformat(),
                "leftTicketDTO.from_station": from_code,
                "leftTicketDTO.to_station": to_code,
                "purpose_codes": "ADULT",
            }
            body = client.get(rail.TICKET_URL, params=params).json()
            payload = body.get("data") or {}
            names = payload.get("map") or {}
            rows = payload.get("result") or []

            codes: set[str] = set()
            triples: list[tuple[str, str, str]] = []
            for raw in rows:
                parts = raw.split("|")
                if len(parts) < 11:
                    continue
                codes.add(parts[3])
                triples.append(
                    (
                        parts[3],
                        names.get(parts[6].strip(), parts[6].strip()),
                        names.get(parts[7].strip(), parts[7].strip()),
                    )
                )
            sets[label] = codes
            detail[label] = triples
            print(f"{label:<26} {len(codes)} 个车次")
    finally:
        client.close()

    print()
    labels = list(sets)
    base = sets[labels[0]]
    print(f"以「{labels[0]}」为基准比较车次集合：")
    for label in labels[1:]:
        other = sets[label]
        same = base == other
        print(
            f"  {label:<26} 相同={same}  仅基准有={len(base - other)}  仅它{len(other - base)}"
        )

    print()
    if all(sets[labels[0]] == sets[label] for label in labels[1:]):
        print("结论 A：12306 按城市返回，站代码只用来定位城市。")
        print("      → 候选站排序与多站对查询都是多余的，应当简化。")
    else:
        print("结论 B：站对确实影响结果，候选站排序需要修（要保证覆盖高铁主站）。")

    print()
    print("实际返回的出发站分布（取第一组）：")
    counts: dict[str, int] = {}
    for _, from_station, _to_station in detail[labels[0]]:
        counts[from_station] = counts.get(from_station, 0) + 1
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {name}: {count} 个车次")


if __name__ == "__main__":
    main()
