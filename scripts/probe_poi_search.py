"""看高德对某个名称返回了什么。

种子库里的 `name` 是给人看的官方名，而**高德的名称索引是另一回事**：
「湖南博物院」可能在高德那边叫「湖南省博物馆」，「苏州博物馆（本馆）」
用的是半角括号。名称对不上时对齐只能靠字符重合度，而那道门槛是故意设高的
（宁可让人来判，也不自动接受一个「看起来挺像」的候选）。

这个脚本把高德给的原样打出来，好判断是「种子里的写法要改」还是
「高德确实没有这一处」。

用法：
    python scripts/probe_poi_search.py 湖南博物院 长沙
    python scripts/probe_poi_search.py 敦煌            # 只查城市
"""

from __future__ import annotations

import sys

from _bootstrap import setup

setup()

from lushu.adapters.poi import search_pois  # noqa: E402
from lushu.domain.align import name_score  # noqa: E402
from lushu.domain.poi import classify_poi  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    name = argv[0]
    city = argv[1] if len(argv) > 1 else None

    if city is None:
        from lushu.engine import resolve_city

        match = resolve_city(name)
        if match is None:
            print(f"高德查不到城市「{name}」")
            return 1
        print(f"{match.name}  adcode={match.adcode}  {match.lat_gcj02},{match.lng_gcj02}")
        return 0

    result = search_pois(name, city=city, limit=10)
    print(f"搜「{name}」限定「{city}」，返回 {len(result.candidates)} 条")
    for index, poi in enumerate(result.candidates, 1):
        score = name_score(name, poi.name)
        kind = classify_poi(poi.typecode, poi.type_name, poi.name).value
        parent = poi.parent_id or "（本体）"
        print(
            f"  {index:>2}. {score:.2f}  {poi.name}  [{kind}]  {poi.typecode}\n"
            f"      {poi.poi_id}  adcode={poi.adcode}  城市={poi.city_name}  父={parent}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
