"""高德 POI 搜索适配器。

引擎自带一个 POI 搜索（`third_party/floattrip/providers/amap/poi.py`），
但它的 `poi_to_spot()` 只保留规划要用的那几个字段，**原始响应在到达
`final_plan` 之前就被丢掉了**。后果实测过（docs/M3-PROBE.md 第四节）：
M2 落库的 14 条 POI 里 `typecode` 全是 NULL、`raw_json` 全是空的。

这两列恰恰卡住 M3：

- 没有 `typecode` 就没法把停车场、公交站、公厕从候选里分出去，
  也没法区分「值得去的地方」与「只是个地名」（`lushu/domain/align.py`）
- 没有 `parent` 就没法把「故宫博物院-午门」归并到「故宫博物院」（ADR-0009）

所以这里做一个自有适配器。**引擎的代码保持原样不动**——它是第三方代码，
改动越少越好（ADR-0005），我们自己的路走自己的适配器。

三条实测决定的行为（docs/M3-PROBE.md 第三节）：

1. **`city` 参数传城市名，不传 adcode。** 传 adcode 时高德按「行政区内」理解，
   搜「故宫」传 `110100` 返回 2 条、全部是廊坊的故宫文创店（大兴机场行政上属
   河北）；传「北京」返回 10 条且 `故宫博物院` 居首。城市校验放在取到结果
   之后，用 `adcode` 前四位做，两者分工不同。
2. **`extensions=all` 是必填的。** 只取 `base` 就没有 `biz_ext`，评分与开放
   时间会全空——而它们是硬事实（ADR-0001）。
3. **`biz_ext` 是嵌套对象。** 评分在 `biz_ext.rating`，开放时间优先取
   `biz_ext.opentime2`（陕历博实测只有这个字段有值，`open_time` 是空数组）。
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass

import httpx

from lushu import config
from lushu.domain.poi import CandidatePoi

TEXT_SEARCH_URL = "https://restapi.amap.com/v3/place/text"
DETAIL_URL = "https://restapi.amap.com/v3/place/detail"

# 城市范围的 POI 搜索。够覆盖「搜一个景点名，看前几名候选」这个用法。
DEFAULT_LIMIT = 10

# 高德单次最多 25 条。
MAX_LIMIT = 25


class PoiSearchError(RuntimeError):
    """高德 POI 搜索失败。"""


@dataclass(frozen=True)
class PoiSearchResult:
    """一次查询的完整结果，含原始响应。"""

    candidates: tuple[CandidatePoi, ...]
    query: str
    city: str
    raw_count: int

    def __bool__(self) -> bool:
        return bool(self.candidates)


def parse_poi(raw: dict) -> CandidatePoi | None:
    """把高德返回的一条 POI 转成领域对象。缺 id 或坐标时返回 None。

    坐标缺失不是可以凑合的：`poi.lat_gcj02` 有非空约束，而且没有坐标的景点
    在行程里排不出路线。高德确实会返回没有 `location` 的条目，所以这里要挡。
    """
    poi_id = _text(raw.get("id"))
    if not poi_id:
        return None

    lng, lat = _parse_location(raw.get("location"))
    if lng is None or lat is None:
        return None

    biz = raw.get("biz_ext")
    biz = biz if isinstance(biz, dict) else {}

    return CandidatePoi(
        poi_id=poi_id,
        name=_text(raw.get("name")) or poi_id,
        typecode=_text(raw.get("typecode")),
        type_name=_text(raw.get("type")),
        adcode=_text(raw.get("adcode")),
        city_name=_text(raw.get("cityname")),
        citycode=_text(raw.get("citycode")),
        address=_flatten(raw.get("address")),
        tel=_flatten(raw.get("tel")),
        lng_gcj02=lng,
        lat_gcj02=lat,
        parent_id=_text(raw.get("parent")),
        rating=_as_float(biz.get("rating")),
        open_time=_text(biz.get("opentime2")) or _text(biz.get("opentime")),
        photo_url=_first_photo(raw.get("photos")),
        raw_json=json.dumps(raw, ensure_ascii=False, sort_keys=True),
    )


def search_pois(
    keywords: str,
    *,
    city: str,
    limit: int = DEFAULT_LIMIT,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> PoiSearchResult:
    """按关键字在城市范围内搜 POI。

    `city` 传**城市名**（「北京」「西安」），不传 adcode——理由见模块文档。
    """
    keyword = keywords.strip()
    if not keyword:
        return PoiSearchResult(candidates=(), query=keywords, city=city, raw_count=0)

    city_text = city.strip()
    if not city_text:
        raise PoiSearchError("必须给出城市名，否则无法限定搜索范围")

    key = (api_key or config.amap_api_key()).strip()
    if not key:
        raise PoiSearchError("缺少 AMAP_API_KEY，无法搜索 POI")

    params = {
        "key": key,
        "keywords": keyword,
        "city": city_text,
        "citylimit": "true",
        "offset": str(max(1, min(limit, MAX_LIMIT))),
        "page": "1",
        # 不给 all 就没有 biz_ext，评分与开放时间会静默全空
        "extensions": "all",
        "output": "json",
    }
    url = f"{TEXT_SEARCH_URL}?{urllib.parse.urlencode(params)}"

    owned = client is None
    active = client or httpx.Client(timeout=20)
    try:
        response = active.get(url)
    except httpx.HTTPError as exc:
        raise PoiSearchError(f"高德 POI 搜索请求失败：{type(exc).__name__}") from exc
    finally:
        if owned:
            active.close()

    if response.status_code != 200:
        raise PoiSearchError(
            f"高德 POI 搜索返回 HTTP {response.status_code}：{response.text[:120]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise PoiSearchError("高德 POI 搜索返回的内容不是合法 JSON") from exc

    if str(payload.get("status")) != "1":
        raise PoiSearchError(f"高德 POI 搜索失败：{payload.get('info') or '未知错误'}")

    raw_pois = payload.get("pois") or []
    # 返回 None 表示那条缺 id 或缺坐标，直接丢弃而不是补一个假坐标
    parsed = [parse_poi(item) for item in raw_pois if isinstance(item, dict)]
    return PoiSearchResult(
        candidates=tuple(poi for poi in parsed if poi is not None),
        query=keyword,
        city=city_text,
        raw_count=len(raw_pois),
    )


def fetch_poi(
    poi_id: str,
    *,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> CandidatePoi | None:
    """按 id 取一个 POI 的完整信息。

    用来补候选集之外的**父节点**。实测这个问题很实在：搜「兵马俑」返回的
    前十条里有 `秦兵马俑壹号坑大厅`，它的 `parent` 指向 `秦始皇帝陵博物院`
    ——而博物院自己在结果里排第十。父节点不在候选集里时，
    「本体还是子点」这个判断根本无从做起（`lushu/domain/align.py` 里
    `_SUB_PENALTY` 需要知道父节点也在候选中）。
    """
    key = (api_key or config.amap_api_key()).strip()
    if not key:
        raise PoiSearchError("缺少 AMAP_API_KEY，无法查询 POI 详情")

    target = poi_id.strip()
    if not target:
        return None

    params = {"key": key, "id": target, "extensions": "all", "output": "json"}
    url = f"{DETAIL_URL}?{urllib.parse.urlencode(params)}"

    owned = client is None
    active = client or httpx.Client(timeout=20)
    try:
        response = active.get(url)
    except httpx.HTTPError as exc:
        raise PoiSearchError(f"高德 POI 详情请求失败：{type(exc).__name__}") from exc
    finally:
        if owned:
            active.close()

    if response.status_code != 200:
        raise PoiSearchError(f"高德 POI 详情返回 HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise PoiSearchError("高德 POI 详情返回的内容不是合法 JSON") from exc

    if str(payload.get("status")) != "1":
        raise PoiSearchError(f"高德 POI 详情失败：{payload.get('info') or '未知错误'}")

    pois = payload.get("pois") or []
    if not pois or not isinstance(pois[0], dict):
        return None
    return parse_poi(pois[0])


def _text(value: object) -> str | None:
    """高德把「没有值」表示成空数组 `[]`，把「有值」表示成字符串。

    直接 `str(value)` 会把 `[]` 变成字面量 `"[]"`，那是一个看起来有值的垃圾值，
    比空更糟——它会通过「非空」判断一路流到界面上。
    """
    if isinstance(value, list):
        joined = "".join(str(item) for item in value if item)
        return joined.strip() or None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _flatten(value: object) -> str | None:
    """地址与电话可能是数组，拼成一个字符串。"""
    return _text(value)


def _parse_location(value: object) -> tuple[float | None, float | None]:
    """高德的 `location` 是 `"经度,纬度"`。顺序写反就是几百公里的偏移。"""
    text = _text(value)
    if not text or "," not in text:
        return None, None
    lng_text, lat_text = text.split(",", 1)
    lng, lat = _as_float(lng_text), _as_float(lat_text)
    return lng, lat


def _first_photo(value: object) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    first = value[0]
    if not isinstance(first, dict):
        return None
    return _text(first.get("url"))


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, list):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
