"""高德接口的薄包装。

引擎自带 POI 搜索与天气，但**没有行政区划查询**——而我们需要它把用户给的
城市名解析成行政区划代码与中心坐标：

- `city_stay.city_adcode` 有非空外键约束，城市标识必须以高德为准（ADR-0002）
- M2 的天气面板按城市分别展示，需要城市中心坐标

这里复用引擎的 HTTP 工具（重试、退避、代理、UA），不重复造一套。
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

from lushu import config

# 高德行政区划查询。extensions=base 就够了，我们只要代码、层级与中心点。
DISTRICT_URL = "https://restapi.amap.com/v3/config/district"


class AmapError(RuntimeError):
    """高德接口返回了失败状态。"""


@dataclass(frozen=True)
class CityMatch:
    """行政区划查询的一个候选结果。"""

    name: str
    adcode: str
    level: str  # country / province / city / district / street
    lat_gcj02: float | None = None
    lng_gcj02: float | None = None
    province: str | None = None

    @property
    def is_city(self) -> bool:
        return self.level == "city"


def lookup_city(name: str, *, api_key: str | None = None) -> list[CityMatch]:
    """按名称查询行政区划，返回全部候选。

    返回列表而不是单个结果，是因为「南京」这类输入可能同时命中省市同名项，
    选择权应该交给调用方（未来还要交给用户确认）。
    """
    keyword = name.strip()
    if not keyword:
        return []

    key = (api_key or config.amap_api_key()).strip()
    if not key:
        raise AmapError("缺少 AMAP_API_KEY，无法解析城市")

    # 延迟导入：必须先加载 config，再触碰引擎
    from third_party.floattrip.core.http import http_get_json

    params = {
        "key": key,
        "keywords": keyword,
        "subdistrict": "0",
        "extensions": "base",
        "output": "json",
    }
    data = http_get_json(f"{DISTRICT_URL}?{urllib.parse.urlencode(params)}")

    if data.get("status") != "1":
        raise AmapError(f"高德行政区划查询失败：{data.get('info') or '未知错误'}")

    districts = data.get("districts") or []
    if not isinstance(districts, list):
        return []

    matches: list[CityMatch] = []
    for raw in districts:
        if not isinstance(raw, dict):
            continue
        adcode = str(raw.get("adcode") or "").strip()
        if not adcode:
            continue
        lng, lat = _parse_center(raw.get("center"))
        matches.append(
            CityMatch(
                name=str(raw.get("name") or "").strip(),
                adcode=adcode,
                level=str(raw.get("level") or "").strip(),
                lat_gcj02=lat,
                lng_gcj02=lng,
                province=str(raw.get("province") or "").strip() or None,
            )
        )
    return matches


def resolve_city(name: str, *, api_key: str | None = None) -> CityMatch | None:
    """把城市名解析成唯一的行政区划。

    优先精确同名且层级为「市」的结果。省级或区级结果一律不采用——
    把「江苏」当成一座城市停留会让整个行程的天数分配失去意义。
    """
    matches = lookup_city(name, api_key=api_key)
    exact = [m for m in matches if m.is_city and m.name == name.strip()]
    if exact:
        return exact[0]

    cities = [m for m in matches if m.is_city]
    return cities[0] if cities else None


def _parse_center(value: object) -> tuple[float | None, float | None]:
    """高德返回的中心点是 "经度,纬度" 字符串。顺序写反就是几百公里的偏移。"""
    if not isinstance(value, str) or "," not in value:
        return None, None
    lng_text, lat_text = value.split(",", 1)
    try:
        return float(lng_text), float(lat_text)
    except ValueError:
        return None, None
