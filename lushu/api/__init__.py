"""接口层。

**全项目唯一允许依赖 FastAPI 的地方。** 由
`tests/test_architecture_boundaries.py` 强制。
"""

from .config_routes import router as config_router
from .health import router as health_router

__all__ = ["config_router", "health_router"]
