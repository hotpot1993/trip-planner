"""外部适配器：引擎没有覆盖的数据源。

与 `lushu.engine` 的分工：

- **高德的 POI 搜索、天气、行政区划**走 `lushu.engine`——引擎已经实现了，
  没必要再造一套 HTTP 客户端。
- **铁路（12306）与天气的开放数据源（Open-Meteo）**放在这里——引擎没有。

本层负责与外部世界打交道，把外部响应翻译成领域对象。它不依赖 FastAPI，
也不依赖 `third_party`。
"""

from .rail import (
    RailQuery,
    RailUnavailableError,
    Station,
    haversine_km,
    load_stations,
    parse_station_table,
    parse_ticket_rows,
    pick_station,
    query_rail,
    rail_distance_km,
    sale_window_end,
    station_candidates,
    within_sale_window,
)

__all__ = [
    "RailQuery",
    "RailUnavailableError",
    "Station",
    "haversine_km",
    "load_stations",
    "parse_station_table",
    "parse_ticket_rows",
    "pick_station",
    "query_rail",
    "rail_distance_km",
    "sale_window_end",
    "station_candidates",
    "within_sale_window",
]
