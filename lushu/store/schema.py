"""路书的表结构。

分七域：实体、素材、知识、预约、行程、运行、人工介入。
表的形状直接对应 docs/DESIGN.md 第三节，改动这里必须同步改那里。

迁移用 `PRAGMA user_version` 记录版本。每次变更追加一个迁移，不修改已发布的迁移。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .connection import connect

SCHEMA_VERSION = 1


# ─── 迁移 1：初始表结构 ────────────────────────────────────────
#
# 命名约定：
#   坐标系后缀   GCJ-02 是唯一存储格式，坐标字段一律带 `_gcj02` 后缀（ADR-0003）
#   时间字段     ISO 8601 文本，UTC 或带偏移
#   枚举字段     存英文标识符，中文展示名由前端负责
#
_MIGRATION_1 = """
-- ═══ 实体域 ═══════════════════════════════════════════════════
-- 硬事实的来源。攻略素材对这类字段只能标注差异，不能覆盖（ADR-0001）。

CREATE TABLE city (
    adcode       TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    lat_gcj02    REAL,
    lng_gcj02    REAL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE poi (
    -- 全系统的实体主键，来自高德（ADR-0002）
    amap_poi_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    city_adcode  TEXT REFERENCES city(adcode),
    adcode       TEXT,
    address      TEXT,
    lat_gcj02    REAL NOT NULL,
    lng_gcj02    REAL NOT NULL,
    tel          TEXT,
    type         TEXT,
    typecode     TEXT,
    raw_json     TEXT,
    fetched_at   TEXT NOT NULL
);
CREATE INDEX idx_poi_city ON poi(city_adcode);
CREATE INDEX idx_poi_name ON poi(name);

-- ═══ 素材域 ═══════════════════════════════════════════════════
-- 攻略素材全文长期留存为本地底档，只留在本地，路书只带片段。

CREATE TABLE source_group (
    -- 独立来源。置信度按本表计数，绝不按 source_document 计数（Q34）
    id           TEXT PRIMARY KEY,
    site         TEXT,
    author       TEXT,
    -- domain_author=同站同作者  similarity=正文相似度命中已有素材
    basis        TEXT NOT NULL CHECK(basis IN ('domain_author', 'similarity', 'single')),
    created_at   TEXT NOT NULL
);

CREATE TABLE source_document (
    id               TEXT PRIMARY KEY,
    site             TEXT NOT NULL,   -- mafengwo | zhihu | qiongyou | ctrip | xhs | manual
    url              TEXT,
    author           TEXT,
    title            TEXT,
    body_text        TEXT NOT NULL,   -- 全文底档
    body_sha256      TEXT NOT NULL,   -- 精确去重
    simhash          TEXT,            -- 近似去重，用于转载与洗稿归组
    source_group_id  TEXT REFERENCES source_group(id),
    published_at     TEXT,
    imported_at      TEXT NOT NULL,
    import_kind      TEXT NOT NULL CHECK(import_kind IN ('paste', 'file', 'fetch'))
);
CREATE INDEX idx_sd_group ON source_document(source_group_id);
CREATE INDEX idx_sd_sha ON source_document(body_sha256);
CREATE INDEX idx_sd_simhash ON source_document(simhash);

-- ═══ 知识域 ═══════════════════════════════════════════════════
-- 三层结论共用一张表，区别只在 subject_type 与 subject（Q26）。

CREATE TABLE claim (
    id                        TEXT PRIMARY KEY,
    subject_type              TEXT NOT NULL CHECK(subject_type IN ('poi', 'route', 'city')),
    -- 按 subject_type 三选一：poi 用 poi_id；route 用 poi_a_id+poi_b_id；city 用 city_adcode
    poi_id                    TEXT REFERENCES poi(amap_poi_id),
    poi_a_id                  TEXT REFERENCES poi(amap_poi_id),
    poi_b_id                  TEXT REFERENCES poi(amap_poi_id),
    city_adcode               TEXT REFERENCES city(adcode),
    -- avoid=避坑指南  highlight=打卡建议，两类分开建模分开呈现
    polarity                  TEXT NOT NULL CHECK(polarity IN ('avoid', 'highlight')),
    -- queue=排队 entrance=入口 hours=时段 crowd=人流 photo=拍照
    -- transit=交通 price_diff=票价差异 closure=闭馆 other=其它
    facet                     TEXT NOT NULL,
    text                      TEXT NOT NULL,
    confidence                TEXT NOT NULL CHECK(confidence IN ('high', 'single_source')),
    independent_source_count  INTEGER NOT NULL DEFAULT 0,
    status                    TEXT NOT NULL DEFAULT 'active'
                              CHECK(status IN ('active', 'needs_reverify', 'retired')),
    first_seen_at             TEXT NOT NULL,
    last_verified_at          TEXT,
    verify_due_at             TEXT
);
CREATE INDEX idx_claim_poi ON claim(poi_id);
CREATE INDEX idx_claim_city ON claim(city_adcode);
CREATE INDEX idx_claim_route ON claim(poi_a_id, poi_b_id);
CREATE INDEX idx_claim_status ON claim(status);

CREATE TABLE claim_evidence (
    -- 无溯源的结论不得入库，因此每条 claim 至少有一条 evidence
    id                  TEXT PRIMARY KEY,
    claim_id            TEXT NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    source_document_id  TEXT NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
    source_group_id     TEXT REFERENCES source_group(id),
    quote               TEXT NOT NULL,   -- 原文片段，进路书的就是它
    char_start          INTEGER,
    char_end            INTEGER,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_ce_claim ON claim_evidence(claim_id);

CREATE TABLE alignment_task (
    -- 待对齐：攻略提到的景点尚未对应到实体真源（Q19）
    id                  TEXT PRIMARY KEY,
    mention_name        TEXT NOT NULL,
    city_adcode         TEXT REFERENCES city(adcode),
    context_snippet     TEXT,
    source_document_id  TEXT REFERENCES source_document(id) ON DELETE SET NULL,
    candidate_pois_json TEXT,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'resolved', 'discarded')),
    resolved_poi_id     TEXT REFERENCES poi(amap_poi_id),
    created_at          TEXT NOT NULL,
    resolved_at         TEXT
);
CREATE INDEX idx_at_status ON alignment_task(status);

