"""把 vendored 引擎接到本项目上的薄适配层。

做三件事，每件都对应一个已验证的真实适配点（详见
`third_party/floattrip/README.md` 的「已知适配项」）：

1. **保证配置先于引擎加载。** 引擎在 import 期就会读取 `LLM_PROVIDER`
   与各家 API Key，所以 `lushu.config` 必须先完成 `.env.local` 的加载。
   这里用延迟导入把顺序写死在代码里，而不是靠调用方自觉。
2. **把引擎自带的 SQLite 库路径指到本项目的数据目录。** 引擎的
   `core/database.py` 按 `parents[3]` 定位默认库，换位置后会指向
   `third_party/data/app.db`。上游留了 `configure_database()` 这个接缝，
   正是为这类场景准备的。
3. **让引擎的表结构就位。** 引擎有它自己的 13 张表（会话、运行、检查点），
   与本项目的七域表结构共存于同一个 SQLite 文件。

关于第 3 点的一个重要说明：**引擎的表与本项目的表互不替代。**
引擎的表服务于对话与运行状态，本项目的表服务于行程、知识与预约。
两者之间由 services 层做转换，不做跨层直连。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lushu import config

# 引擎初始化是幂等的，但重复调用 configure_database 会重复建表检查。
# 记录「已经为哪个库文件准备过」而不是简单的布尔标记——库路径变了就必须重做，
# 否则测试里换了临时库之后引擎的表不会建出来。
_prepared_path: str | None = None


@dataclass
class EngineInfo:
    """引擎状态，供健康检查展示。"""

    available: bool
    upstream_commit: str | None = None
    error: str | None = None
    database_path: str | None = None
    modules: list[str] = field(default_factory=list)


def prepare_engine(*, init_database: bool = True) -> EngineInfo:
    """让引擎就绪。返回状态而不是抛异常——引擎不可用时服务仍应能起来。

    缺少 API Key 与引擎 import 失败是两件事：前者由
    `config.missing_required_keys()` 报告，后者才会走到这里的 error 分支。
    """
    global _prepared_path

    # 延迟导入：必须先加载 config，再触碰引擎
    try:
        from third_party.floattrip.core import database as engine_database
    except Exception as exc:  # pragma: no cover - 依赖缺失时的兜底
        return EngineInfo(available=False, error=f"{type(exc).__name__}: {exc}")

    config.ensure_dirs()

    try:
        engine_database.configure_database(config.DB_PATH)
        target = str(config.DB_PATH)
        if init_database and _prepared_path != target:
            engine_database.init_db()
            _prepared_path = target
    except Exception as exc:  # pragma: no cover
        return EngineInfo(available=False, error=f"{type(exc).__name__}: {exc}")

    return EngineInfo(
        available=True,
        upstream_commit=_engine_version(),
        database_path=str(config.DB_PATH),
        modules=_loaded_modules(),
    )


def engine_info() -> EngineInfo:
    """只读地报告引擎状态，不触发初始化。"""
    try:
        from third_party.floattrip.core import database as engine_database
    except Exception as exc:  # pragma: no cover
        return EngineInfo(available=False, error=f"{type(exc).__name__}: {exc}")

    return EngineInfo(
        available=True,
        upstream_commit=_engine_version(),
        database_path=str(engine_database.get_db_path()),
        modules=_loaded_modules(),
    )


def reset_engine() -> None:
    """清掉进程内标记，供测试使用。"""
    global _prepared_path
    _prepared_path = None


def _engine_version() -> str | None:
    """上游没有版本号，用提交哈希的前 7 位代替，便于追溯。"""
    try:
        manifest = config.THIRD_PARTY_DIR / "MANIFEST.txt"
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.startswith("commit"):
                return line.split()[-1][:7]
    except OSError:
        return None
    return None


def _loaded_modules() -> list[str]:
    """列出可用且已导入的引擎模块。"""
    import importlib
    import sys

    candidates = (
        "third_party.floattrip.core.database",
        "third_party.floattrip.llm.factory",
        "third_party.floattrip.providers.amap.poi",
        "third_party.floattrip.planning.graph",
        "third_party.floattrip.chat.graph",
        "third_party.floattrip.runtime.manager",
    )
    names: list[str] = []
    for name in candidates:
        if name in sys.modules:
            names.append(name.removeprefix("third_party.floattrip."))
            continue
        try:
            importlib.import_module(name)
            names.append(name.removeprefix("third_party.floattrip."))
        except Exception:
            continue
    return names
