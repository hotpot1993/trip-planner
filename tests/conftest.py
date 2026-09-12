"""测试公共夹具。"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """一个独立建好表结构的临时数据库。"""
    from lushu.store import initialize

    db_path = tmp_path / "lushu.db"
    initialize(db_path)
    return db_path


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把数据目录与库文件都指向临时路径，避免测试污染真实的 data/ 目录。"""
    from lushu import config
    from lushu.engine import reset_engine

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "SOURCES_DIR", data_dir / "sources")
    monkeypatch.setattr(config, "EXPORTS_DIR", data_dir / "exports")
    monkeypatch.setattr(config, "DB_PATH", data_dir / "lushu.db")
    monkeypatch.setattr(config, "CHECKPOINT_DB", data_dir / "checkpoints.db")

    # 引擎记得「已经为哪个库文件准备过」，换库之后必须清掉这个记忆
    reset_engine()
    return config


@pytest.fixture
def api_client(isolated_config):
    """跑完整生命周期的测试客户端（含表结构与引擎初始化）。"""
    from fastapi.testclient import TestClient

    from lushu.app import create_app

    with TestClient(create_app()) as client:
        yield client
