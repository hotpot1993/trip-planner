"""用真实高德接口验证 POI 适配器与对齐逻辑。

单测用的是录下来的响应片段；这个脚本打真接口，验证三件事：

1. 适配器解析真实响应的结果与 `scripts/probe_m3_poi_parent.py` 直接看到的一致
   （`typecode`、`parent`、`biz_ext` 三处以前是空的）
2. `lushu/domain/align.py` 的判据在真实候选上跑得通，且**准确率量得出来**
3. 对不上的那些确实进了「待对齐」而不是被硬塞了一个结果

判定的口径与探测脚本一致：提及的期望答案由人工核对写在 `EXPECT` 里。
`expect=None` 表示「这条本来就该对不上」，用来验证不会硬凑。

用法：.venv\\Scripts\\python.exe scripts\\verify_align.py
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from _bootstrap import setup

setup()

import lushu.config as config  # noqa: E402
from lushu.adapters.poi import PoiSearchError, fetch_poi, search_pois  # noqa: E402
from lushu.adapters.poi_lineage import complete_lineage  # noqa: E402
from lushu.domain.align import AlignOutcome, Mention, align  # noqa: E402
from lushu.engine.amap import resolve_city  # noqa: E402

# 高德的个人 Key 有 QPS 限制，实测连续查询会返回
# `CUQPS_HAS_EXCEEDED_THE_LIMIT`。每次查询之间停一下，
# 顺便也符合「对别人的接口客气一点」。
QUERY_INTERVAL_SECONDS = 1.2


@dataclass(frozen=True)
class Case:
    """一条待对齐的提及，附人工核对过的期望结果。"""

    name: str
    city: str
    # 期望的本体 POI 名，None 表示「这条本来就不该对上」；
    # 元组表示高德对这个词有多条同名记录，命中任何一条都对（见钟楼那条）
    expect: str | tuple[str, ...] | None
    note: str = ""


CASES = [
    # 全称：应当直接命中
    Case("故宫博物院", "北京", "故宫博物院"),
    Case("陕西历史博物馆", "西安", "陕西历史博物馆"),
    Case("西安城墙", "西安", "西安城墙"),
    # 简称与俗称：靠高德的别名能力
    Case("故宫", "北京", "故宫博物院"),
    Case("陕历博", "西安", "陕西历史博物馆"),
    Case("兵马俑", "西安", "秦始皇帝陵博物院", "俗称，且本体是博物院"),
    # 子点：必须归并到本体（ADR-0009）
    Case("午门", "北京", "故宫博物院", "子点，要沿 parent 归并"),
    Case("神武门", "北京", "故宫博物院", "子点，要沿 parent 归并"),
    Case("太和殿", "北京", "故宫博物院", "子点，要沿 parent 归并"),
    Case("珍宝馆", "北京", "故宫博物院", "子点，要沿 parent 归并"),
    # 通用地标：高德对这个词有两条同名记录，命中任何一条都对。
    # `钟楼` 与 `西安钟楼` 指的是同一处地方，一条是热点地名、
    # 一条是风景名胜，高德自己也没有合并它们。
    Case("钟楼", "西安", ("钟楼", "西安钟楼"), "高德有两条同名记录"),
    Case("大唐不夜城", "西安", "大唐不夜城"),
    Case("回民街", "西安", "回民街"),
    # 正常景点名
    Case("景山公园", "北京", "景山公园"),
    # 本来就对不上：这些不该硬塞一个结果
    Case("不倒翁小姐姐", "西安", None, "不是一个地点"),
    Case("大唐遗宝展", "西安", None, "是一个展览，未必有独立 POI"),
]


def main() -> int:
    if not config.amap_api_key():
        print("缺少 AMAP_API_KEY")
        return 1

    print("真实接口验证：POI 适配器 + 对齐逻辑")
    print()

    # 先把用到的城市解析出来（对齐需要 adcode 做跨城校验）
    adcodes: dict[str, str] = {}
    for city in sorted({case.city for case in CASES}):
        match = resolve_city(city)
        if match is None:
            print(f"  城市解析失败：{city}")
            return 1
        adcodes[city] = match.adcode
    print("  城市 adcode：" + "，".join(f"{k}={v}" for k, v in adcodes.items()))
    print()

    hit = miss = 0
    correct_reject = wrong_accept = 0
    field_filled = {"typecode": 0, "parent": 0, "rating": 0, "open_time": 0}
    total_candidates = 0
    total_ancestors = 0

    for index, case in enumerate(CASES):
        if index:
            time.sleep(QUERY_INTERVAL_SECONDS)
        try:
            result = search_pois(case.name, city=case.city, api_key=config.amap_api_key())
            # 候选集之外的祖先要补进来，否则「本体还是子点」判断不出来：
            # 实测搜「兵马俑」的十条结果里有六条属于同一个本体，
            # 而本体自己可能排在最后。
            roots = complete_lineage(
                result.candidates,
                fetch_ancestor=lambda poi_id: fetch_poi(poi_id, api_key=config.amap_api_key()),
            )
        except PoiSearchError as exc:
            print(f"  [{case.city}] “{case.name}” → 查询失败：{exc}")
            miss += 1
            continue

        total_ancestors += len(roots.fetched)

        for poi in result.candidates:
            total_candidates += 1
            field_filled["typecode"] += 1 if poi.typecode else 0
            field_filled["parent"] += 1 if poi.parent_id else 0
            field_filled["rating"] += 1 if poi.rating is not None else 0
            field_filled["open_time"] += 1 if poi.open_time else 0

        verdict = align(
            Mention(name=case.name, city_name=case.city, city_adcode=adcodes[case.city]),
            result.candidates,
            lineage=roots,
        )

        if case.expect is None:
            # 期望是对不上。硬塞一个结果就是错。
            if verdict.outcome is AlignOutcome.ALIGNED:
                wrong_accept += 1
                mark = "✗"
                detail = f"不该对齐却对齐到了「{verdict.resolved.name if verdict.resolved else '?'}」"
            else:
                correct_reject += 1
                mark = "✓"
                detail = f"正确地判为 {verdict.outcome.value}"
        elif verdict.outcome is AlignOutcome.ALIGNED and verdict.resolved is not None:
            # 命中的两个层次都算对：
            #   `matched` 是名称真正命中的候选（可能是子点）
            #   `resolved` 是最终挂结论的本体
            # 搜「午门」时 matched 是「故宫博物院-午门」而 resolved 是
            # 「故宫博物院」——后者才是答案，但前者对得上也说明搜对了地方。
            matched_names = {
                verdict.matched.name if verdict.matched else "",
                verdict.resolved.name,
            }
            expected = case.expect
            accepted = (
                any(name in matched_names for name in expected)
                if isinstance(expected, tuple)
                else expected in matched_names
            )
            if accepted:
                hit += 1
                mark = "✓"
                detail = verdict.resolved.name
                if verdict.collapsed_from_sub and verdict.matched is not None:
                    detail += f"（由「{verdict.matched.name}」归并）"
            else:
                miss += 1
                mark = "✗"
                detail = f"对到了「{verdict.resolved.name}」，期望「{expected}」"
        else:
            miss += 1
            mark = "✗"
            detail = f"{verdict.outcome.value}：{verdict.reason}"

        note = f"  ← {case.note}" if case.note else ""
        print(f"  {mark} [{case.city}] “{case.name}” → {detail}{note}")

    print()
    print("=" * 72)
    judged = hit + miss
    print(f"  应当对齐的 {judged} 条：命中 {hit}，未命中 {miss}"
          + (f"，准确率 {hit / judged:.0%}" if judged else ""))
    print(f"  本来对不上的 {correct_reject + wrong_accept} 条："
          f"正确拒绝 {correct_reject}，错误接受 {wrong_accept}")
    if total_candidates:
        print(f"  候选 {total_candidates} 条，字段填充率：" + "，".join(
            f"{name} {count / total_candidates:.0%}"
            for name, count in field_filled.items()
        ))
    print(f"  额外查了 {total_ancestors} 个祖先节点（补齐候选集之外的层级）")
    print()
    print("  字段填充率是这次验证的重点：这三列在 M2 落库时全空（typecode 与")
    print("  raw_json 为 NULL），适配器能不能补上决定了 M3 做不做得下去。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
