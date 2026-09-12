"""把引擎的规划产出转换成自有行程模型。

这是一个**反腐蚀层**：引擎的 `final_plan` 是围绕「一座城市 + 一串扁平的天」
设计的，本项目的行程是「城市停留序列」。转换必须显式地做，且只在一个地方做。

本模块是纯函数，不碰数据库、不碰 third_party、不发起任何网络请求——
城市 adcode 之类的信息由调用方查好后传进来。这样它可以被完全离线测试，
而离线测试正是这一段最需要的：它承载的是 ADR-0004 的不变量。

引擎产出的形状（摘自 `third_party/floattrip/planning/nodes.py` 的 `_finalize_impl`）::

    {
      "query": str, "destination": str, "start_date": "YYYY-MM-DD" | None,
      "end_date": str | None, "days_count": int, "weather_note": str | None,
      "route_issues": [str],
      "days": [{
        "day": int, "date": "YYYY-MM-DD" | None, "theme": str,
        "timeline": [
          {"type": "attraction", "name": str, "amap_poi_id": str | None,
           "start_time": "HH:MM", "end_time": "HH:MM", "period": str,
           "tip": str | None, "rating": float | None, "address": str | None, ...},
          {"type": "lunch"|"dinner", "name": str | None, "reason": str | None,
           "no_restaurant": bool},
        ],
      }],
      "candidate_spots": [...],
    }
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from lushu.domain.planned import (
    ItemKind,
    PlannedDay,
    PlannedItem,
    PlannedStay,
    PlannedTrip,
    PoiFacts,
)

# 引擎 timeline 里的餐饮项类型 → 中文名。用于餐厅缺失时给出可读的占位标题。
_MEAL_LABELS = {"lunch": "午餐", "dinner": "晚餐"}


class PlanConversionError(RuntimeError):
    """规划产出无法转成自有行程模型。"""


def plan_to_trip(
    plan: Mapping[str, Any],
    *,
    start_date: date | None = None,
    city_adcodes: Mapping[str, str] | None = None,
    trip_name: str | None = None,
) -> PlannedTrip:
    """把引擎的 final_plan 转成 PlannedTrip。

    `start_date` 由调用方（行程记录或规划需求）提供时优先于计划里自带的值——
    自有表的 `trip.start_date` 是 NOT NULL，不能依赖引擎一定给了日期。
    """
    days_raw = plan.get("days") or []
    if not isinstance(days_raw, Sequence) or not days_raw:
        raise PlanConversionError("规划产出里没有任何一天，无法生成行程")

    resolved_start = start_date or _parse_date(plan.get("start_date"))
    if resolved_start is None:
        raise PlanConversionError("规划产出没有开始日期，调用方也没有提供，无法生成行程")

    city_name = str(plan.get("destination") or "").strip()
    if not city_name:
        raise PlanConversionError("规划产出没有目的地，无法生成行程")

    warnings: list[str] = []
    ordered_days = sorted(days_raw, key=lambda d: int(d.get("day") or 0))

    declared_days = plan.get("days_count")
    if isinstance(declared_days, int) and declared_days != len(ordered_days):
        warnings.append(f"引擎声称 {declared_days} 天，实际排出 {len(ordered_days)} 天，以实际为准")

    planned_days: list[PlannedDay] = []
    for offset, day_raw in enumerate(ordered_days):
        expected = _add_days(resolved_start, offset)
        actual = _parse_date(day_raw.get("date"))
        if actual is None:
            warnings.append(f"第 {offset + 1} 天没有日期，已按开始日期推算")
        elif actual != expected:
            warnings.append(
                f"第 {offset + 1} 天的日期 {actual} 与推算的 {expected} 不一致，以推算结果为准"
            )

        items, item_warnings = _convert_timeline(day_raw.get("timeline") or [], offset)
        warnings.extend(item_warnings)

        planned_days.append(
            PlannedDay(
                day=expected,
                seq_in_stay=offset,
                theme=_clean_text(day_raw.get("theme")) or None,
                items=items,
            )
        )

    adcodes = city_adcodes or {}
    adcode = adcodes.get(city_name)
    if adcode is None:
        warnings.append(f"城市「{city_name}」未解析到行政区划代码，天气与预算的城市归属会受影响")

    unresolved = sum(
        1 for d in planned_days for i in d.items if i.unresolved_name
    )
    if unresolved:
        warnings.append(f"有 {unresolved} 个景点没能对上实体真源，已送入待对齐队列")

    plan_issues = [str(x) for x in (plan.get("route_issues") or []) if str(x).strip()]
    if plan.get("weather_note"):
        plan_issues.append(str(plan["weather_note"]))

    stay = PlannedStay(
        city_name=city_name,
        city_adcode=adcode,
        seq=0,
        days=tuple(planned_days),
    )
    total = len(planned_days)

    return PlannedTrip(
        name=trip_name or f"{city_name} {total} 天",
        start_date=resolved_start,
        stays=(stay,),
        query=str(plan.get("query") or ""),
        # 引擎给的出行提醒并入 warnings，前端在行程页顶部展示
        warnings=tuple(warnings) + tuple(plan_issues),
    )


def _convert_timeline(
    timeline: Sequence[Any], day_offset: int
) -> tuple[tuple[PlannedItem, ...], list[str]]:
    """转换一天的事项。返回（事项, 警告）。"""
    items: list[PlannedItem] = []
    warnings: list[str] = []
    seen_types: set[str] = set()

    for raw in timeline:
        if not isinstance(raw, Mapping):
            continue
        kind_raw = str(raw.get("type") or "").strip()
        seen_types.add(kind_raw)

        if kind_raw == "attraction":
            items.append(_convert_attraction(raw))
        elif kind_raw in _MEAL_LABELS:
            items.append(_convert_meal(kind_raw, raw))
        else:
            warnings.append(f"第 {day_offset + 1} 天出现未知类型的事项「{kind_raw}」，已跳过")

    if "attraction" not in seen_types:
        warnings.append(f"第 {day_offset + 1} 天没有任何景点")

    return tuple(items), warnings


def _convert_attraction(raw: Mapping[str, Any]) -> PlannedItem:
    """转换一个景点事项。

    引擎给的 `amap_poi_id` 是实体主键（ADR-0002），`location` 是它的 GCJ-02 坐标。
    **只有两者都在才算已对齐**——`poi` 表的坐标是 NOT NULL，缺坐标的景点既存不进去，
    也无法在地图上出现。任何一个缺失都记进 `unresolved_name` 交给待对齐队列，
    绝不静默丢弃，否则这个景点永远挂不上攻略知识。
    """
    name = str(raw.get("name") or "").strip()
    poi_id = _clean_text(raw.get("amap_poi_id"))
    facts = _parse_facts(raw)

    aligned = bool(poi_id) and facts.has_coordinates

    return PlannedItem(
        kind=ItemKind.POI,
        title=name,
        poi_id=poi_id if aligned else None,
        unresolved_name=None if aligned else name,
        # 未对齐时也保留已取到的硬事实：待对齐队列需要这些线索来人工判断
        facts=facts,
        start_time=_clean_text(raw.get("start_time")),
        end_time=_clean_text(raw.get("end_time")),
        note=_clean_text(raw.get("tip")),
    )


def _parse_facts(raw: Mapping[str, Any]) -> PoiFacts:
    """从引擎的景点结构里取出硬事实。坐标是高德的 "lng,lat" 字符串。"""
    lat: float | None = None
    lng: float | None = None

    location = raw.get("location")
    if isinstance(location, Mapping):
        lng = _to_float(location.get("lng"))
        lat = _to_float(location.get("lat"))
    elif isinstance(location, str) and "," in location:
        # 高德的顺序是「经度,纬度」，写反了就是几百公里的偏移
        lng_text, lat_text = location.split(",", 1)
        lng = _to_float(lng_text)
        lat = _to_float(lat_text)

    return PoiFacts(
        lat_gcj02=lat,
        lng_gcj02=lng,
        address=_clean_text(raw.get("address")),
        tel=_clean_text(raw.get("tel")),
        rating=_to_float(raw.get("rating")),
        open_time=_clean_text(raw.get("open_time")),
        photo=_clean_text(raw.get("photo")),
    )


def _convert_meal(kind_raw: str, raw: Mapping[str, Any]) -> PlannedItem:
    """转换一个餐饮事项。引擎没找到合适餐厅时会给出 name=None。"""
    label = _MEAL_LABELS[kind_raw]
    name = _clean_text(raw.get("name"))
    no_restaurant = bool(raw.get("no_restaurant")) or name is None
    reason = _clean_text(raw.get("reason"))

    if no_restaurant:
        return PlannedItem(
            kind=ItemKind.MEAL,
            title=f"{label}（未找到合适餐厅）",
            note=reason or "引擎未在周边找到符合偏好的餐厅，请现场决定",
        )

    return PlannedItem(
        kind=ItemKind.MEAL,
        title=name or label,
        start_time=_clean_text(raw.get("start_time")),
        end_time=_clean_text(raw.get("end_time")),
        note=reason,
    )


# ─── 小工具 ──────────────────────────────────────────────────


def _clean_text(value: Any) -> str | None:
    """把引擎可能给出的 None / 空串 / 空白统一成 None，其余去空白。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = _clean_text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _add_days(anchor: date, offset: int) -> date:
    return anchor + timedelta(days=offset)
