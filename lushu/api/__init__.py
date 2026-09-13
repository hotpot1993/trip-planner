"""接口层。

**全项目唯一允许依赖 FastAPI 的地方。** 由
`tests/test_architecture_boundaries.py` 强制。
"""

from .candidate_routes import router as candidate_router
from .config_routes import router as config_router
from .gold_routes import eval_router as eval_router
from .gold_routes import router as gold_router
from .health import router as health_router
from .pipeline_routes import router as pipeline_router
from .plan_routes import router as plan_router
from .trip_routes import router as trip_router
from .workbench_routes import router as workbench_router

__all__ = [
    "candidate_router",
    "config_router",
    "eval_router",
    "gold_router",
    "health_router",
    "pipeline_router",
    "plan_router",
    "trip_router",
    "workbench_router",
]