-- ═══ 预约域 ═══════════════════════════════════════════════════
-- status='draft' 的规则不得展示给用户（Q10）

CREATE TABLE booking_rule (
    poi_id             TEXT PRIMARY KEY REFERENCES poi(amap_poi_id) ON DELETE CASCADE,
    booking_required   INTEGER NOT NULL DEFAULT 0,
    advance_days       INTEGER,        -- 需提前几天
    release_time       TEXT,           -- 放票时点，如 "08:00" 或 "提前7天08:00"
    channels_json      TEXT,           -- [{name,url,kind}] kind: web|miniapp|official_account|phone
    requires_real_name INTEGER,
    id_required_note   TEXT,
    closed_days_json   TEXT,           -- 闭馆日：星期几或日期区间
    status             TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'reviewed')),
    evidence_url       TEXT,           -- 规则必须有渠道来源链接
    reviewed_at        TEXT,
    reviewer_note      TEXT,
    verify_due_at      TEXT,           -- 复验到期，默认入库后 90 天
    updated_at         TEXT NOT NULL
);

-- ═══ 行程域 ═══════════════════════════════════════════════════
-- 行程 → 城市停留[] → 天[]（ADR-0004）
-- 停车天数是用户输入，总行程天数是各 stay_days 之和，不由天列表反推

CREATE TABLE trip (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'confirmed')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE city_stay (
    id           TEXT PRIMARY KEY,
    trip_id      TEXT NOT NULL REFERENCES trip(id) ON DELETE CASCADE,
    city_adcode  TEXT NOT NULL REFERENCES city(adcode),
    city_name    TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    stay_days    INTEGER NOT NULL CHECK(stay_days > 0),
    UNIQUE(trip_id, seq)
);
CREATE INDEX idx_cs_trip ON city_stay(trip_id);

CREATE TABLE day (
    id            TEXT PRIMARY KEY,
    trip_id       TEXT NOT NULL REFERENCES trip(id) ON DELETE CASCADE,
    -- 每一天都归属某个城市停留，天气与预算的城市归属因此无歧义
    city_stay_id  TEXT NOT NULL REFERENCES city_stay(id) ON DELETE CASCADE,
    date          TEXT NOT NULL,
    seq_in_stay   INTEGER NOT NULL,
    UNIQUE(trip_id, date)
);
CREATE INDEX idx_day_stay ON day(city_stay_id);

