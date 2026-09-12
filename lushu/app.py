"""FastAPI 应用装配。

中间件与生命周期都用当前写法（`lifespan`），不用上游那套已弃用的
`@app.on_event("startup")`。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from lushu import __version__, config
from lushu.api import config_router, health_router
from lushu.engine import prepare_engine
from lushu.store import initialize

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()

    version = initialize()
    logger.info("表结构就绪，版本 %s，库文件 %s", version, config.DB_PATH)

    info = prepare_engine()
    if info.available:
        logger.info("引擎就绪，上游提交 %s", info.upstream_commit)
    else:
        logger.warning("引擎不可用：%s", info.error)

    missing = config.missing_required_keys()
    if missing:
        logger.warning("缺少配置：%s。功能会受限，但服务照常提供。", "、".join(missing))

    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="路书",
        version=__version__,
        description="本地优先的旅行攻略规划工具",
        lifespan=lifespan,
    )

    # 只放行本机来源。不使用通配符与凭据的组合——那个组合在浏览器规范下本就无效，
    # 上游正是这么写的，这里不重蹈。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.DEV_ORIGINS),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(config_router)

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """如果有构建好的前端就托管它，否则给一句可读的提示。

    M0 阶段前端往往还没构建，此时访问根路径不应该是一个 404。
    """
    dist = config.WEB_DIST_DIR
    if dist.is_dir() and (dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
        return

    @app.get("/", include_in_schema=False)
    def frontend_not_built() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "message": "前端尚未构建。开发期请运行 cd web && pnpm dev；构建请运行 pnpm build。",
                "api": "/api/health",
            }
        )


app = create_app()
