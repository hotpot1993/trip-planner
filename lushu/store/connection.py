"""SQLite 连接管理。

连接策略：短连接 + WAL。本项目是单机自用工具，并发量极低，
短连接换来的简单性远比连接池值得。WAL 配合 busy_timeout 足以
让数据管线的长任务与 Web 请求互不阻塞。

**注意事务语义**：`connect()` 返回的是标准 sqlite3 连接，写入后必须
`commit()`。直接 `close()` 会**回滚未提交的写入**——这是个安静的坑，
不会报错，只是数据不见了。批量写入请一律用 `transaction()`。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from lushu import config

# 数据管线的批量写入可能持续较久，给足等待时间
BUSY_TIMEOUT_MS = 30_000


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """打开一个已配置好的连接。调用方负责关闭。"""
    path = Path(db_path) if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    # 外键约束默认是关的，本项目大量依赖级联删除，必须显式打开
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """在一个事务里操作数据库，异常时回滚。"""
    conn = connect(db_path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()
