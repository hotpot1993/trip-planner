"""让候选池驱动引擎的景点搜索。

设计 5.1：规划某座城市时，**候选池以攻略知识库为主、高德搜索补全**（Q27）。
候选池是「网友真的写过的地方」（有结论挂着的 POI），高德负责补齐坐标之外的
那些字段与缺口。

引擎的 `attraction_search_node` 无条件调用 `fetch_city_spots_async`（纯高德
搜索），候选池进不去。要改它就得再动一次 vendored 代码（ADR-0005 记着两处
additive edit）。而 `planning/nodes.py` 里那一句是**模块级导入**：

    from ..planning.helpers import fetch_city_spots_async

所以可以在我们这一侧**在运行时替换掉它**，`third_party/` 一个字节不动。
这一层是唯一能碰 vendored 代码的地方（`tests/test_architecture_boundaries.py`
把 ADR-0005 钉成了会失败的测试），所以替换动作住在 engine 层，而「池子里
有哪些地方」由 services 层以领域对象（`CandidatePoi`）喂进来——
engine 只认 domain，不认识我们的表。

替换之后的行为，就是设计那句话的字面意思：

1. **池子在前**。引擎下游把这份清单交给 LLM 选点，`max_spots` 是硬上限
   （默认 30），排在后面的会被截掉。所以「池子在前」＝网友推荐过的地方
   先被看到、也优先被选中。
2. **高德补全**。池子不够 `max_spots` 时，用原函数补后面的位置；
   同名的不重复进来（与引擎自己的去重口径一致）。
3. **池子没覆盖的城市原样走原路**——游客去一座没人写过攻略的城市时，
   行为与从前完全一样，不会因为接了这个而少掉什么。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from lushu.domain.poi import CandidatePoi

# 池子的提供者：给一个城市名，返回那座城市候选池里的地方。
# 返回领域对象而不是引擎要的 dict：翻译是这一层的事。
SpotProvider = Callable[[str], list[CandidatePoi]]

# 替换前的原函数。只用它来恢复，以及补全池子没覆盖的部分。
_state: dict[str, Any] = {"original": None}


def to_spot(poi: CandidatePoi) -> dict[str, Any] | None:
    """把候选池里的一个地方转成引擎要的 spot。

    **优先用引擎自己的转换**：库里存着高德的原始响应（`raw_json`，91/100 行
    都有），交给 `poi_to_spot` 转出来的字段与高德搜索结果完全同构——
    自己再拼一份，就等于给同一个东西定两套字段约定，迟早分岔。

    没有原始响应时才用列里的字段拼（少 `adname` 与 `cost` 两个装饰性字段，
    坐标、评分、开放时间、地址都在）。
    """
    from third_party.floattrip.providers.amap.poi import poi_to_spot

    if poi.raw_json:
        try:
            raw = json.loads(poi.raw_json)
        except ValueError:
            raw = None
        if isinstance(raw, dict):
            spot = poi_to_spot(raw)
            if spot is not None:
                return spot

    if poi.lat_gcj02 is None or poi.lng_gcj02 is None:
        return None
    return {
        "amap_poi_id": poi.poi_id,
        "name": poi.name,
        "rating": poi.rating,
        "open_time": poi.open_time,
        "location": {"lng": poi.lng_gcj02, "lat": poi.lat_gcj02},
        "photo": poi.photo_url,
        "adname": "",
        "address": poi.address or "",
        "tel": poi.tel,
        "cost": None,
    }


def install(provider: SpotProvider | None, *, pool_only_from: int = 1) -> None:
    """装上（或卸下）候选池优先的景点搜索。

    每次规划都调一次，`provider=None` 表示恢复原样——**状态由调用方每次显式
    设定**，不在两次规划之间残留。否则「上一次带池子、这一次不带」这种情形
    会拿到上一次的行为，而它没有任何地方看得出来。

    `pool_only_from` 是**封闭世界的门槛**：池子里有这么多地方，就只用池子。
    它由调用方传进来（`services/candidate_pool.THIN_COVERAGE`），因为这个数
    说的是「这座城市算不算有攻略数据」——那是候选池那边的判断，
    不该在引擎里再写一份。默认 1 的意思是「只要有池子就用池子」；
    卸下时（`provider=None`）这个参数不参与。

    换的只有**一个函数**：那个节点。它自己调池子、自己调高德、自己决定
    谁受评分门禁管——两件事都在一处，就不会出现「搜索换了、门禁没换」
    这种一半的状态。
    """
    from third_party.floattrip.planning import graph, nodes

    if _state["original"] is None:
        _state["original"] = nodes.fetch_city_spots_async
        _state["original_node"] = nodes.attraction_search_node
        _state["original_graph_node"] = graph.attraction_search_node

    node = (
        _pool_exempt_node(provider, pool_only_from=pool_only_from)
        if provider is not None
        else _state["original_node"]
    )
    nodes.attraction_search_node = node
    # `graph.py` 在模块级 `from ...nodes import attraction_search_node`，
    # 于是它自己持有一份引用——图是在函数里现搭的，所以这一份也得换。
    graph.attraction_search_node = node


# 引擎那个节点做的事（抓清单 → 滤评分 → 记日志），改了三处：
#
# 1. **清单里池子在前**。设计 5.1：「候选池以攻略知识库为主、高德搜索补全」。
# 2. **池子条目不受评分门禁管**。那道门禁的意图是滤掉高德搜出来的噪声
#    （评分缺失的往往是个广场、停车场、上车点），而池子里的地方不是
#    「高德搜出来的」——它们是网友真的写过的地方，评分缺失只是高德没给
#    （实测夫子庙就没有）。拿滤噪声的规则去滤已经认定过的条目，是判据用错了对象。
# 3. **池子够用时就只用池子**。设计 5.1 的另一句：「排程不得引入候选池之外的
#    景点」。两句要一起读：高德「补全」是在知识库还没覆盖这座城市的时候
#    （同节原文：「知识库尚未覆盖的城市仍可直接搜高德」）。池子够用还把高德
#    那一片塞进去，后果实测过——LLM 从里面挑了「不老村」「水墨大埝旅游区」，
#    两个都在四十公里外的郊区。
#
# 为什么连节点一起换、而不是只换搜索函数：门禁在节点里，而 **`graph.py` 对
# 节点另有一份模块级绑定**。只换搜索函数的话，池子条目进得了清单、过不了门禁
# ——真机跑出来就是这样（夫子庙被丢掉，排程选了高德的同名变体顶上，而那一个
# 身上没有任何网友结论）。
#
# 代价是这个节点的几行逻辑在我们这边有一份副本。上游固定在 ADR-0005 记的
# 那个提交上，不会自主变化；真变化了，`tests/test_pool_search.py` 会红着提醒。
def _pool_exempt_node(provider: SpotProvider, *, pool_only_from: int):
    """与引擎那个节点同一套逻辑：池子在前、不受评分门禁管、够用就封闭世界。"""

    async def attraction_search_node(state):
        from third_party.floattrip.planning.helpers import amap_key, filter_by_rating

        city = state.destination or ""

        # 池子自己转，不走搜索函数：这样「哪些是池子里的」不需要靠名字去猜，
        # 也就不必把 provider 调两遍。
        from_pool: list[dict[str, Any]] = []
        seen: set[str] = set()
        for poi in provider(city):
            if len(from_pool) >= state.max_spots:
                break
            spot = to_spot(poi)
            name = (spot or {}).get("name") or ""
            if spot is None or not name or name in seen:
                continue
            seen.add(name)
            from_pool.append(spot)

        # 池子够用：**就到这里**。高德那一片不进清单，LLM 也就挑不到池子之外的地方。
        if len(from_pool) >= pool_only_from:
            note = (
                f"景点搜索：候选池 {len(from_pool)} 个，已达封闭世界门槛"
                f"（{pool_only_from} 个），不引入池子之外的景点"
            )
            return {"pois": from_pool, "history": state.history + [note]}

        found = await _state["original"](city, amap_key(), max_spots=state.max_spots)
        # 池子里的地方大多也搜得到：不同来源的同名条目只留池子那一份
        fresh = [spot for spot in found if (spot.get("name") or "") not in seen]
        kept, dropped = filter_by_rating(fresh, state.min_rating)

        spots = (from_pool + kept)[: state.max_spots]
        note = (
            f"景点搜索：候选池 {len(from_pool)} 个（不受评分门禁管）"
            f" + 高德 {len(fresh)} 个（rating≥{state.min_rating} 保留 {len(kept)}，"
            f"滤掉 {len(dropped)}）= {len(spots)} 个"
            f"——池子不足 {pool_only_from} 个，按设计用高德补全"
        )
        return {"pois": spots, "history": state.history + [note]}

    return attraction_search_node
