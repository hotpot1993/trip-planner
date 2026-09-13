"""数据链路编排的测试。

整条链路离线跑：提纯的模型是假的，高德的搜索与取数是注入的。
真实调用分别在 `scripts/verify_extract.py` 与 `scripts/verify_align.py` 里验证。

这里守的是四步之间的**边界**——哪一步能决定什么、不能决定什么：
提纯不能产生新事实、对齐失败的不许进知识库、归组必须在提纯之后、
置信度只按独立来源组计数。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lushu.adapters.poi import PoiSearchResult
from lushu.domain.knowledge import ClaimEvidence
from lushu.domain.poi import CandidatePoi
from lushu.services import knowledge_store as ks
from lushu.services.ingest import ImportRequest, import_document
from lushu.services.pipeline import (
    DEFAULT_CONCLUSION_OVERLAP,
    MIN_CONCLUSIONS_TO_COMPARE,
    align_pending,
    extract_documents,
    group_by_conclusions,
    merge_claims,
    pipeline_stats,
)
from lushu.store import connect, initialize, transaction

BEIJING_BODY = """北京旅行篇章：故宫参观攻略

故宫现在只有午门能进，北门（神武门）只出不进。正确的走法是从天安门东站B口
出来，穿过天安门城楼的门洞，再往前走到午门。这一段路要走二十分钟左右。

故宫不卖现场票，全部要提前预约，提前7天的晚上8点在官方小程序放票。
注意是晚上8点不是零点，我第一次等到零点白等了一场。

想拍没人的太和殿，唯一的办法是开门就冲。故宫早上8点半开门，
8点20左右午门外面就排起队了。"""

XIAN_BODY = """西安三天，博物馆和城墙

陕西历史博物馆是免费的，但是必须预约，而且要提前3天在官方公众号预约，
每天早上8点放票。注意陕历博周一闭馆，安排行程的时候要避开。

兵马俑不在西安市区，在临潼区，从西安北站坐地铁14号线再转9号线能到，
全程一个半小时。也可以坐游5路（306路）从火车站东广场直达，票价7块，
但是这个车路上会拉你去买玉，不要下车，坚持坐到终点。

