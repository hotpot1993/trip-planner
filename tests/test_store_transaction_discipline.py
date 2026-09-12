"""事务纪律测试：`with connect(...)` 不提交。

这是一个**安静的坑**，而且我在这轮里真的踩了：写测试造数据时用

    with connect(db) as conn:
        conn.execute("INSERT ...")

看起来提交了，其实没有。`sqlite3.Connection` 的上下文管理器只管事务边界，
**退出时不 commit**；连接一关，写入就回滚。它不报错，只是数据不见了，
所以症状出现在很远的地方——另一个测试报外键约束失败，而根因在这里。

`lushu/store/connection.py` 的模块文档写明了「写入必须 commit，批量写入
一律用 transaction()」。本测试把那句话变成会失败的检查。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = (ROOT / "lushu", ROOT / "tests", ROOT / "scripts")

# 写语句的开头。用关键字判断而不是完整解析 SQL——这里要抓的是「有没有写」，
# 不是「写得对不对」。
_WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER")

# 这些语句虽然以写关键字开头，但不改数据行，不算「需要提交的写入」
_WRITE_EXEMPT = ("CREATE TEMP", "PRAGMA")


def _is_connection_context(item: ast.withitem) -> bool:
    """`with connect(...) as conn:` 这种形态。"""
    call = item.context_expr
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "connect"
    if isinstance(func, ast.Attribute):
        return func.attr == "connect"
    return False


def _sql_literals(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def _is_write(node: ast.AST) -> bool:
    for text in _sql_literals(node):
        head = text.lstrip().upper()
        if any(head.startswith(keyword) for keyword in _WRITE_EXEMPT):
            continue
        if any(head.startswith(keyword) for keyword in _WRITE_KEYWORDS):
            return True
    return False


def _commits(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = getattr(child.func, "attr", None) or getattr(child.func, "id", None)
            if name in ("commit", "rollback"):
                return True
    return False


def _offenders(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        if not any(_is_connection_context(item) for item in node.items):
            continue
        if _commits(node):
            continue
        for child in node.body:
            if _is_write(child):
                found.append((node.lineno, ast.unparse(child)[:80]))
                break
    return found


def _modules() -> list[Path]:
    modules: list[Path] = []
    for package in PACKAGES:
        if package.is_dir():
            modules.extend(sorted(package.rglob("*.py")))
    return modules


def test_sources_were_found() -> None:
    """防止路径写错导致下面的检查空跑通过。"""
    modules = _modules()
    assert len(modules) >= 40, f"只找到 {len(modules)} 个模块：{PACKAGES}"
    assert (ROOT / "lushu" / "store" / "connection.py").is_file()


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(ROOT)))
def test_writes_inside_a_plain_connection_are_committed(path: Path) -> None:
    """`with connect(...)` 里写数据，要么显式 commit，要么改用 transaction()。"""
    offenders = _offenders(path)
    if not offenders:
        return

    detail = "\n".join(f"  第 {line} 行：{code}" for line, code in offenders)
    pytest.fail(
        f"{path.relative_to(ROOT)} 在 `with connect(...)` 里执行了写语句，"
        f"但没有 commit：\n{detail}\n"
        "sqlite3.Connection 的上下文管理器退出时**不提交**，连接一关写入就回滚。"
        "请改用 `with transaction(...) as conn:`，或在块内显式 conn.commit()。"
    )


class TestTheCheckerItself:
    """检查器本身要被检查：一个永远通过的检查比没有检查更糟。"""

    def _run(self, source: str, tmp_path: Path) -> list[tuple[int, str]]:
        path = tmp_path / "sample.py"
        path.write_text(source, encoding="utf-8")
        return _offenders(path)

    def test_catches_uncommitted_write(self, tmp_path: Path) -> None:
        source = (
            "with connect(db) as conn:\n"
            '    conn.execute("INSERT INTO poi (a) VALUES (1)")\n'
        )
        assert self._run(source, tmp_path), "漏掉了未提交的写入"

    def test_accepts_explicit_commit(self, tmp_path: Path) -> None:
        source = (
            "with connect(db) as conn:\n"
            '    conn.execute("INSERT INTO poi (a) VALUES (1)")\n'
            "    conn.commit()\n"
        )
        assert not self._run(source, tmp_path)

    def test_accepts_reads(self, tmp_path: Path) -> None:
        source = (
            "with connect(db) as conn:\n"
            '    conn.execute("SELECT * FROM poi").fetchall()\n'
        )
        assert not self._run(source, tmp_path)

    def test_accepts_transaction_helper(self, tmp_path: Path) -> None:
        source = (
            "with transaction(db) as conn:\n"
            '    conn.execute("INSERT INTO poi (a) VALUES (1)")\n'
        )
        assert not self._run(source, tmp_path)
