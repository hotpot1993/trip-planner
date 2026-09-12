"""路书配置：环境变量加载与路径解析。

整个项目只在这里读取环境变量。

**导入顺序很重要**：必须先导入本模块，再导入 third_party/floattrip 下的任何模块。
引擎会在 import 期读取 LLM_PROVIDER 与各家 API Key，所以环境变量必须先就位。
`lushu/engine/bootstrap.py` 负责保证这个顺序。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT_DIR / ".env.local"

# 不覆盖已存在的环境变量：真实环境优先于文件
load_dotenv(ENV_FILE, override=False)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else default


DATA_DIR = _env_path("LUSHU_DATA_DIR", ROOT_DIR / "data")
DB_PATH = _env_path("LUSHU_DB_PATH", DATA_DIR / "lushu.db")
CHECKPOINT_DB = _env_path("LUSHU_CHECKPOINT_DB", DATA_DIR / "langgraph-checkpoints.db")

SOURCES_DIR = DATA_DIR / "sources"  # 攻略素材全文底档，只留在本地
EXPORTS_DIR = DATA_DIR / "exports"  # 导出的路书

WEB_DIST_DIR = ROOT_DIR / "web" / "dist"
THIRD_PARTY_DIR = ROOT_DIR / "third_party" / "floattrip"

# 本地优先形态：只监听回环地址，不对外暴露
HOST = os.getenv("LUSHU_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = _env_int("LUSHU_PORT", 8756)

# 前端开发服务器（Vite）的地址，开发期需要放行
DEV_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")


def ensure_dirs() -> None:
    """建立运行期需要的目录。"""
    for path in (DATA_DIR, SOURCES_DIR, EXPORTS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def amap_api_key() -> str:
    """高德 Web 服务 Key。实体真源是必填项，缺失时上游适配器会失败。"""
    return os.getenv("AMAP_API_KEY", "").strip()


def amap_js_key() -> str:
    """高德 JS API Key。后端只下发这个，不下发 REST Key。"""
    return os.getenv("AMAP_JS_KEY", "").strip()


def amap_js_security_code() -> str:
    """高德 JS API 安全密钥，与 JS Key 配套。"""
    return os.getenv("AMAP_JS_SECURITY_CODE", "").strip()


def missing_required_keys() -> list[str]:
    """返回缺失的必填配置名，供启动自检提示用。

    这里不抛异常：缺少 Key 时前端仍应能打开并给出可读提示，
    而不是让整个服务起不来。
    """
    missing: list[str] = []
    if not amap_api_key():
        missing.append("AMAP_API_KEY")
    provider = os.getenv("LLM_PROVIDER", "deepseek").strip().lower() or "deepseek"
    if provider == "deepseek" and not os.getenv("DEEPSEEK_API_KEY", "").strip():
        missing.append("DEEPSEEK_API_KEY")
    if provider == "doubao" and not os.getenv("DOUBAO_API_KEY", "").strip():
        missing.append("DOUBAO_API_KEY")
    return missing
