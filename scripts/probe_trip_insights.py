"""核对行程上挂到的软经验，与覆盖检查相互印证。

用法：python scripts/probe_trip_insights.py <trip_id>
"""

from __future__ import annotations

import sys

from _bootstrap import setup

setup()

from lushu.services import coverage, trip_insights  # noqa: E402


def main(argv: list[str]) -> int:
    trip_id = argv[0] if argv else "trip_5f8dec330aa1"
    data = trip_insights.insights_for_trip(trip_id)
    cover = coverage.coverage_for_trip(trip_id)

    print(f"覆盖：{cover.total} 个景点，网友推荐过 {cover.recommended} 个"
          f"（{cover.ratio:.0%}），没对上 {cover.unresolved} 个")
    print(f"软经验：{data.covered_items} 个景点挂到了结论，共 {data.total_claims} 条\n")

    for item in cover.items:
        insights = data.for_poi(item.poi_id)
        mark = "有推荐" if item.recommended else "只地图"
        detail = (
            f"打卡 {len(insights.highlights)} / 避坑 {len(insights.avoids)}"
            if insights
            else "——"
        )
        print(f"  [{mark}] {item.title:<24}{detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
