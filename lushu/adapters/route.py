"""高德路径规划适配器：两点之间**真实**怎么走、多远、多久。

为什么要单独一层：路书里的路段说明原先全是「直线距离估算」，页面上如实写着
「直线距离估算，实际路程更长」。那是诚实的，但不等于够用——真实步行距离
通常是直线的 1.3 倍，**写着「步行 1200 米」而实际要走 1800 米**，
正是设计里说的「现场会很意外」。

只做两个端点，不解析更细的换乘方案：

- **步行**（`direction/walking`）：市内相邻两点，这是最要紧的一类。
- **驾车**（`direction/driving`）：距离超过步行上限时用它给一个「打车大约多久」。
  它同时也是**最快路径**，不是公交——公交换乘要解析 `transit/integrated` 的
  分段结果（线路名、上下车点、步行接驳），那是另一个量级的工作，而且公交
  方案随时刻变化，存下来第二天就可能不对。所以这一层如实只给「打车」，
  公交仍然交给用户在当地 App 里查。

与 `adapters/poi.py` 一样：返回的是**能算出来的那几个数**，算不出来就抛，
不猜一个值出来。
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

import httpx

from lushu import config

WALKING_URL = "https://restapi.amap.com/v3/direction/walking"
DRIVING_URL = "https://restapi.amap.com/v3/direction/driving"


class RouteError(RuntimeError):
    """路径规划失败。"""


@dataclass(frozen=True)
class RoutePlan:
    """一条真实路径。"""

    mode: str  # walk | taxi
    distance_m: int
    duration_min: int


def _api_key(api_key: str | None) -> str:
    key = (api_key or config.amap_api_key()).strip()
    if not key:
        raise RouteError("缺少 AMAP_API_KEY，无法做路径规划")
    return key


def _plan(
    url: str,
    *,
    mode: str,
    from_lat: float,
    from_lng: float,
    to_lat: float,
    to_lng: float,
    what: str,
    api_key: str | None,
    client: httpx.Client | None,
) -> RoutePlan:
    params = {
        "key": _api_key(api_key),
        # 与周边搜索同样的坑：高德的 location 是「经度,纬度」
        "origin": f"{from_lng:.6f},{from_lat:.6f}",
        "destination": f"{to_lng:.6f},{to_lat:.6f}",
        "output": "json",
    }
    owned = client is None
    active = client or httpx.Client(timeout=20)
    try:
        response = active.get(f"{url}?{urllib.parse.urlencode(params)}")
    except httpx.HTTPError as exc:
        raise RouteError(f"高德{what}请求失败：{type(exc).__name__}") from exc
    finally:
        if owned:
            active.close()

    if response.status_code != 200:
        raise RouteError(f"高德{what}返回 HTTP {response.status_code}：{response.text[:120]}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise RouteError(f"高德{what}返回的内容不是合法 JSON") from exc

    if str(payload.get("status")) != "1":
        raise RouteError(f"高德{what}失败：{payload.get('info') or '未知错误'}")

    path = _first_path(payload)
    distance, duration = path
    if distance is None or duration is None:
        # 两点之间没有可行路径时高德返回空的 paths。**这不是 0 米**，
        # 是「这条路算不出来」——写成 0 会让路书说「就在旁边」。
        raise RouteError(f"高德{what}没有给出可行路径")
    return RoutePlan(
        mode=mode,
        distance_m=round(distance),
        # 高德的 duration 是秒
        duration_min=max(1, round(duration / 60)),
    )


def _first_path(payload: dict) -> tuple[float | None, float | None]:
    route = payload.get("route") or {}
    paths = route.get("paths") or []
    if not paths or not isinstance(paths[0], dict):
        return None, None
    first = paths[0]
    return _as_float(first.get("distance")), _as_float(first.get("duration"))


def _as_float(value: object) -> float | None:
    """高德把数字给成字符串，把「没有」给成空数组。"""
    if isinstance(value, list):
        return None
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def plan_walk(
    *,
    from_lat: float,
    from_lng: float,
    to_lat: float,
    to_lng: float,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> RoutePlan:
    """步行路径。传 GCJ-02 坐标（ADR-0003）。"""
    return _plan(
        WALKING_URL,
        mode="walk",
        from_lat=from_lat,
        from_lng=from_lng,
        to_lat=to_lat,
        to_lng=to_lng,
        what="步行路径规划",
        api_key=api_key,
        client=client,
    )


def plan_drive(
    *,
    from_lat: float,
    from_lng: float,
    to_lat: float,
    to_lng: float,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> RoutePlan:
    """驾车路径。用来给「打车大约多久」，不是公交方案（见模块文档）。"""
    return _plan(
        DRIVING_URL,
        mode="taxi",
        from_lat=from_lat,
        from_lng=from_lng,
        to_lat=to_lat,
        to_lng=to_lng,
        what="驾车路径规划",
        api_key=api_key,
        client=client,
    )