城墙我推荐傍晚上去，南门上，租自行车骑一圈，全程13.7公里，一个半小时
左右。白天上城墙太晒了，城墙上没有什么遮阴的地方。"""


def gugong(*, parent: str | None = None) -> CandidatePoi:
    return CandidatePoi(
        poi_id="B000A8UIN8",
        name="故宫博物院",
        typecode="110201",
        type_name="风景名胜;风景名胜;世界遗产",
        adcode="110101",
        city_name="北京市",
        parent_id=parent,
        lng_gcj02=116.397,
        lat_gcj02=39.918,
    )


def wumen() -> CandidatePoi:
    return CandidatePoi(
        poi_id="B000A84GDN",
        name="故宫博物院-午门",
        typecode="110200",
        adcode="110101",
        city_name="北京市",
        parent_id="B000A8UIN8",
        lng_gcj02=116.397,
        lat_gcj02=39.913,
    )


def search_returning(*pois: CandidatePoi):
    def search(keywords: str, city: str) -> PoiSearchResult:
        return PoiSearchResult(candidates=tuple(pois), query=keywords, city=city,
                               raw_count=len(pois))

    return search


def fetch_returning(*pois: CandidatePoi):
    table = {poi.poi_id: poi for poi in pois}

    def fetch(poi_id: str) -> CandidatePoi | None:
        return table.get(poi_id)

    return fetch


class FakeLlm:
    """假的提纯客户端。按正文里出现的关键词决定吐什么。"""

    def __init__(self, *claims: dict) -> None:
        self.payload = {"claims": list(claims)}

    def invoke(self, messages: list[dict]) -> Any:
        from lushu.adapters.extract import ExtractionOut

        return ExtractionOut.model_validate(self.payload)


def llm_with(*claims: dict) -> FakeLlm:
    return FakeLlm(*claims)


def a_claim(**overrides: object) -> dict:
    base = {
        "subject_name": "故宫",
        "subject_type": "poi",
        "polarity": "avoid",
        "facet": "entrance",
        "text": "故宫只能从午门进入",
        "quote": "故宫现在只有午门能进，北门（神武门）只出不进。",
    }
    base.update(overrides)
    return base


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    initialize(path)
    return path


def seed_city(db: Path, adcode: str = "110100", name: str = "北京") -> None:
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO city (adcode, name, updated_at) VALUES (?, ?, '2026-09-12')",
            (adcode, name),
        )


def seed_poi(db: Path, poi: CandidatePoi, city_adcode: str = "110100") -> None:
    """把候选写进 poi 表。

    `claim.poi_id` 有外键指向 `poi`，所以对齐成功之前主体就得先存在——
    这也是 `align_pending` 落库前必须先写 poi 的原因。
    """
    with transaction(db) as conn:
        insert_poi(conn, poi, city_adcode)


def insert_poi(conn, poi: CandidatePoi, city_adcode: str = "110100") -> None:
    """在已有事务里插一个 POI。测试造数据用。"""
    conn.execute(
        "INSERT OR REPLACE INTO poi (amap_poi_id, name, city_adcode, adcode, typecode, "
        "lat_gcj02, lng_gcj02, parent_poi_id, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-09-12')",
        (
            poi.poi_id,
            poi.name,
            city_adcode,
            poi.adcode or city_adcode,
            poi.typecode,
            poi.lat_gcj02,
            poi.lng_gcj02,
            poi.parent_id,
        ),
    )


def seed_document(db: Path, body: str = BEIJING_BODY, **kwargs: object) -> str:
    """导入一篇素材。

    这里用普通连接而不是 `transaction()`，因为 `import_document` 自己提交
    （它是一个服务入口，调用方不该再管事务）。
    """
    conn = connect(db)
    try:
        result = import_document(ImportRequest(body=body, **kwargs), conn=conn)  # type: ignore[arg-type]
    finally:
        conn.close()
    return result.document_id


class TestExtractDocuments:
    def _prepare(self, db: Path, monkeypatch, *claims: dict) -> str:
        """导入一篇素材、装好假的模型，返回素材 id。

        POI 也一起造好：`claim.poi_id` 有外键指向 `poi`，
        而对齐成功之后落库需要这个主体已经存在。
        """
        seed_city(db)
        with transaction(db) as conn:
            insert_poi(conn, gugong())
        document_id = seed_document(db)
        _patch_extract(monkeypatch, *claims)
        return document_id

    def test_grounded_claims_are_parked_for_alignment(self, db: Path, monkeypatch) -> None:
        """提纯的产出先挂到待对齐队列上，不直接进知识库。"""
        doc = self._prepare(db, monkeypatch, a_claim())

        with connect(db) as conn:
            report = extract_documents(conn=conn)

        assert report.documents == 1
        assert report.accepted == 1
        assert report.dropped == 0

        with connect(db) as conn:
            tasks = ks.pending_alignments(conn=conn)
            claims = conn.execute("SELECT COUNT(*) AS n FROM claim").fetchone()["n"]

        assert len(tasks) == 1
        assert tasks[0].mention_name == "故宫"
        assert tasks[0].source_document_id == doc
        assert len(tasks[0].claims) == 1
        # 关键：还没进知识库
        assert claims == 0

    def test_fabricated_quote_is_dropped_and_counted(self, db: Path, monkeypatch) -> None:
        """引文对不上原文的直接丢弃（DESIGN 4.2），而且要留下计数。"""
        self._prepare(
            db,
            monkeypatch,
            a_claim(),
            a_claim(text="每天限流八万", quote="故宫每天限流八万人需要提前十天预约"),
        )

        with connect(db) as conn:
            report = extract_documents(conn=conn)

        assert report.accepted == 1
        assert report.dropped == 1
        assert report.candidates == 2

        with connect(db) as conn:
            stats = ks.extraction_stats(conn=conn)

        assert stats["dropped"] == 1
        assert stats["accepted"] == 1

    def test_claims_of_one_subject_share_a_single_task(self, db: Path, monkeypatch) -> None:
        """同一个主体名的多条结论合成一张待办。

        人工处置「故宫」一次就够了，不该为它的十三条结论开十三张待办。
        """
        self._prepare(
            db,
            monkeypatch,
            a_claim(text="只有午门能进"),
            a_claim(text="不卖现场票", quote="故宫不卖现场票，全部要提前预约，"),
            a_claim(text="8点半开门", quote="故宫早上8点半开门，"),
        )

        with connect(db) as conn:
            extract_documents(conn=conn)
            tasks = ks.pending_alignments(conn=conn)

        assert len(tasks) == 1
        assert len(tasks[0].claims) == 3

    def test_already_extracted_documents_are_skipped(self, db: Path, monkeypatch) -> None:
        """已经成功提纯过的不重复跑——重跑要花钱。"""
        self._prepare(db, monkeypatch, a_claim())
        calls: list[str] = []

        _patch_extract(monkeypatch, a_claim())
        import lushu.adapters.extract as extract_module

        inner = extract_module.extract

        def counting_extract(**kwargs):
            calls.append("call")
            return inner(**kwargs)

        monkeypatch.setattr("lushu.adapters.extract.extract", counting_extract)

        with connect(db) as conn:
            first = extract_documents(conn=conn)
            again = extract_documents(conn=conn)

        assert first.documents == 1
        assert len(calls) == 1
        assert again.documents == 0

    def test_force_reextracts_and_refreshes_the_payload(self, db: Path, monkeypatch) -> None:
        """`--force` 重跑一遍，把候选载荷补上。

        迁移 9 之前跑的篇在 `extraction_run.accepted_json` 上是空的，而评测
        要靠它当预测池。重跑**不花钱**（`llm_cache` 命中），所以这是补齐老数据
        的正路——评测那边从待办与证据行捡回来的兜底终究会漏掉个别候选。
        """
        self._prepare(db, monkeypatch, a_claim())
        with connect(db) as conn:
            extract_documents(conn=conn)

        # 抹掉载荷，模拟迁移 9 之前的老行
        with transaction(db) as conn:
            conn.execute("UPDATE extraction_run SET accepted_json = NULL")

        with connect(db) as conn:
            report = extract_documents(conn=conn, force=True)
            payload = conn.execute(
                "SELECT accepted_json FROM extraction_run ORDER BY rowid DESC LIMIT 1"
            ).fetchone()["accepted_json"]

        assert report.documents == 1
        assert payload is not None
        assert len(json.loads(payload)) == 1

    def test_force_does_not_duplicate_alignment_tasks(self, db: Path, monkeypatch) -> None:
        """重跑不该把同一批候选再开一遍待办——`_already_handled` 挡住它。"""
        self._prepare(db, monkeypatch, a_claim())
        with connect(db) as conn:
            extract_documents(conn=conn)
            extract_documents(conn=conn, force=True)
            tasks = conn.execute("SELECT COUNT(*) AS n FROM alignment_task").fetchone()["n"]

        assert tasks == 1

    def test_llm_failure_is_recorded_not_fatal(self, db: Path, monkeypatch) -> None:
        """一篇素材提纯失败不该中断整批，但要留下失败记录。"""
        from lushu.adapters.extract import ExtractionError

        self._prepare(db, monkeypatch, a_claim())

        def boom(**kwargs):
            raise ExtractionError("连接被重置")

        monkeypatch.setattr("lushu.adapters.extract.extract", boom)

        with connect(db) as conn:
            report = extract_documents(conn=conn, document_ids=[_only_document(conn)])

        assert len(report.failed) == 1
        with connect(db) as conn:
            row = conn.execute("SELECT status, error FROM extraction_run").fetchone()
        assert row["status"] == "failed"
        assert "连接被重置" in row["error"]

    def test_loose_hits_are_counted(self, db: Path, monkeypatch) -> None:
        """宽松命中要能一眼看出有几条——它是正文清洗问题的信号。"""
        self._prepare(
            db,
            monkeypatch,
            a_claim(quote="故宫现在只有午门能进,北门(神武门)只出不进."),
        )

        with connect(db) as conn:
            report = extract_documents(conn=conn)

        assert report.accepted == 1
        assert report.loose == 1


def _only_document(conn) -> str:
    return conn.execute("SELECT id FROM source_document LIMIT 1").fetchone()["id"]


def _patch_extract(monkeypatch, *claims: dict) -> None:
    """把 `lushu.adapters.extract.extract` 换掉，但保留它自己的校验逻辑。

    两个细节：

    - 载荷用 `_fake_payload` 这个**显式关键字**传进去，而不是靠闭包。
      若在替身里回调被替换掉的那个名字，就是无限递归——踩过一次。
    - `conn` 用 `kwargs.get("conn")` 取：`align_pending` 会用
      `extract(conn=conn)` 的写法调用它，不带 title。
    """
    from lushu.adapters.extract import extract as real_extract

    def patched(**kwargs):
        return real_extract(
            title=kwargs.get("title"),
            body=kwargs["body"],
            model="fake-model",
            conn=kwargs.get("conn"),
            llm=llm_with(*claims),
        )

    monkeypatch.setattr("lushu.adapters.extract.extract", patched)


class TestAlignPending:
    def _prepare(self, db: Path, monkeypatch, *claims: dict) -> None:
        seed_city(db)
        seed_poi(db, gugong())
        seed_document(db)
        _patch_extract(monkeypatch, *claims)
        with connect(db) as conn:
            extract_documents(conn=conn)

    def test_aligned_claim_lands_in_the_knowledge_base(self, db: Path, monkeypatch) -> None:
        self._prepare(db, monkeypatch, a_claim())

        with connect(db) as conn:
            report = align_pending(
                conn=conn,
                search=search_returning(gugong(), wumen()),
                fetch=fetch_returning(),
            )
            claims = ks.list_claims(conn=conn)

        assert report.aligned == 1
        assert len(claims) == 1
        assert claims[0].poi_id == "B000A8UIN8"
        assert claims[0].subject_name == "故宫"

    def test_unmatched_mention_stays_pending_with_candidates(self, db: Path, monkeypatch) -> None:
        """对不上的不许硬塞一个近似结果，要带着候选进待人工（Q19）。"""
        self._prepare(db, monkeypatch, a_claim(text="不倒翁表演取消", quote="想拍没人的太和殿，唯一的办法是开门就冲。"))

        with connect(db) as conn:
            report = align_pending(
                conn=conn,
                search=search_returning(),
                fetch=fetch_returning(),
            )
            claims = conn.execute("SELECT COUNT(*) AS n FROM claim").fetchone()["n"]
            tasks = ks.pending_alignments(conn=conn)

        assert report.pending == 1
        assert claims == 0
        assert tasks and tasks[0].task_id

    def test_search_failure_is_collected_not_fatal(self, db: Path, monkeypatch) -> None:
        from lushu.adapters.poi import PoiSearchError

        self._prepare(db, monkeypatch, a_claim())

        def boom(keywords: str, city: str):
            raise PoiSearchError("高德返回 HTTP 502")

        with connect(db) as conn:
            report = align_pending(conn=conn, search=boom, fetch=fetch_returning())

        assert len(report.failed) == 1
        assert "502" in report.failed[0][1]

    def test_mention_without_a_city_is_left_for_humans(self, db: Path, monkeypatch) -> None:
        """猜不出城市时不猜——挂到瞎猜的城市上就是跨城错误。"""
        # 刻意不往 city 表里放任何城市
        seed_document(db)
        _patch_extract(monkeypatch, a_claim())
        with connect(db) as conn:
            extract_documents(conn=conn)
            report = align_pending(
                conn=conn, search=search_returning(gugong()), fetch=fetch_returning()
            )

        assert report.unresolved_subjects == 1
        assert report.aligned == 0

    def test_sub_poi_mention_attaches_to_the_root(self, db: Path, monkeypatch) -> None:
        """搜「午门」时结论要挂到故宫博物院上（ADR-0009）。"""
        self._prepare(
            db,
            monkeypatch,
            a_claim(
                subject_name="午门",
                text="午门是唯一的入口",
                quote="故宫现在只有午门能进，北门（神武门）只出不进。",
            ),
        )

        with connect(db) as conn:
            report = align_pending(
                conn=conn,
                search=search_returning(wumen(), gugong()),
                fetch=fetch_returning(),
            )
            claims = ks.list_claims(conn=conn)

        assert report.aligned == 1
        assert report.collapsed == 1
        assert claims[0].poi_id == "B000A8UIN8"
        assert claims[0].subject_name == "午门"


class TestConfidence:
    def _seed_claims_from(self, db: Path, documents: list[tuple[str, str | None]]) -> None:
        """直接写结论与证据，跳过提纯与对齐——这一段测的是计数规则。

        有来源组的先建组行：`source_document.source_group_id` 是外键，
        造一个不存在的组 id 会被数据库挡下来（这是对的）。
        """
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO city (adcode, name, updated_at) VALUES ('110100', '北京', '2026-09-12')"
            )
            insert_poi(conn, gugong())
            for group_id in {group for _, group in documents if group}:
                conn.execute(
                    "INSERT INTO source_group (id, basis, created_at) VALUES (?, 'similarity', "
                    "'2026-09-12')",
                    (group_id,),
                )
            for document_id, group_id in documents:
                conn.execute(
                    "INSERT INTO source_document (id, site, body_text, body_sha256, imported_at, "
                    "import_kind, source_group_id) VALUES (?, 'manual', '正文', ?, '2026-09-12', "
                    "'paste', ?)",
                    (document_id, f"sha-{document_id}", group_id),
                )

    def test_three_independent_documents_make_it_high_confidence(self, db: Path) -> None:
        """三个独立来源算高置信（MIN_INDEPENDENT_SOURCES = 3）。"""
        docs = [(f"src_{i}", None) for i in range(3)]
        self._seed_claims_from(db, docs)

        with transaction(db) as conn:
            from lushu.domain.knowledge import ClaimEvidence

            claim_id = ks.save_claim(
                conn=conn,
                subject_type="poi",
                subject_name="故宫",
                poi_id="B000A8UIN8",
                polarity="avoid",
                facet="entrance",
                text="只有午门能进",
                evidence=[
                    ClaimEvidence(source_document_id=doc, quote="只有午门能进", source_group_id=None)
                    for doc, _ in docs
                ],
                first_seen_at="2026-09-12",
                verify_due_at="2027-03-11",
            )
            count, confidence = ks.recount_independent_sources(claim_id, conn=conn)

        assert count == 3
        assert confidence.value == "high"

    def test_three_reposts_count_as_one_source(self, db: Path) -> None:
        """**这条是整条链路最要紧的一条断言。**

        同一篇被三个站转载，只算一个独立来源。不这么做，置信度就退化成
        转发量，整个信任模型是假的（第十一节的风险表）。
        """
        docs = [(f"src_{i}", "grp_reposted") for i in range(3)]
        self._seed_claims_from(db, docs)

        with transaction(db) as conn:
            from lushu.domain.knowledge import ClaimEvidence

            claim_id = ks.save_claim(
                conn=conn,
                subject_type="poi",
                subject_name="故宫",
                poi_id="B000A8UIN8",
                polarity="avoid",
                facet="entrance",
                text="只有午门能进",
                evidence=[
                    ClaimEvidence(
                        source_document_id=doc,
                        quote="只有午门能进",
                        source_group_id=group,
                    )
                    for doc, group in docs
                ],
                first_seen_at="2026-09-12",
                verify_due_at="2027-03-11",
            )
            count, confidence = ks.recount_independent_sources(claim_id, conn=conn)

        assert count == 1
        assert confidence.value == "single_source"

    def test_append_evidence_ignores_duplicate_document(self, db: Path) -> None:
        """同一篇素材对同一条结论只算一条证据，重复追加不该让支持者看起来变多。"""
        self._seed_claims_from(db, [("src_a", None)])

        from lushu.domain.knowledge import ClaimEvidence

        with transaction(db) as conn:
            claim_id = ks.save_claim(
                conn=conn,
                subject_type="poi",
                subject_name="故宫",
                poi_id="B000A8UIN8",
                polarity="avoid",
                facet="entrance",
                text="只有午门能进",
                evidence=[
                    ClaimEvidence(source_document_id="src_a", quote="只有午门能进")
                ],
                first_seen_at="2026-09-12",
                verify_due_at=None,
            )
            added = ks.append_evidence(
                conn=conn,
                claim_id=claim_id,
                evidence=ClaimEvidence(source_document_id="src_a", quote="只有午门能进"),
                created_at="2026-09-13",
            )
            count, _ = ks.recount_independent_sources(claim_id, conn=conn)

        assert not added
        assert count == 1


class TestGroupByConclusions:
    def _two_documents_with_shared_conclusions(
        self, db: Path, *, shared: int, total_b: int
    ) -> tuple[str, str]:
        """造两篇素材：A 有 4 条结论，B 有 total_b 条，其中 shared 条与 A 相同。"""
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO city (adcode, name, updated_at) VALUES ('110100', '北京', '2026-09-12')"
            )
            insert_poi(conn, gugong())
            for doc in ("src_a", "src_b"):
                conn.execute(
                    "INSERT INTO source_document (id, site, body_text, body_sha256, imported_at, "
                    "import_kind) VALUES (?, 'manual', '正文', ?, '2026-09-12', 'paste')",
                    (doc, f"sha-{doc}"),
                )

            from lushu.domain.knowledge import ClaimEvidence

            def add(document: str, texts: list[str]) -> None:
                for text in texts:
                    claim_id = ks.save_claim(
                        conn=conn,
                        subject_type="poi",
                        subject_name="故宫",
                        poi_id="B000A8UIN8",
                        polarity="avoid",
                        facet="other",
                        text=text,
                        evidence=[
                            ClaimEvidence(source_document_id=document, quote="原文里的一句")
                        ],
                        first_seen_at="2026-09-12",
                        verify_due_at=None,
                    )
                    assert claim_id

            shared_texts = [f"共有的结论{i}" for i in range(shared)]
            add("src_a", shared_texts + ["A 独有的结论"])
            add("src_b", shared_texts + [f"B 独有的结论{i}" for i in range(total_b - shared)])
        return "src_a", "src_b"

    def test_rewritten_repost_is_merged(self, db: Path) -> None:
        """逐句改写的洗稿靠这一层认出来（第一层的文字复制认不出）。"""
        self._two_documents_with_shared_conclusions(db, shared=4, total_b=4)

        with connect(db) as conn:
            report = group_by_conclusions(conn=conn)
            rows = conn.execute(
                "SELECT id, source_group_id FROM source_document"
            ).fetchall()

        groups = {row["source_group_id"] for row in rows}
        # 两篇本来都没有组，所以是「新建一个组 + 另一篇并进来」两次动作；
        # 真正要断言的是它们最后落在同一个组里，而不是动作次数
        assert report.merged >= 1
        assert len(groups) == 1
        assert None not in groups
        assert report.groups == 1

    def test_independent_articles_are_not_merged(self, db: Path) -> None:
        """两位各写各的游客写同一批景点，结论天然重合——阈值必须挡住这类。"""
        self._two_documents_with_shared_conclusions(db, shared=3, total_b=10)

        with connect(db) as conn:
            report = group_by_conclusions(conn=conn)

        assert report.merged == 0

    def test_tiny_documents_do_not_participate(self, db: Path) -> None:
        """只抽出两条结论的素材与另一篇偶然重合两条就是 100%，那不是同源。"""
        self._two_documents_with_shared_conclusions(db, shared=2, total_b=2)

        with connect(db) as conn:
            report = group_by_conclusions(conn=conn)

        assert report.merged == 0
        assert MIN_CONCLUSIONS_TO_COMPARE > 2

    def test_threshold_is_much_stricter_than_the_text_layer(self) -> None:
        """第二层的阈值必须比第一层严：放宽会把独立来源错并、把高置信降级。"""
        from lushu.domain.similarity import DUPLICATE_COVERAGE

        assert DEFAULT_CONCLUSION_OVERLAP > DUPLICATE_COVERAGE


class TestMergeClaims:
    def test_duplicate_conclusions_are_merged_and_evidence_moved(self, db: Path) -> None:
        """同主体同正文的两条结论合成一条，证据不能丢（有 ON DELETE CASCADE）。"""
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO city (adcode, name, updated_at) VALUES ('110100', '北京', '2026-09-12')"
            )
            insert_poi(conn, gugong())
            for doc in ("src_a", "src_b", "src_c"):
                conn.execute(
                    "INSERT INTO source_document (id, site, body_text, body_sha256, imported_at, "
                    "import_kind) VALUES (?, 'manual', '正文', ?, '2026-09-12', 'paste')",
                    (doc, f"sha-{doc}"),
                )

        from lushu.domain.knowledge import ClaimEvidence

        with transaction(db) as conn:
            for doc in ("src_a", "src_b", "src_c"):
                ks.save_claim(
                    conn=conn,
                    subject_type="poi",
                    subject_name="故宫",
                    poi_id="B000A8UIN8",
                    polarity="avoid",
                    facet="entrance",
                    # 空格位置不同——归一化之后是同一条
                    text="只有午门能进" if doc != "src_c" else "只有午门 能进",
                    evidence=[
                        ClaimEvidence(
                            source_document_id=doc,
                            quote="只有午门能进",
                            # 三个文档各自独立：不给来源组，计数就按文档自身算
                            source_group_id=None,
                        )
                    ],
                    first_seen_at="2026-09-12",
                    verify_due_at=None,
                )

        with connect(db) as conn:
            report = merge_claims(conn=conn)
            claims = ks.list_claims(conn=conn)
            evidence = ks.evidence_for_claim(claims[0].claim_id, conn=conn)

        assert report.created == 1
        assert report.extended == 2
        assert len(claims) == 1
        assert len(evidence) == 3, "证据被级联删掉了——溯源就丢了"
        assert claims[0].independent_source_count == 3
        assert claims[0].confidence.value == "high"

    def test_different_polarity_is_not_merged(self, db: Path) -> None:
        """避坑与打卡分开建模（Q26），呈现方式不同，不能合并。"""
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO city (adcode, name, updated_at) VALUES ('110100', '北京', '2026-09-12')"
            )
            insert_poi(conn, gugong())
            for doc in ("src_a", "src_b"):
                conn.execute(
                    "INSERT INTO source_document (id, site, body_text, body_sha256, imported_at, "
                    "import_kind) VALUES (?, 'manual', '正文', ?, '2026-09-12', 'paste')",
                    (doc, f"sha-{doc}"),
                )
            for doc, polarity in (("src_a", "avoid"), ("src_b", "highlight")):
                ks.save_claim(
                    conn=conn,
                    subject_type="poi",
                    subject_name="故宫",
                    poi_id="B000A8UIN8",
                    polarity=polarity,
                    facet="entrance",
                    text="同一个正文但极性不同",
                    evidence=[
                        ClaimEvidence(source_document_id=doc, quote="原文", source_group_id=None)
                    ],
                    first_seen_at="2026-09-12",
                    verify_due_at=None,
                )

        with connect(db) as conn:
            merge_claims(conn=conn)
            n = conn.execute("SELECT COUNT(*) AS n FROM claim").fetchone()["n"]

        assert n == 2

    def test_claims_on_different_pois_are_not_merged(self, db: Path) -> None:
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO city (adcode, name, updated_at) VALUES ('110100', '北京', '2026-09-12')"
            )
            for poi_id, name in (("A1", "故宫博物院"), ("A2", "景山公园")):
                conn.execute(
                    "INSERT INTO poi (amap_poi_id, name, city_adcode, lat_gcj02, lng_gcj02, "
                    "fetched_at) VALUES (?, ?, '110100', 39.9, 116.4, '2026-09-12')",
                    (poi_id, name),
                )
            for doc in ("src_a", "src_b"):
                conn.execute(
                    "INSERT INTO source_document (id, site, body_text, body_sha256, imported_at, "
                    "import_kind) VALUES (?, 'manual', '正文', ?, '2026-09-12', 'paste')",
                    (doc, f"sha-{doc}"),
                )

        from lushu.domain.knowledge import ClaimEvidence

        with transaction(db) as conn:
            for doc, poi_id in (("src_a", "A1"), ("src_b", "A2")):
                ks.save_claim(
                    conn=conn,
                    subject_type="poi",
                    subject_name="同一个说法",
                    poi_id=poi_id,
                    polarity="avoid",
                    facet="other",
                    text="同一句话挂在两个景点上",
                    evidence=[
                        ClaimEvidence(source_document_id=doc, quote="原文", source_group_id=None)
                    ],
                    first_seen_at="2026-09-12",
                    verify_due_at=None,
                )

        with connect(db) as conn:
            merge_claims(conn=conn)
            n = conn.execute("SELECT COUNT(*) AS n FROM claim").fetchone()["n"]

        assert n == 2


class TestPipelineStats:
    def test_counts_every_stage(self, db: Path) -> None:
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO source_document (id, site, body_text, body_sha256, imported_at, "
                "import_kind) VALUES ('src_a', 'manual', '正文', 'sha', '2026-09-12', 'paste')"
            )

        with connect(db) as conn:
            stats = pipeline_stats(conn=conn)

        assert stats["documents"] == 1
        assert stats["claims"] == 0
        assert stats["align_pending"] == 0
