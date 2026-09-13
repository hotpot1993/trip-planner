"""路书的表结构。

分七域：实体、素材、知识、预约、行程、运行、人工介入。
表的形状直接对应 docs/DESIGN.md 第三节，改动这里必须同步改那里。

迁移用 `PRAGMA user_version` 记录版本。每次变更追加一个迁移，不修改已发布的迁移。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .connection import connect

SCHEMA_VERSION = 12


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

# ─── 迁移 2：补齐引擎产出里那些行程页要显示的字段 ────────────────
#
# 迁移 1 漏掉了几样东西：poi 表只有坐标与地址，但评分、开放时间、图片
# 同样是硬事实（只能来自官方接口，见 ADR-0001），而开放时间还要参与
# 时间可行性判断；day 表没有主题，trip 表没有用户原话。
# 加列在 SQLite 里不需要重建表，代价很低。
#
_MIGRATION_2 = """
ALTER TABLE poi ADD COLUMN rating REAL;
ALTER TABLE poi ADD COLUMN open_time TEXT;
ALTER TABLE poi ADD COLUMN photo_url TEXT;
ALTER TABLE day ADD COLUMN theme TEXT;
ALTER TABLE trip ADD COLUMN query TEXT;
"""

# ─── 迁移 3：城际转移补上站名与余票快照 ──────────────────────────
#
# 转移卡片要显示「南京南 → 西安北」。12306 的余票查询返回的是电报码，
# 站名是查出来之后才知道的，所以得存下来——否则每次展示都要重查一次。
# 余票状态是个快照，会变；存它并在界面上标注抓取时间，比不存更诚实。
#
_MIGRATION_3 = """
ALTER TABLE intercity_transfer ADD COLUMN from_station TEXT;
ALTER TABLE intercity_transfer ADD COLUMN to_station TEXT;
ALTER TABLE intercity_transfer ADD COLUMN has_tickets INTEGER;
"""

# ─── 迁移 4：城际转移补上说明 ────────────────────────────────────
#
# advice_reason 说的是「为什么推荐这种方式」，note 说的是「这份数据的可信度」——
# 票价是参考价、还没到放票期、车次没查到。两者读者不同，都必须在界面上看得见，
# 否则用户会把估价当成实价，或者以为这条线路没有铁路。
#
_MIGRATION_4 = """
ALTER TABLE intercity_transfer ADD COLUMN note TEXT;
"""

# ─── 迁移 5：M3 数据链路的落点 ───────────────────────────────────
#
# 分三块，都是为了把「这条结论是怎么来的」留成可回查的数据，而不是靠日志。
#
# 一、poi 补 parent_poi_id。M3 靠它认层级：`parent` 为空的才是本体，
#     结论一律挂本体（ADR-0009）。M1/M2 落库时没有这个字段，
#     因为引擎的 poi_to_spot() 把原始 POI 丢掉了（docs/M3-PROBE.md 第四节）。
#
# 二、source_document 补三类东西：
#       content_sha256  精确去重（body_sha256 留着不动，它已被索引引用）
#       fetched_at      抓取时间，与导入时间分开——批量囤稿时两者会差很远
#       group_score     归组时的重合度。实测 LCS 覆盖同篇 [0.051,0.852]、
#                       异篇 [0.000,0.000]（ADR-0008），把它留下，
#                       日后有人问「这两篇为什么算一个来源」才有据可查
#
# 三、加两张表：
#       extraction_run  每次提纯运行。跑过哪篇、用哪个模型、抽出几条、
#                       引文有几条回不到原文——「这条结论是怎么来的」的答案。
#       llm_cache       同一篇素材用同一个提示词重复提纯时直接复用。
#                       批量提纯要花钱，重跑是常事（改了一处解析、加了一个字段），
#                       不缓存等于每次重跑都重新付费。
#
# 刻意**没有**为 claim 加「待确认」状态：`workbench_task` 已经能承载
# 待办（用 ref_type='extraction' 区分自动任务与人工任务），再加一套状态
# 会让「一条 claim 现在处于什么状态」有两个答案。
#
_MIGRATION_5 = """
ALTER TABLE poi ADD COLUMN parent_poi_id TEXT;
CREATE INDEX idx_poi_parent ON poi(parent_poi_id);

