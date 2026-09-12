"""应用服务层。

编排领域对象与适配器，向 `api` 层提供用例级接口。
本层不依赖 FastAPI，也不直接触碰 `third_party`（引擎访问统一走 `lushu.engine`）。
"""

from .plan_converter import PlanConversionError, plan_to_trip

__all__ = ["PlanConversionError", "plan_to_trip"]