CREATE TABLE day_item (
    id          TEXT PRIMARY KEY,
    day_id      TEXT NOT NULL REFERENCES day(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK(kind IN ('poi', 'meal', 'rest')),
    poi_id      TEXT REFERENCES poi(amap_poi_id),
    title       TEXT,
    start_time  TEXT,
    end_time    TEXT,
    note        TEXT,
    origin      TEXT NOT NULL DEFAULT 'ai' CHECK(origin IN ('ai', 'manual'))
);
CREATE INDEX idx_di_day ON day_item(day_id);

CREATE TABLE intercity_transfer (
    -- 挂在某一天上，该天归属某个城市停留（ADR-0004）
    id                  TEXT PRIMARY KEY,
    trip_id             TEXT NOT NULL REFERENCES trip(id) ON DELETE CASCADE,
    from_city_stay_id   TEXT NOT NULL REFERENCES city_stay(id) ON DELETE CASCADE,
    to_city_stay_id     TEXT NOT NULL REFERENCES city_stay(id) ON DELETE CASCADE,
    day_id              TEXT NOT NULL REFERENCES day(id) ON DELETE CASCADE,
    mode                TEXT NOT NULL,   -- rail | air | coach | drive | other
    service_no          TEXT,            -- 车次或航班号
    dep_time            TEXT,
    arr_time            TEXT,
    duration_min        INTEGER,
    price               REAL,
    price_source        TEXT NOT NULL DEFAULT 'estimate'
                        CHECK(price_source IN ('12306', 'estimate', 'manual')),
    is_reference_price  INTEGER NOT NULL DEFAULT 1,
    advice_reason       TEXT,            -- 规则给出的推荐理由
    alternatives_json   TEXT
);
CREATE INDEX idx_it_trip ON intercity_transfer(trip_id);
CREATE INDEX idx_it_day ON intercity_transfer(day_id);

CREATE TABLE leg_option (
    -- 同城相邻两个天项之间的移动方案（借鉴 Voyago 的 route 一等公民）
    id             TEXT PRIMARY KEY,
    day_id         TEXT NOT NULL REFERENCES day(id) ON DELETE CASCADE,
    from_item_id   TEXT NOT NULL REFERENCES day_item(id) ON DELETE CASCADE,
    to_item_id     TEXT NOT NULL REFERENCES day_item(id) ON DELETE CASCADE,
    options_json   TEXT NOT NULL,
    chosen_index   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_lo_day ON leg_option(day_id);

CREATE TABLE budget_item (
    id                  TEXT PRIMARY KEY,
    trip_id             TEXT NOT NULL REFERENCES trip(id) ON DELETE CASCADE,
    -- intercity=城际交通 local=市内交通 lodging=住宿 meal=餐饮 ticket=门票 other=其它
    category            TEXT NOT NULL
                        CHECK(category IN ('intercity', 'local', 'lodging', 'meal', 'ticket', 'other')),
    label               TEXT NOT NULL,
    amount              REAL NOT NULL,
    currency            TEXT NOT NULL DEFAULT 'CNY',
    is_reference_price  INTEGER NOT NULL DEFAULT 0,
    source              TEXT,
    note                TEXT
);
CREATE INDEX idx_bi_trip ON budget_item(trip_id);

-- ═══ 人工介入域 ═══════════════════════════════════════════════
-- 三类任务共用一个工作台页面，交互形状相同（Q47）

CREATE TABLE workbench_task (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK(kind IN ('align', 'review_booking', 'gold_label')),
    ref_type      TEXT,
    ref_id        TEXT,
    payload_json  TEXT,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending', 'done', 'discarded')),
    note          TEXT,
    created_at    TEXT NOT NULL,
    resolved_at   TEXT
);
CREATE INDEX idx_wt_kind_status ON workbench_task(kind, status);

CREATE TABLE gold_label (
    -- 金标准集：人工标注的素材结论，用于衡量提纯质量（Q42）
    id                  TEXT PRIMARY KEY,
    source_document_id  TEXT NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
    quote               TEXT NOT NULL,
    char_start          INTEGER,
    char_end            INTEGER,
    subject_type        TEXT NOT NULL CHECK(subject_type IN ('poi', 'route', 'city')),
    expected_poi_id     TEXT,
    polarity            TEXT NOT NULL CHECK(polarity IN ('avoid', 'highlight')),
    facet               TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_gl_doc ON gold_label(source_document_id);

-- ═══ 关于全文检索：这里刻意不建 FTS5 索引 ═══════════════════
-- 理由已实测，不是偷懒：
--
--   SQLite 的 unicode61 分词器把一整串连续汉字当成**一个词元**。于是
--   「故宫只有午门能进，北门只出不进」只有整段照抄才能命中，搜「午门」
--   是 0 结果——而这恰恰是最常见的查法。
--   换成 trigram 分词器可以子串匹配，但它要求查询至少 3 个字符，
--   两字词「午门」「故宫」「排队」「闭馆」全部查不到。
--
-- 我们真正的需求是「这段原文里有没有这句话」，语义上就是子串包含，
-- 所以直接 LIKE 扫描即可，对任意长度的中文查询都准确。自用规模
-- （数百到数千篇素材）下它足够快。检索入口见 lushu/store/search.py。
--
-- 若日后素材量增长到需要索引，正确的升级路径是先接一个中文分词器
-- 把正文预切好再喂给 unicode61 索引；直接上 FTS5 只会得到一个
-- 查不到两字词的索引（Q43）。
"""

_MIGRATIONS: dict[int, str] = {
    1: _MIGRATION_1,
}


def initialize(db_path: Path | str | None = None) -> int:
    """把数据库升级到最新表结构，返回升级后的版本号。

    幂等：已是最新版时什么都不做。
    """
    conn = connect(db_path)
    try:
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        for version in sorted(_MIGRATIONS):
            if version <= current:
                continue
            with conn:
                conn.executescript(_MIGRATIONS[version])
                conn.execute(f"PRAGMA user_version = {version}")
            current = version
        return current
    finally:
        conn.close()


def table_names(conn: sqlite3.Connection) -> set[str]:
    """列出业务表名，供测试与诊断用。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%'"
    ).fetchall()
    return {row["name"] for row in rows}
