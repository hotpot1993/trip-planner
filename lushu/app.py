"""FastAPI 应用装配。

中间件与生命周期都用当前写法（`lifespan`），不用上游那套已弃用的
`@app.on_event("startup")`。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from lushu import __version__, config
from lushu.api import config_router, health_router, trip_router
from lushu.engine import AmapError, prepare_engine
from lushu.services.plan_converter import PlanConversionError
from lushu.services.trip_service import CityNotFoundError, MissingInputError
from lushu.services.trip_store import UnresolvedCityError
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
    app.include_router(trip_router)

    _register_error_handlers(app)
    _mount_frontend(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    """把领域与服务的异常翻译成可读的中文 HTTP 响应。

    这些错误都是用户能据以行动的（「这个城市查不到」「还需要告诉我日期」），
    所以必须带着结构化信息返回，而不是一个 500 加一段堆栈。
    """

    @app.exception_handler(MissingInputError)
    def _missing_input(_request: Request, exc: MissingInputError) -> JSONResponse:
        # 对话式流程的正常分支：把该问的问题交回前端
        return JSONResponse(
            status_code=422,
            content={"error": "missing_input", "message": str(exc), "missing_fields": list(exc.missing_fields)},
        )

    @app.exception_handler(CityNotFoundError)
    def _city_not_found(_request: Request, exc: CityNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "city_not_found", "message": str(exc), "city_names": list(exc.city_names)},
        )

    @app.exception_handler(UnresolvedCityError)
    def _unresolved_city(_request: Request, exc: UnresolvedCityError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "city_not_found", "message": str(exc), "city_names": list(exc.city_names)},
        )

    @app.exception_handler(AmapError)
    def _amap_error(_request: Request, exc: AmapError) -> JSONResponse:
        # 上游服务的问题不是用户的错，用 502 明确区分
        return JSONResponse(
            status_code=502,
            content={"error": "amap_unavailable", "message": str(exc)},
        )

    @app.exception_handler(PlanConversionError)
    def _plan_conversion(_request: Request, exc: PlanConversionError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={"error": "plan_conversion_failed", "message": str(exc)},
        )


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
