"""确认两件事：子 POI 的 parent 字段能不能认亲，以及 biz_ext 里到底有什么。

第一件：对齐探测里看到 `故宫博物院-午门` 与 `故宫博物院` 并列返回。如果能靠
`parent` 字段认出前者属于后者，就可以把「只有午门能进」这类结论挂到正确的
层级上；如果不能，就只能在名称上做启发式。

第二件：`biz_ext` 这个嵌套对象里有 rating / open_time / cost。我们的 poi 表把
评分与开放时间当硬事实存（ADR-0001），如果取值路径错了，表里就是长期空的，
而且不会报错——这种错最难发现。顺便把已有数据翻出来看看是不是真的空着。

用法：.venv\\Scripts\\python.exe scripts\\probe_m3_poi_parent.py
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from pathlib import Path

from _bootstrap import setup

setup()

import lushu.config as config  # noqa: E402

TEXT_SEARCH_URL = "https://restapi.amap.com/v3/place/text"


def search(keywords: str, city: str, key: str, limit: int = 10) -> list[dict]:
    from third_party.floattrip.core.http import http_get_json

    params = {
        "key": key,
        "keywords": keywords,
        "city": city,
        "citylimit": "true",
        "offset": str(limit),
        "page": "1",
        "extensions": "all",
        "output": "json",
    }
    data = http_get_json(f"{TEXT_SEARCH_URL}?{urllib.parse.urlencode(params)}")
    if data.get("status") != "1":
        print(f"    查询失败：{data.get('info')}")
        return []
    return [p for p in (data.get("pois") or []) if isinstance(p, dict)]


def cell(value: object) -> str:
    if isinstance(value, list):
        value = "".join(str(v) for v in value)
    return str(value or "").strip() or "-"


def main() -> int:
    key = config.amap_api_key()
    if not key:
        print("缺少 AMAP_API_KEY")
        return 1

    print("一、parent 字段能不能认亲")
    print()
    for keywords, city in (("故宫", "北京"), ("陕西历史博物馆", "西安"), ("兵马俑", "西安")):
        pois = search(keywords, city, key)
        print(f"  [{city}] “{keywords}” 返回 {len(pois)} 条")
        print(f"    {'id':<14}{'parent':<14}{'name':<28}{'typecode':<10}{'评分':<7}{'开放时间'}")
        for poi in pois:
            biz = poi.get("biz_ext") or {}
            if not isinstance(biz, dict):
                biz = {}
            print(f"    {cell(poi.get('id')):<14}{cell(poi.get('parent')):<14}"
                  f"{cell(poi.get('name'))[:26]:<28}{cell(poi.get('typecode')):<10}"
                  f"{cell(biz.get('rating')):<7}{cell(biz.get('open_time'))[:28]}")
        print()

    print("二、biz_ext 里有什么（取一条景点看全）")
    pois = search("陕西历史博物馆", "西安", key, limit=1)
    if pois:
        print(f"  {cell(pois[0].get('name'))} 的 biz_ext：{pois[0].get('biz_ext')}")
        print(f"  顶层是否有 rating / open_time："
              f"{'rating' in pois[0]} / {'open_time' in pois[0]}")
    print()

    print("三、库里已有的 poi 记录，评分与开放时间是不是空的")
    db = Path("data/lushu.db")
    if not db.exists():
        print("  数据库不存在")
        return 0
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) AS n FROM poi").fetchone()["n"]
    rated = conn.execute("SELECT COUNT(*) AS n FROM poi WHERE rating IS NOT NULL").fetchone()["n"]
    timed = conn.execute("SELECT COUNT(*) AS n FROM poi WHERE open_time IS NOT NULL").fetchone()["n"]
    print(f"  poi 共 {total} 条，rating 非空 {rated} 条，open_time 非空 {timed} 条")
    for row in conn.execute(
        "SELECT name, type, typecode, tel, address, raw_json FROM poi ORDER BY name LIMIT 8"
    ):
        raw = row["raw_json"] or ""
        has_biz = "biz_ext" in raw
        print(f"    {row['name'][:20]:<22} typecode={row['typecode'] or '-':<8} "
              f"电话={'有' if row['tel'] else '无'} raw含biz_ext={'是' if has_biz else '否'}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
