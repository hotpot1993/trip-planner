"""探测 M3 实体对齐：把攻略里的自然语言景点名对到高德 POI。

风险表把这一条列为最致命的：

    对不上，攻略知识就挂不到行程上，产品价值归零

提纯产出的是「故宫只有午门能进」这种带上下文线索的自然语言（docs/DESIGN.md 4.4），
对齐阶段要把它变成一个 `amap_poi_id`。这里要看的是**对不齐会怎么错**，
而不只是「准不准」——因为错的形状决定了管线要加哪些防护。

四类失败模式，代价完全不同：

1. **量级不符**：搜「故宫」回来一个「故宫博物院(售票处)」或整座城市的中心点，
   外部键（amap_poi_id）本身合规，但挂上去的知识会出现在错误的卡片上。
2. **跨城**：搜「钟楼」在西安却回了北京的钟楼。坐标一错，路线与天气全错。
3. **多候选**：同名多个 POI，谁在前全凭高德排序，我们没有判断依据就选了第一个。
4. **确实不存在**：例如「武英殿」是故宫内部的殿，高德不一定把它建成独立 POI。
   这一类必须进待对齐队列，而不是硬塞一个近似结果。

用法：.venv\\Scripts\\python.exe scripts\\probe_m3_align.py
"""

from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass

from _bootstrap import setup

setup()

import lushu.config as config  # noqa: E402
from lushu.engine.amap import AmapError, resolve_city  # noqa: E402

TEXT_SEARCH_URL = "https://restapi.amap.com/v3/place/text"


@dataclass(frozen=True)
class Mention:
    """提纯阶段会产出的一条景点提及，加上它自带的上文线索。"""

    name: str  # 原文里的叫法，可能是简称、俗称
    city: str  # 上下文线索：这篇素材讲的是哪座城市
    expect: str  # 人工判断的正确答案（用于对照，不进管线）


# 刻意混入四种难度：全称、简称/俗称、同名歧义、可能不存在
MENTIONS = [
    Mention("故宫", "北京", "故宫博物院"),
    Mention("故宫博物院", "北京", "故宫博物院"),
    Mention("神武门", "北京", "神武门"),
    Mention("午门", "北京", "午门"),
    Mention("角楼", "北京", "故宫角楼"),
    Mention("太和殿", "北京", "太和殿"),
    Mention("珍宝馆", "北京", "珍宝馆"),
    Mention("武英殿", "北京", "（可能不存在独立 POI）"),
    Mention("景山", "北京", "景山公园"),
    Mention("金鱼胡同", "北京", "（地铁站，未必是景点 POI）"),
    Mention("陕西历史博物馆", "西安", "陕西历史博物馆"),
    Mention("陕历博", "西安", "陕西历史博物馆"),
    Mention("兵马俑", "西安", "秦始皇帝陵博物院"),
    Mention("大唐不夜城", "西安", "大唐不夜城"),
    Mention("西安城墙", "西安", "西安城墙"),
    Mention("钟楼", "西安", "西安钟楼"),
    Mention("回民街", "西安", "回民街"),
    Mention("洒金桥", "西安", "洒金桥"),
    Mention("袁家村", "西安", "袁家村（咸阳）"),
    Mention("不倒翁小姐姐", "西安", "（不是一个地点）"),
]


def text_search(keywords: str, *, city: str | None, key: str, limit: int = 10) -> list[dict]:
    """高德关键字搜索。citylimit 打开，避免跨城命中被排到前面。"""
    from third_party.floattrip.core.http import http_get_json

    params = {
        "key": key,
        "keywords": keywords,
        "offset": str(limit),
        "page": "1",
        "extensions": "all",
        "output": "json",
    }
    if city:
        params["city"] = city
        params["citylimit"] = "true"

    data = http_get_json(f"{TEXT_SEARCH_URL}?{urllib.parse.urlencode(params)}")
    if data.get("status") != "1":
        raise AmapError(f"{data.get('info') or '未知错误'}")
    pois = data.get("pois") or []
    return [p for p in pois if isinstance(p, dict)]


def short(poi: dict) -> str:
    return str(poi.get("name") or "").strip()


def addr(poi: dict) -> str:
    value = poi.get("address")
    if isinstance(value, list):
        value = "".join(str(v) for v in value)
    return str(value or "").strip()


def main() -> int:
    key = config.amap_api_key()
    if not key:
        print("缺少 AMAP_API_KEY")
        return 1

    print("M3 实体对齐探测：高德关键字搜索 vs 攻略里的自然语言叫法")
    print(f"  共 {len(MENTIONS)} 条提及，每条取前 5 个候选")
    print()

    codes: dict[str, str] = {}
    for city in ("北京", "西安", "咸阳"):
        try:
            match = resolve_city(city)
            codes[city] = match.adcode if match else ""
            print(f"  城市解析 {city} → {match.name if match else '失败'} ({codes[city]})")
        except AmapError as exc:
            print(f"  城市解析 {city} 失败：{exc}")
            return 1
    print()

    stats = {"top1": 0, "top5": 0, "none": 0}
    slow = 0.0

    for mention in MENTIONS:
        started = time.time()
        try:
            pois = text_search(mention.name, city=mention.city, key=key)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{mention.city}] {mention.name} → 查询失败：{exc}")
            stats["none"] += 1
            continue
        slow += time.time() - started

        print(f"  [{mention.city}] “{mention.name}”  期望：{mention.expect}")
        if not pois:
            print("      → 零结果")
            stats["none"] += 1
            continue

        for index, poi in enumerate(pois[:5]):
            mark = "①" if index == 0 else f"{index + 1}"
            # 高德返回的 adcode 前四位是市一级
            poi_adcode = str(poi.get("adcode") or "").strip()
            city_ok = poi_adcode[:4] == codes.get(mention.city, "")[:4] if poi_adcode else None
            flag = "" if city_ok else "  ⚠跨城"
            print(f"      {mark} {short(poi):<22} "
                  f"{poi.get('type', '')[:18]:<20} {addr(poi)[:24]:<26}"
                  f"adcode={poi_adcode}{flag}")

        top_name = short(pois[0])
        # 期望值里带括号的表示「不一定存在」，只做观察不做判定
        if mention.expect.startswith("（"):
            print("      （此条无标准答案，仅观察）")
        elif mention.expect in top_name or top_name in mention.expect:
            stats["top1"] += 1
            print("      ✓ 首位命中")
        elif any(mention.expect in short(p) or short(p) in mention.expect for p in pois[:5]):
            stats["top5"] += 1
            print("      ~ 首位未中，前五内有")
        else:
            print("      ✗ 前五内没有期望结果")
        print()

    judged = len([m for m in MENTIONS if not m.expect.startswith("（")])
    print("=" * 70)
    print(f"  可判定的 {judged} 条中：首位命中 {stats['top1']}，"
          f"前五内命中 {stats['top5']}，零结果 {stats['none']}")
    if judged:
        print(f"  首位准确率 {stats['top1'] / judged:.0%}，"
              f"前五召回率 {(stats['top1'] + stats['top5']) / judged:.0%}")
    print(f"  {len(MENTIONS)} 次查询共 {slow:.1f}s，平均 {slow / len(MENTIONS):.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
