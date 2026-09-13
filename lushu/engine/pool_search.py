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


def _pool_first(original, provider: SpotProvider):
    """把「池子优先、高德补全」包成原函数的样子。"""

    async def fetch_city_spots_async(
        city: str, api_key: str, *, max_spots: int = 30
    ) -> list[dict[str, Any]]:
        spots: list[dict[str, Any]] = []
        seen: set[str] = set()

        for poi in provider(city or ""):
            if len(spots) >= max_spots:
                break
            spot = to_spot(poi)
            if spot is None or not spot.get("name") or spot["name"] in seen:
                continue
            seen.add(spot["name"])
            spots.append(spot)

        if len(spots) < max_spots:
            # 池子不够才问高德。补进来的同样要过一遍去重——
            # 池子里的地方大多也是高德搜得到的，不去重就会一份清单里出现两遍。
            for spot in await original(city, api_key, max_spots=max_spots):
                if len(spots) >= max_spots:
                    break
                name = spot.get("name") or ""
                if not name or name in seen:
                    continue
                seen.add(name)
                spots.append(spot)

        return spots

    return fetch_city_spots_async


def install(provider: SpotProvider | None) -> None:
    """装上（或卸下）候选池优先的景点搜索。

    每次规划都调一次，`provider=None` 表示恢复原样——**状态由调用方每次显式
    设定**，不在两次规划之间残留。否则「上一次带池子、这一次不带」这种情形
    会拿到上一次的行为，而它没有任何地方看得出来。
    """
    from third_party.floattrip.planning import nodes

    if _state["original"] is None:
        _state["original"] = nodes.fetch_city_spots_async

    nodes.fetch_city_spots_async = (
        _pool_first(_state["original"], provider)
        if provider is not None
        else _state["original"]
    )