ALTER TABLE source_document ADD COLUMN content_sha256 TEXT;
ALTER TABLE source_document ADD COLUMN fetched_at TEXT;
ALTER TABLE source_document ADD COLUMN group_score REAL;
CREATE INDEX idx_sd_content_sha ON source_document(content_sha256);

CREATE TABLE extraction_run (
    id                  TEXT PRIMARY KEY,
    source_document_id  TEXT NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
    prompt_version      TEXT NOT NULL,
    model               TEXT NOT NULL,
    candidate_count     INTEGER NOT NULL DEFAULT 0,   -- 模型吐出的条数
    accepted_count      INTEGER NOT NULL DEFAULT 0,   -- 引文校验通过的条数
    dropped_count       INTEGER NOT NULL DEFAULT 0,   -- 引文回不到原文而丢弃的条数
    input_chars         INTEGER,
    output_chars        INTEGER,
    duration_ms         INTEGER,
    group_id            TEXT REFERENCES source_group(id),
    status              TEXT NOT NULL DEFAULT 'ok'
                        CHECK(status IN ('ok', 'failed')),
    error               TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_er_doc ON extraction_run(source_document_id);
CREATE INDEX idx_er_created ON extraction_run(created_at);

CREATE TABLE llm_cache (
    -- 键是「提示词版本 + 模型 + 输入摘要」，换模型或改提示词都会自然失效
    cache_key    TEXT PRIMARY KEY,
    prompt_version TEXT NOT NULL,
    model        TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""

# ─── 迁移 6：claim 记住原文里的叫法 ───────────────────────────────
#
# ADR-0009 要求对齐结果显式记录「提及名」与「归并到的本体」两个字段。
# `poi_id` 是本体（结论挂在它上面），`subject_name` 是原文里的叫法——
# 可能是「陕历博」「午门」这类简称或子点。两者都留着，人工复核时才看得懂
# 「这条结论当时是从哪句话里来的、为什么挂到这个景点上」。
#
# 顺带加 claim_evidence 的 (source_group_id) 索引：置信度按独立来源组计数，
# 每次重算都要按组去重，是热路径。
#
_MIGRATION_6 = """
ALTER TABLE claim ADD COLUMN subject_name TEXT;
CREATE INDEX idx_ce_group ON claim_evidence(source_group_id);
"""

# ─── 迁移 7：把候选的原始载荷留在待办上 ───────────────────────────
#
# 提纯与对齐分两步跑，中间会有「提纯出了结论但还没对齐」的状态。
# 那个状态下结论全文只存在于内存里——脚本一结束就没了，
# 人工去处置待对齐时看不到「这条结论说的是什么」。
#
# 所以待办要带上完整的候选载荷，而不是只存一个提及名。
# 顺带把 `alignment_task` 的候选数上限固定为 10（记录时截断），
# 免得一条噪声查询把几万个候选塞进一行的 JSON 里。
#
_MIGRATION_7 = """
ALTER TABLE alignment_task ADD COLUMN extracted_claims_json TEXT;
"""

# ─── 迁移 8：来源组的归组依据多一个取值 ───────────────────────────
#
# 迁移 1 给 `basis` 定的取值只有 domain_author / similarity / single，
# 那是「文字复制」这一层的判据（照搬、节选）。M3 落地时补上了第二层——
# 按提纯结果的结论重合度判同源（ADR-0008）——它需要自己的取值，
# 否则写不进去（实测撞了 CHECK 约束）。
#
# `domain_author` 这个取值在实现里没被用到：第一层的实际判据是 LCS 覆盖，
# 也就是 similarity；域名与作者留着做人工复核的参考。保留它不动，
# 理由与所有已发布的迁移一样：不改写历史。
#
# **订正（M7）**：上面那句「没被用到」当时是错的——`ingest.import_document`
# 给转载建组时写的就是 `domain_author`，判据明明是 LCS 覆盖，标签却写着
# 「同站同作者」。现在导入与重跑两处都写 `similarity`，这句话才成立。
# 真库里当时一个组都没有，所以没有历史数据需要迁移。
#
# SQLite 不能直接改 CHECK 约束，只能重建表。这张表极小（一个来源组一行），
# 重建的代价可以接受。
#
_MIGRATION_8 = """
CREATE TABLE source_group_new (
    id           TEXT PRIMARY KEY,
    site         TEXT,
    author       TEXT,
    -- domain_author 与 similarity 是「文字复制」层（照搬、节选）的判据；
    -- conclusion_overlap 是「结论同源」层（逐句改写）的判据（ADR-0008）
    basis        TEXT NOT NULL
                 CHECK(basis IN ('domain_author', 'similarity', 'conclusion_overlap', 'single')),
    created_at   TEXT NOT NULL
);
INSERT INTO source_group_new (id, site, author, basis, created_at)
    SELECT id, site, author, basis, created_at FROM source_group;
DROP TABLE source_group;
ALTER TABLE source_group_new RENAME TO source_group;
"""

# ─── 迁移 9：金标准集与评测的落点 ─────────────────────────────────
#
# 三处，都是「要报出抽取精确率、召回率与对齐准确率」缺的东西：
#
# 一、`gold_label.subject_name`。迁移 1 建这张表时只记了 `expected_poi_id`，
#     那是**对齐之后**的答案。但标注发生在对齐之前：人看着原文写下「兵马俑
#     门票 120」时，脑子里的是「兵马俑」这个叫法，不一定顺手把 POI id 也选上。
#     没有提及名，比对预测时主体这一项就无从比起（ADR-0010）。
#
# 二、`gold_set`。金标准集必须是一个**明确的集合**，不能靠「有没有
#     gold_label 行」反推：一篇读完发现全是废话、一条真结论都没有的素材，
#     零标注行恰恰是最重要的标注结果（它进召回率的分母）。所以要显式记
#     「这篇我标注完了」，而不是让沉默同时表示「标完了没结论」和「还没标」。
#
#     顺带记下标注时**有没有看过模型输出**。看过后再标，人会不自觉地
#     只修模型给的东西而漏掉模型没抽到的——这是金标准最常见的偏差来源，
#     必须留在数据里，否则日后没法判断某个指标偏乐观是不是标注方式造成的。
#
# 三、`extraction_run.accepted_json`。评测的预测池需要一个稳定来源。
#     不能拿 `claim` 表当预测池：落库的 claim 只是「抽到且对齐上了」的那些，
#     把它当分母会把对齐失败算成抽取错误——那正是三个指标要分开的原因。
#     也不能拿 `alignment_task.extracted_claims_json`：那是按主体名归堆的
#     待办，处置过的会散落各处。提纯的输出就留在提纯自己的运行记录上。
#
_MIGRATION_9 = """
ALTER TABLE gold_label ADD COLUMN subject_name TEXT;
ALTER TABLE gold_label ADD COLUMN note TEXT;
-- 引文是原样命中还是宽松命中（空白与全角半角差异）。人对错误率的影响要看得见
ALTER TABLE gold_label ADD COLUMN quote_verdict TEXT;

CREATE TABLE gold_set (
    source_document_id  TEXT PRIMARY KEY REFERENCES source_document(id) ON DELETE CASCADE,
    annotator           TEXT,
    -- 标注时是否参考过模型输出：0 是盲标（先自己读原文写结论），
    -- 1 是看着候选增删。偏差方向不同，指标要分开看
    model_output_seen   INTEGER NOT NULL DEFAULT 0
                        CHECK(model_output_seen IN (0, 1)),
    note                TEXT,
    annotated_at        TEXT NOT NULL
);

ALTER TABLE extraction_run ADD COLUMN accepted_json TEXT;
"""

# ─── 迁移 10：天项自己记住坐标与地址 ─────────────────────────────
#
# 天项的坐标原先只存在 `poi` 表里，靠 `day_item.poi_id` 指过去。这对**景点**
# 是成立的——ADR-0002 说高德是实体真源，景点有 id、有坐标。
#
# 但餐饮不是。引擎的餐饮环节确实从高德周边搜索拿到了餐厅的坐标与地址
# （`restaurant_to_dict` 返回 `location` 与 `address`），却**没有 id**——
# 给餐厅编一个 id 塞进 `poi` 表是错的：那会污染实体表，让餐厅出现在候选池
# 与结论里。于是转换层只能把坐标丢掉，天项上就只剩一个店名。
#
# 后果在路书上看得见：导出时「这段路没有坐标，到当地问一下」出现了 7 次，
# 全部来自餐饮项——**中午从博物馆走多久能到那家店，行程里答不出来**。
#
# 所以坐标与地址要在天项上留一份。这是对 ADR-0001 的补充而不是违背：
# 它记的是「这个地方在哪儿」，来源仍然是官方接口。
#
_MIGRATION_10 = """
ALTER TABLE day_item ADD COLUMN address TEXT;
ALTER TABLE day_item ADD COLUMN lat_gcj02 REAL;
ALTER TABLE day_item ADD COLUMN lng_gcj02 REAL;
"""

# ─── 11：路段的真实距离 ─────────────────────────────────────────
#
# 路书里「这座博物馆到那家店怎么走」原先只有直线距离估算，页面上如实写着
# 「直线距离估算，实际路程更长」。那是诚实的，但不等于够用：真实步行距离
# 通常是直线的 1.3 倍，**写着「步行 1200 米」而实际要走 1800 米**，
# 就是设计里说的「现场会很意外」。
#
# 为什么不导出时现查：路书是**确认后导出的只读交付物**，导出这一步现在是
# 纯粹的读库（ADR-0006 的整套取舍都建立在「不必联网」上）。让导出去联网会
# 把它变成慢、不确定、且网断了就导不出的操作。所以照 `ls trip coords` 与
# `ls verify scan` 的先例：先跑一条命令查好写进库，导出照旧只读库。
#
# `leg_to_item_id` 是**自失效的键**：它记下这条路段通向哪一项。行程一改
# （插入、删除、重排），键就对不上，读的时候自然退回估算——不需要任何
# 「行程变了要清缓存」的额外记账，那种记账迟早会漏。
_MIGRATION_11 = """
ALTER TABLE day_item ADD COLUMN leg_mode TEXT;
ALTER TABLE day_item ADD COLUMN leg_distance_m INTEGER;
ALTER TABLE day_item ADD COLUMN leg_duration_min INTEGER;
ALTER TABLE day_item ADD COLUMN leg_to_item_id TEXT;
"""

# ─── 12：路段的键要覆盖两端坐标 ─────────────────────────────────
#
# 11 用的键是「通向哪一项」。它挡住了行程重排，**挡不住坐标变化**：
# `ls align recheck` 会把天项挪到另一个实体上（`UPDATE day_item SET poi_id`），
# 坐标随之改变而 id 没变，于是那段存下来的距离还在，只是已经不是这
# 两个地方之间的距离了——一条错的距离，在路书上看起来和一个对的一模一样。
#
# 所以键换成**指纹**：两端的坐标加上通向哪一项。距离只由两端坐标决定，
# 键覆盖全部依赖，就没有「悄悄过期」的余地。
#
# `leg_to_item_id` 由 `leg_key` 完全包含（指纹尾巴上就是它），删掉不留死列。
_MIGRATION_12 = """
ALTER TABLE day_item ADD COLUMN leg_key TEXT;
ALTER TABLE day_item DROP COLUMN leg_to_item_id;
"""

_MIGRATIONS: dict[int, str] = {
    1: _MIGRATION_1,
    2: _MIGRATION_2,
    3: _MIGRATION_3,
    4: _MIGRATION_4,
    5: _MIGRATION_5,
    6: _MIGRATION_6,
    7: _MIGRATION_7,
    8: _MIGRATION_8,
    9: _MIGRATION_9,
    10: _MIGRATION_10,
    11: _MIGRATION_11,
    12: _MIGRATION_12,
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
