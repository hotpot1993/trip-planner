"""健康检查与运行状态。"""

from __future__ import annotations

from fastapi import APIRouter

from lushu import __version__, config
from lushu.engine import engine_info
from lushu.store import SCHEMA_VERSION

router = APIRouter(prefix="/api", tags=["系统"])


@router.get("/health")
def health() -> dict:
    """服务、表结构与引擎三段状态。

    配置缺失只作为警告返回，不让健康检查失败——缺 Key 时前端仍应能打开
    并给出可读提示，而不是看到一个起不来的服务。
    """
    info = engine_info()
    return {
        "status": "ok",
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "database": {"path": str(config.DB_PATH), "exists": config.DB_PATH.exists()},
        "engine": {
            "available": info.available,
            "upstream_commit": info.upstream_commit,
            "database_path": info.database_path,
            "modules": info.modules,
            "error": info.error,
        },
        "warnings": [f"缺少配置 {name}" for name in config.missing_required_keys()],
    }
