"""查看高德 POI 搜索的原始字段，确认有没有能区分「景点本体」与「景点内部子点」的东西。

对齐探测里看到的现象值得单独确认：搜「故宫」回来的前五个里，
`故宫博物院` 与 `故宫博物院-午门`、`故宫博物院检票处` 是并列的候选，
谁在前只由高德排序决定。这两类东西在系统里的地位完全不同——

- `故宫博物院` 是行程里的一个天项，有门票、有预约规则、有开放时间
- `故宫博物院-午门` 是它的一个入口，攻略里的「只有午门能进」挂在这里就错了

如果不能从字段上区分，就只能在名称上做启发式（例如带 `-` 后缀的视为子点），
那是一条脆弱的规则，必须先把原始字段看清楚再决定。

用法：.venv\\Scripts\\python.exe scripts\\probe_m3_poi_fields.py
"""

from __future__ import annotations

import json
import urllib.parse

from _bootstrap import setup

setup()

import lushu.config as config  # noqa: E402
from lushu.engine.amap import resolve_city  # noqa: E402

TEXT_SEARCH_URL = "https://restapi.amap.com/v3/place/text"

CASES = [("故宫", "北京"), ("钟楼", "西安"), ("陕西历史博物馆", "西安"), ("兵马俑", "西安")]


def main() -> int:
    key = config.amap_api_key()
    if not key:
        print("缺少 AMAP_API_KEY")
        return 1

    from third_party.floattrip.core.http import http_get_json

    print("高德 place/text 原始字段勘察")
    print()

    seen_keys: set[str] = set()
    samples: list[dict] = []

    for keywords, city in CASES:
        match = resolve_city(city)
        adcode = match.adcode if match else ""
        params = {
            "key": key,
            "keywords": keywords,
            "city": adcode,
            "citylimit": "true",
            "offset": "5",
            "page": "1",
            "extensions": "all",
            "output": "json",
        }
        data = http_get_json(f"{TEXT_SEARCH_URL}?{urllib.parse.urlencode(params)}")
        pois = data.get("pois") or []
        print(f"── [{city}] “{keywords}”  city 参数用 adcode {adcode}，返回 {len(pois)} 条")
        for poi in pois[:5]:
            seen_keys.update(poi.keys())
            samples.append(poi)
        print()

    print("所有出现过的字段：")
    for name in sorted(seen_keys):
        print(f"  {name}")

    # 重点看这几个可能承载层级关系的字段
    print()
    print("可能承载层级关系的字段在各条上的取值：")
    interesting = [k for k in sorted(seen_keys)
                   if k in ("parent", "pname", "pcode", "adname", "cityname", "citycode",
                            "adcode", "business_area", "distance", "type", "typecode", "indoor")]
    print(f"  {'名称':<34}" + "  ".join(f"{k:<12}" for k in interesting))
    for poi in samples:
        name = str(poi.get("name") or "")[:32]
        cells = []
        for k in interesting:
            value = poi.get(k)
            if isinstance(value, list):
                value = "".join(str(v) for v in value)
            cells.append(f"{str(value or '-')[:12]:<12}")
        print(f"  {name:<34}" + "  ".join(cells))

    print()
    print("第一条的完整 JSON（看有没有上面没列到的东西）：")
    print(json.dumps(samples[0], ensure_ascii=False, indent=2)[:2500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
