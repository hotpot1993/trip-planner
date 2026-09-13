"""查「同一个地方在库里是几行实体」。

这是本项目反复撞上的一类问题：M1/M2 的行程天项用引擎回填的 id，
M3 的自有适配器又按高德搜了一遍，M4 改了归并规则之后旧结论还留在旧实体上。
`ls align merge-pois` 只抓**同名**的两行，而「中山陵」与「中山陵景区」
名字不同、确实是同一个地方——这个脚本把这类也摆出来。

用法：
    python scripts/probe_entity_split.py                # 全部行程涉及的地方
    python scripts/probe_entity_split.py trip_xxx       # 只看某份行程
"""

from __future__ import annotations

import sys

from _bootstrap import setup

setup()

from lushu.store import connect  # noqa: E402


def main(argv: list[str]) -> int:
    conn = connect()
    try:
        where = "AND d.trip_id = ?" if argv else ""
        params = (argv[0],) if argv else ()
        rows = conn.execute(
            "SELECT DISTINCT di.poi_id AS poi_id, COALESCE(di.title, p.name) AS title, "
            "  d.trip_id AS trip_id "
            "FROM day_item di JOIN day d ON d.id = di.day_id "
            "LEFT JOIN poi p ON p.amap_poi_id = di.poi_id "
            f"WHERE di.kind = 'poi' {where}",
            params,
        ).fetchall()

        print(f"行程涉及 {len(rows)} 个天项，逐个看它们的实体与同名近邻：\n")
        for row in rows:
            poi_id = row["poi_id"]
            if not poi_id:
                print(f"  （未对齐）{row['title']}")
                continue
            poi = conn.execute(
                "SELECT * FROM poi WHERE amap_poi_id = ?", (poi_id,)
            ).fetchone()
            claims = conn.execute(
                "SELECT COUNT(*) AS n FROM claim WHERE poi_id = ?", (poi_id,)
            ).fetchone()["n"]
            print(f"  {row['title'] or poi['name']}")
            print(f"    天项指向   {poi_id}  {poi['name']}  结论 {claims} 条")

            # 名字里互相包含的近邻：中山陵 / 中山陵景区 这类
            siblings = conn.execute(
                "SELECT amap_poi_id, name FROM poi "
                "WHERE amap_poi_id != ? AND (name LIKE ? OR ? LIKE '%' || name || '%')",
                (poi_id, f"%{poi['name']}%", poi["name"]),
            ).fetchall()
            for sibling in siblings:
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM claim WHERE poi_id = ?",
                    (sibling["amap_poi_id"],),
                ).fetchone()["n"]
                mark = "  ← 结论在这里" if count else ""
                print(
                    f"    名字相近   {sibling['amap_poi_id']}  {sibling['name']}  "
                    f"结论 {count} 条{mark}"
                )
            print()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
