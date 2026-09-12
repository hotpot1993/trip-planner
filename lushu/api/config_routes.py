"""下发前端所需的运行时配置。

**密钥隔离**：这里只下发高德 JS API Key，绝不下发 Web 服务 Key（REST Key）。
JS Key 按设计就是给浏览器用的；REST Key 一旦下发到前端就等于公开，
而它带着服务端配额。这条边界由 `tests/test_api_config.py` 守护。
"""

from __future__ import annotations

from fastapi import APIRouter

from lushu import config

router = APIRouter(prefix="/api", tags=["系统"])


@router.get("/config")
def client_config() -> dict:
    return {
        "amap_js_key": config.amap_js_key(),
        "amap_js_security_code": config.amap_js_security_code(),
        "missing_keys": config.missing_required_keys(),
    }
