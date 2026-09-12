"""引擎适配层测试。

验证 ADR-0005 的接缝真的接上了：引擎可用、库路径被改到本项目的数据目录、
引擎自己的表与本项目的表共存于同一个 SQLite 文件。
"""

from __future__ import annotations

from pathlib import Path

from lushu.engine import engine_info, prepare_engine, reset_engine
from lushu.store import connect, initialize
from lushu.store.schema import table_names

OUR_TABLES = {"trip", "city_stay", "claim", "booking_rule"}
ENGINE_TABLES = {"users", "itineraries", "conversations", "messages"}


def test_engine_is_available(isolated_config) -> None:
    info = prepare_engine()
    assert info.available, info.error
    assert info.upstream_commit == "ec911f7"


def test_engine_database_is_redirected_into_the_project(isolated_config) -> None:
    """上游按 parents[3] 定位库文件，换位置后会指错；适配层必须把它掰回来。"""
    prepare_engine()

    info = engine_info()
    assert Path(info.database_path) == isolated_config.DB_PATH
    # 确认没有落到 third_party 里
    assert "third_party" not in str(info.database_path).lower()


def test_engine_tables_land_in_the_same_file_as_ours(isolated_config) -> None:
    initialize()
    prepare_engine()

    conn = connect()
    try:
        present = table_names(conn)
    finally:
        conn.close()

    assert OUR_TABLES <= present, f"缺本项目的表：{sorted(OUR_TABLES - present)}"
    assert ENGINE_TABLES <= present, f"缺引擎的表：{sorted(ENGINE_TABLES - present)}"


def test_prepare_engine_is_idempotent(isolated_config) -> None:
    first = prepare_engine()
    second = prepare_engine()
    assert first.available and second.available


def test_prepare_engine_reinitializes_when_database_changes(isolated_config, tmp_path) -> None:
    """换库之后引擎的表必须在新库里重新建出来。

    这是适配层里一个真实的坑：如果只用布尔标记记住「准备过了」，
    测试里第二次换库时引擎的表就永远不会建出来。
    """
    prepare_engine()
    assert isolated_config.DB_PATH.exists()

    moved = tmp_path / "另一个目录" / "lushu.db"
    isolated_config.DB_PATH = moved
    info = prepare_engine()
    assert info.available, info.error

    conn = connect(moved)
    try:
        present = table_names(conn)
    finally:
        conn.close()
    assert ENGINE_TABLES <= present


def test_reset_engine_clears_the_marker(isolated_config) -> None:
    prepare_engine()
    reset_engine()
    # 重置之后仍然可用，说明重置没有破坏任何状态
    assert prepare_engine().available


def test_engine_reports_modules(isolated_config) -> None:
    info = prepare_engine()
    assert info.modules, "应至少列出可导入的引擎模块"
    assert any("planning" in name or "core" in name for name in info.modules)
