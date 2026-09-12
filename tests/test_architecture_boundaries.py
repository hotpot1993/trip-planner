"""架构边界测试。

这是 M0 的验收条件之一，也是本项目从 FloatTrip 学到的少数几条工程经验里
最值钱的一条：**依赖方向必须由测试守护，而不是靠约定。**

已核实上游确实做到了这一点——它的 `planning`、`chat`、`runtime`、`providers`、
`llm`、`core` 六个模块对 fastapi 与 starlette 零命中，只有 `api/` 依赖 Web 框架。
我们沿用这条纪律，并把它写死在这里。

三条规则：

1. 只有接口层与装配层可以依赖 Web 框架
2. 只有引擎适配层可以触碰 vendored 的 third_party
3. 领域层不得依赖项目内任何其它子包，也不得依赖任何框架
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "lushu"

WEB_FRAMEWORKS = {"fastapi", "starlette", "uvicorn"}
VENDOR_PREFIX = "third_party"

# 领域层连 IO 与编排库也不能碰：它要能在没有数据库、没有网络、没有 LLM 的情况下被测
IO_AND_FRAMEWORK_MODULES = {
    "sqlite3",
    "httpx",
    "requests",
    "langgraph",
    "langchain_core",
    "langchain_openai",
    "openai",
    "redis",
}

# 各层允许引用的自有子包。`lushu` 与 `lushu.config` 是所有层都可用的公共项。
LAYER_ALLOWS: dict[str, set[str]] = {
    "pure": set(),  # __init__.py 与 config.py：只依赖标准库与第三方基础库
    "domain": {"lushu.domain"},
    "store": set(),
    "adapters": {"lushu.domain", "lushu.store"},
    "services": {"lushu.domain", "lushu.store", "lushu.adapters", "lushu.engine"},
    "engine": set(),
    "api": {"lushu.domain", "lushu.services", "lushu.store", "lushu.engine"},
}

# 所有层都可引用的公共项。**必须精确匹配**：裸 `lushu` 若参与前缀判断，
# 会让 `lushu.<任何子包>` 一律合法，规则三就形同虚设。
COMMON_ALLOWED = {"lushu", "lushu.config"}

# 装配层是组合根，允许引用一切
COMPOSITION_FILES = {"app.py", "cli.py", "__main__.py"}
# 只有这两层可以依赖 Web 框架
WEB_ALLOWED_LAYERS = {"api", "composition"}


def _layer_of(path: Path) -> str:
    rel = path.relative_to(PKG)
    if len(rel.parts) == 1:
        return "pure" if rel.name in {"__init__.py", "config.py"} else "composition"
    if rel.parts[0] in LAYER_ALLOWS:
        return rel.parts[0]
    return "composition"


def _own_package(layer: str) -> set[str]:
    """本层自己的包名。

    分层约束管的是**层与层之间的依赖方向**，不是层内引用：
    `services/trip_service` 引用 `services/trip_store` 完全正常。
    少了这一条，规则就会把合法的层内引用误判为违规。
    """
    return {f"lushu.{layer}"} if layer != "pure" else set()


def _module_label(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def _imported_roots(path: Path) -> set[str]:
    """收集模块里出现的所有顶层 import 名。

    同时看普通 import 与函数内的延迟导入——否则把 import 藏进函数就能绕过边界。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # 相对导入（level > 0）算作本包内部，不在本测试的管辖范围
            if node.level == 0 and node.module:
                names.add(node.module)
    return names


def _all_modules() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py"))


def test_sources_were_found() -> None:
    """防止路径写错导致下面三条规则空跑通过。"""
    modules = _all_modules()
    assert len(modules) >= 12, f"只找到 {len(modules)} 个模块，路径可能不对：{PKG}"
    assert (PKG / "domain" / "trip.py").is_file()
    assert (PKG / "store" / "schema.py").is_file()


@pytest.mark.parametrize("path", _all_modules(), ids=_module_label)
def test_web_framework_only_in_interface_layer(path: Path) -> None:
    """规则一：只有接口层与装配层可以依赖 Web 框架。"""
    layer = _layer_of(path)
    if layer in WEB_ALLOWED_LAYERS:
        return

    hits = {name for name in _imported_roots(path) if name.split(".")[0] in WEB_FRAMEWORKS}
    assert not hits, (
        f"{_module_label(path)} 属于 {layer} 层，却引用了 Web 框架 {sorted(hits)}。"
        f"Web 框架只能出现在 api/ 与装配文件里。"
    )


@pytest.mark.parametrize("path", _all_modules(), ids=_module_label)
def test_third_party_only_in_engine_layer(path: Path) -> None:
    """规则二：只有引擎适配层可以触碰 vendored 代码。

    这是 ADR-0005 的可执行形式——把「隔离」从承诺变成会失败的测试。
    """
    layer = _layer_of(path)
    if layer == "engine":
        return

    hits = {name for name in _imported_roots(path) if name.split(".")[0] == VENDOR_PREFIX}
    assert not hits, (
        f"{_module_label(path)} 属于 {layer} 层，却直接引用了 {sorted(hits)}。"
        f"vendored 引擎必须经由 lushu.engine 适配，见 docs/adr/0005。"
    )


@pytest.mark.parametrize("path", _all_modules(), ids=_module_label)
def test_dependency_direction(path: Path) -> None:
    """规则三：依赖方向必须与分层一致。"""
    layer = _layer_of(path)
    if layer == "composition":
        return

    allowed_prefixes = LAYER_ALLOWS[layer] | _own_package(layer)
    offenders: list[str] = []

    for name in _imported_roots(path):
        if not name.startswith("lushu"):
            continue
        # 公共项精确匹配，层级项按前缀匹配
        if name in COMMON_ALLOWED:
            continue
        if any(name == allow or name.startswith(allow + ".") for allow in allowed_prefixes):
            continue
        offenders.append(name)

    assert not offenders, (
        f"{_module_label(path)} 属于 {layer} 层，违规引用了 {sorted(set(offenders))}。"
        f"该层只允许精确引用 {sorted(COMMON_ALLOWED)}，以及前缀 {sorted(allowed_prefixes)}。"
    )


def test_domain_layer_is_framework_free() -> None:
    """领域层不得依赖任何框架、存储或适配器——它要能被纯函数式地测试。"""
    forbidden = WEB_FRAMEWORKS | {VENDOR_PREFIX} | IO_AND_FRAMEWORK_MODULES
    offenders: list[str] = []
    for path in (PKG / "domain").rglob("*.py"):
        for name in _imported_roots(path):
            if name.split(".")[0] in forbidden:
                offenders.append(f"{_module_label(path)} -> {name}")

    assert not offenders, "领域层必须与框架和 IO 无关，违规项：\n  " + "\n  ".join(offenders)


def test_config_layer_is_leaf() -> None:
    """config 是所有层的公共依赖，它自己不能依赖项目内任何东西——否则会成环。"""
    offenders = [name for name in _imported_roots(PKG / "config.py") if name.startswith("lushu")]
    assert not offenders, f"config.py 不能引用项目内其它模块，违规：{offenders}"
