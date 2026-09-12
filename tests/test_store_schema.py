"""表结构测试：七域表是否齐备、约束是否真的生效。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lushu.store import SCHEMA_VERSION, connect, find_documents_containing, initialize, transaction
from lushu.store.schema import table_names
from lushu.store.search import document_contains

EXPECTED_TABLES = {
    # 实体域
    "city",
    "poi",
    # 素材域
    "source_group",
    "source_document",
    # 知识域
    "claim",
    "claim_evidence",
    "alignment_task",
    # 预约域
    "booking_rule",
    # 行程域
    "trip",
    "city_stay",
    "day",
    "day_item",
    "intercity_transfer",
    "leg_option",
    "budget_item",
    # 人工介入域
    "workbench_task",
    "gold_label",
    # 运行与缓存（M3）
    "extraction_run",
    "llm_cache",
}


def test_migration_5_columns_exist(temp_db: Path) -> None:
    """迁移 5 加的三处列都要在。

    它们不是可有可无的补充：`parent_poi_id` 是认层级的前提（ADR-0009），
    `content_sha256` 是精确去重的前提，`group_score` 是
    「这两篇为什么算一个来源」的答案（ADR-0008）。
    """
    conn = connect(temp_db)
    try:
        poi_cols = {row["name"] for row in conn.execute("PRAGMA table_info(poi)")}
        doc_cols = {row["name"] for row in conn.execute("PRAGMA table_info(source_document)")}
    finally:
        conn.close()

    assert "parent_poi_id" in poi_cols
    assert {"content_sha256", "fetched_at", "group_score"} <= doc_cols


def test_extraction_run_counts_are_plain_integers(temp_db: Path) -> None:
    """提纯运行的计数列用来回答「抽了几条、丢了几条、为什么丢」。"""
    with transaction(temp_db) as conn:
        conn.execute(
            "INSERT INTO source_document (id, site, body_text, body_sha256, imported_at, import_kind) "
            "VALUES ('d1', 'manual', '正文', 'sha-1', '2026-09-12', 'paste')"
        )
        conn.execute(
            "INSERT INTO extraction_run (id, source_document_id, prompt_version, model, "
            "candidate_count, accepted_count, dropped_count, created_at) "
            "VALUES ('er1', 'd1', 'v1', 'deepseek-v4-flash', 14, 13, 1, '2026-09-12')"
        )
        row = conn.execute("SELECT * FROM extraction_run WHERE id = 'er1'").fetchone()

    assert row["candidate_count"] == 14
    assert row["accepted_count"] == 13
    assert row["dropped_count"] == 1
    assert row["status"] == "ok"


def test_extraction_run_status_is_constrained(temp_db: Path) -> None:
    with transaction(temp_db) as conn:
        conn.execute(
            "INSERT INTO source_document (id, site, body_text, body_sha256, imported_at, import_kind) "
            "VALUES ('d1', 'manual', '正文', 'sha-1', '2026-09-12', 'paste')"
        )

    conn = connect(temp_db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO extraction_run (id, source_document_id, prompt_version, model, "
                "status, created_at) VALUES ('er1', 'd1', 'v1', 'm', '半途而废', '2026-09-12')"
            )
    finally:
        conn.close()


def test_initialize_is_idempotent(temp_db: Path) -> None:
    """重复初始化不应报错，也不应改变版本。"""
    assert initialize(temp_db) == SCHEMA_VERSION
    assert initialize(temp_db) == SCHEMA_VERSION


def test_user_version_is_recorded(temp_db: Path) -> None:
    conn = connect(temp_db)
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
    finally:
        conn.close()


def test_all_domain_tables_exist(temp_db: Path) -> None:
    conn = connect(temp_db)
    try:
        missing = EXPECTED_TABLES - table_names(conn)
    finally:
        conn.close()
    assert not missing, f"缺少表：{sorted(missing)}"


def test_coordinate_columns_carry_the_crs_suffix(temp_db: Path) -> None:
    """ADR-0003：坐标字段必须显式带坐标系后缀，让「这是哪个系」无处可隐。"""
    conn = connect(temp_db)
    try:
        poi_cols = {row["name"] for row in conn.execute("PRAGMA table_info(poi)")}
        city_cols = {row["name"] for row in conn.execute("PRAGMA table_info(city)")}
    finally:
        conn.close()

    assert "lat_gcj02" in poi_cols and "lng_gcj02" in poi_cols
    assert "lat_gcj02" in city_cols and "lng_gcj02" in city_cols
    # 裸坐标字段不允许存在
    assert "lat" not in poi_cols and "lng" not in poi_cols
    assert "lat" not in city_cols and "lng" not in city_cols


def test_foreign_keys_are_enforced(temp_db: Path) -> None:
    """外键必须真的生效——级联删除依赖它。"""
    conn = connect(temp_db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO city_stay (id, trip_id, city_adcode, city_name, seq, stay_days) "
                "VALUES ('cs1', '不存在的行程', '110100', '北京', 0, 3)"
            )
    finally:
        conn.close()


def test_stay_days_must_be_positive(temp_db: Path) -> None:
    """停留天数的下限由数据库兜底，不只靠领域层。"""
    conn = connect(temp_db)
    try:
        conn.execute(
            "INSERT INTO city (adcode, name, updated_at) VALUES ('110100', '北京', '2026-09-12')"
        )
        conn.execute(
            "INSERT INTO trip (id, name, start_date, created_at, updated_at) "
            "VALUES ('t1', '京西行', '2026-10-01', '2026-09-12', '2026-09-12')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO city_stay (id, trip_id, city_adcode, city_name, seq, stay_days) "
                "VALUES ('cs1', 't1', '110100', '北京', 0, 0)"
            )
    finally:
        conn.close()


def test_claim_polarity_is_constrained(temp_db: Path) -> None:
    """避坑与打卡是仅有的两个极性，写错必须被拒绝。"""
    conn = connect(temp_db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO claim (id, subject_type, city_adcode, polarity, facet, text, "
                "confidence, first_seen_at) "
                "VALUES ('c1', 'city', '110100', '中性', 'other', '测试', 'high', '2026-09-12')"
            )
    finally:
        conn.close()


def test_source_search_finds_two_character_chinese_terms(temp_db: Path) -> None:
    """检索必须能搜到两字词。

    这正是 FTS5 做不到的地方：unicode61 会把整串汉字当成一个词元，
    trigram 又要求至少三个字符。所以这里用 LIKE 子串扫描，并把这个
    能力写成断言——它不是随手可改的实现细节。
    """
    with transaction(temp_db) as conn:
        conn.execute(
            "INSERT INTO source_document (id, site, title, body_text, body_sha256, imported_at, import_kind) "
            "VALUES ('d1', 'mafengwo', '北京三日', "
            "'故宫只有午门能进，北门只出不进。', 'sha-1', '2026-09-12', 'paste')"
        )

    hits = find_documents_containing("午门", conn=connect(temp_db))
    assert [h.document_id for h in hits] == ["d1"]
    assert "午门" in hits[0].snippet


def test_source_search_matches_title_too(temp_db: Path) -> None:
    with transaction(temp_db) as conn:
        conn.execute(
            "INSERT INTO source_document (id, site, title, body_text, body_sha256, imported_at, import_kind) "
            "VALUES ('d1', 'zhihu', '陕西历史博物馆怎么约', '正文里没有那四个字', "
            "'sha-2', '2026-09-12', 'paste')"
        )

    assert [h.document_id for h in find_documents_containing("陕西历史博物馆", conn=connect(temp_db))] == ["d1"]


def test_source_search_escapes_like_wildcards(temp_db: Path) -> None:
    """搜「100%」不能退化成匹配所有素材。"""
    with transaction(temp_db) as conn:
        conn.executemany(
            "INSERT INTO source_document (id, site, body_text, body_sha256, imported_at, import_kind) "
            "VALUES (?, 'manual', ?, ?, '2026-09-12', 'paste')",
            [
                ("d1", "旺季门票涨价 50% 是常态", "sha-a"),
                ("d2", "完全无关的另一篇攻略", "sha-b"),
            ],
        )

    hits = find_documents_containing("50%", conn=connect(temp_db))
    assert [h.document_id for h in hits] == ["d1"]


def test_source_search_ignores_blank_phrase(temp_db: Path) -> None:
    assert find_documents_containing("   ", conn=connect(temp_db)) == []


def test_document_contains_verifies_evidence_quotes(temp_db: Path) -> None:
    """证据的原文片段入库前要能校验——引用了原文里没有的话等于没有溯源。"""
    with transaction(temp_db) as conn:
        conn.execute(
            "INSERT INTO source_document (id, site, body_text, body_sha256, imported_at, import_kind) "
            "VALUES ('d1', 'mafengwo', '北门只出不进，别走错。', 'sha-1', '2026-09-12', 'paste')"
        )

    active = connect(temp_db)
    try:
        assert document_contains("北门只出不进", "d1", conn=active)
        assert not document_contains("南门只出不进", "d1", conn=active)
        assert not document_contains("北门只出不进", "不存在的素材", conn=active)
    finally:
        active.close()


def test_cascade_delete_removes_days(temp_db: Path) -> None:
    """删除行程应连带删掉它的城市停留与天。"""
    conn = connect(temp_db)
    try:
        conn.execute("INSERT INTO city (adcode, name, updated_at) VALUES ('110100', '北京', '2026-09-12')")
        conn.execute(
            "INSERT INTO trip (id, name, start_date, created_at, updated_at) "
            "VALUES ('t1', '京西行', '2026-10-01', '2026-09-12', '2026-09-12')"
        )
        conn.execute(
            "INSERT INTO city_stay (id, trip_id, city_adcode, city_name, seq, stay_days) "
            "VALUES ('cs1', 't1', '110100', '北京', 0, 2)"
        )
        conn.execute(
            "INSERT INTO day (id, trip_id, city_stay_id, date, seq_in_stay) "
            "VALUES ('d1', 't1', 'cs1', '2026-10-01', 0)"
        )
        conn.execute("DELETE FROM trip WHERE id = 't1'")
        remaining = conn.execute("SELECT COUNT(*) AS n FROM day").fetchone()["n"]
    finally:
        conn.close()
    assert remaining == 0
