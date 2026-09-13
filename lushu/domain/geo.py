"""地理计算。

只有一个函数，但它必须只有**一份**：路书估路段用它，餐饮候选排序也用它。
两处各写一遍 haversine 不会立刻出错，只会在某一处被改动之后悄悄地不一致——
而两个地方算出来的距离差几十米，页面上根本看不出来。

坐标只比差值，所以 GCJ-02 与 WGS-84 混不进误差：那是一次整体偏移（ADR-0003）。
**但两端必须是同一套坐标**——拿 GCJ-02 的点和 WGS-84 的点比，差的就不是几十米了。
"""

from __future__ import annotations

import math

# 地球平均半径（米）。用球面近似而不是椭球：这里的用途是「大概多远、
# 该走还是该坐车」，误差在千分之几，远小于「实际路程比直线长」这件事本身。
EARTH_RADIUS_M = 6_371_000.0


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两点间的大圆距离（米）。"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))
