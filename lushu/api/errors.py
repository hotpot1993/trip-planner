"""异常到 HTTP 响应 / SSE 事件的翻译。

只在这里定义一次。异常处理器（普通请求）与 SSE 端点（流式请求）都要用同一套
映射——后者拿不到 FastAPI 的异常处理链，很容易写成第二份实现然后慢慢分叉。
"""

from __future__ import annotations

from typing import Any

from lushu.engine import AmapError
from lushu.services.plan_converter import PlanConversionError
from lushu.services.trip_service import CityNotFoundError, ConfigMissingError, MissingInputError
from lushu.services.trip_store import UnresolvedCityError


def describe_error(exc: Exception) -> tuple[int, dict[str, Any]]:
    """把异常翻译成（状态码, 响应体）。

    这些都是用户能据以行动的错误，所以带着结构化信息返回，而不是一段堆栈。
    """
    if isinstance(exc, MissingInputError):
        # 对话式流程的正常分支：把该问的问题交回前端
        return 422, {
            "error": "missing_input",
            "message": str(exc),
            "missing_fields": list(exc.missing_fields),
        }

    if isinstance(exc, ConfigMissingError):
        # 服务端没配好，不是用户的错，也不是程序缺陷——503 与两者都区分得开
        return 503, {
            "error": "config_missing",
            "message": str(exc),
            "missing_keys": list(exc.missing_keys),
        }

    if isinstance(exc, (CityNotFoundError, UnresolvedCityError)):
        return 422, {
            "error": "city_not_found",
            "message": str(exc),
            "city_names": list(exc.city_names),
        }

    if isinstance(exc, AmapError):
        # 上游服务的问题不是用户的错，用 502 明确区分
        return 502, {"error": "amap_unavailable", "message": str(exc)}

    if isinstance(exc, PlanConversionError):
        return 502, {"error": "plan_conversion_failed", "message": str(exc)}

    return 500, {"error": "internal", "message": f"{type(exc).__name__}: {exc}"}


def is_expected(exc: Exception) -> bool:
    """是否为已知的、面向用户的错误（而非程序缺陷）。"""
    return isinstance(
        exc,
        (
            MissingInputError,
            ConfigMissingError,
            CityNotFoundError,
            UnresolvedCityError,
            AmapError,
            PlanConversionError,
        ),
    )
