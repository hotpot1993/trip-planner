"""金标准集与评测的取数与算账边界。

匹配规则本身在 `tests/test_domain_evaluation.py` 里测。这里守的是三件事：

1. 标注必须能落回原文（找不到的引文不许进金标准集）
2. 预测池取自提纯输出，不是落库的 claim——否则对齐失败会被算成抽取错误
3. 分母只含已标注的素材；「标完了但一条结论都没有」也要能表达
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lushu.services import evaluation as ev
from lushu.services import gold_store as gs
from lushu.services import knowledge_store as ks
from lushu.services.ingest import ImportRequest, import_document
from lushu.services.pipeline import align_pending, extract_documents
from lushu.store import connect, initialize, transaction
from tests.test_pipeline import (  # noqa: E402
    BEIJING_BODY,
    _patch_extract,
    a_claim,
    gugong,
    insert_poi,
    search_returning,
    wumen,
)

NOW = "2026-09-12T10:00:00"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "eval.db"
    initialize(path)
    return path


def _city(db: Path) -> None:
    with transaction(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO city (adcode, name, updated_at) VALUES ('110100', '北京', ?)",
            (NOW,),
        )


def _document(db: Path, body: str = BEIJING_BODY) -> str:
    conn = connect(db)
    try:
        return import_document(ImportRequest(body=body, title="北京故宫"), conn=conn).document_id
    finally:
        conn.close()


def _label(db: Path, document_id: str, **kwargs):
    with transaction(db) as conn:
        return gs.add_label(conn=conn, document_id=document_id, created_at=NOW, **kwargs)


class TestAddLabel:
    def test_offsets_are_computed_from_the_quote(self, db: Path) -> None:
        """偏移由程序算，人不用填——填错的位置会让评测静默错判。"""
        doc = _document(db)
        row = _label(
            db,
            doc,
            quote="故宫不卖现场票，全部要提前预约，",
            polarity="avoid",
            subject_name="故宫",
        )
        assert row.char_start is not None and row.char_end is not None
        body = BEIJING_BODY
        assert body[row.char_start : row.char_end] == "故宫不卖现场票，全部要提前预约，"
        assert row.verdict == "exact"

    def test_quote_not_in_body_is_rejected(self, db: Path) -> None:
        """找不到引文的标注不许进库。

        允许它等于允许金标准里混进凭印象写的条目，而金标准一旦不可信，
        后面所有指标都白算。
        """
        doc = _document(db)
        with pytest.raises(gs.GoldError, match="找不到"):
            _label(db, doc, quote="故宫每天限流八万人", polarity="avoid")

    def test_short_quote_is_rejected(self, db: Path) -> None:
        doc = _document(db)
        with pytest.raises(gs.GoldError, match="太短"):
            _label(db, doc, quote="故宫", polarity="avoid")

    def test_loose_quote_is_accepted_and_recorded(self, db: Path) -> None:
        """空白与全角半角差异不拦，但判定结果要如实记下来。"""
        doc = _document(db)
        row = _label(
            db,
            doc,
            quote="故宫现在只有午门能进, 北门（神武门）只出不进。",
            polarity="avoid",
        )
        assert row.verdict == "loose"
        assert row.char_start is not None

    def test_unknown_document_is_rejected(self, db: Path) -> None:
        with pytest.raises(gs.GoldError, match="没有这篇素材"):
            _label(db, "src_missing", quote="故宫现在只有午门能进", polarity="avoid")

    def test_bad_polarity_is_rejected(self, db: Path) -> None:
        doc = _document(db)
        with pytest.raises(gs.GoldError, match="极性"):
            _label(db, doc, quote="故宫现在只有午门能进", polarity="maybe")

    def test_remove_label(self, db: Path) -> None:
        doc = _document(db)
        row = _label(db, doc, quote="故宫现在只有午门能进", polarity="avoid")
        with transaction(db) as conn:
            assert gs.remove_label(conn=conn, label_id=row.label_id) is True
            assert gs.remove_label(conn=conn, label_id=row.label_id) is False
        assert gs.labels_for_document(doc, conn=connect(db)) == []


class TestUpdateLabel:
    def test_attach_poi_after_the_fact(self, db: Path) -> None:
        """标注是来回的：先写下事实，再去查这个提及指哪个景点。"""
        doc = _document(db)
        row = _label(db, doc, quote="故宫现在只有午门能进", polarity="avoid")

        with transaction(db) as conn:
            updated = gs.update_label(
                conn=conn, label_id=row.label_id, expected_poi_id="B000A8UIN8", set_poi=True
            )

        assert updated is not None
        assert updated.expected_poi_id == "B000A8UIN8"
        assert updated.quote == row.quote  # 引文与位置是锚点，改不了
        assert updated.char_start == row.char_start

    def test_omitting_set_poi_leaves_it_alone(self, db: Path) -> None:
        doc = _document(db)
        row = _label(
            db, doc, quote="故宫现在只有午门能进", polarity="avoid", expected_poi_id="B000A8UIN8"
        )
        with transaction(db) as conn:
            updated = gs.update_label(conn=conn, label_id=row.label_id, facet="entrance")
        assert updated is not None
        assert updated.expected_poi_id == "B000A8UIN8"
        assert updated.facet == "entrance"

    def test_set_poi_can_clear_it(self, db: Path) -> None:
        """「不改 POI」与「把 POI 清掉」是两件事，不能用同一个 None 表示。"""
        doc = _document(db)
        row = _label(
            db, doc, quote="故宫现在只有午门能进", polarity="avoid", expected_poi_id="B000A8UIN8"
        )
        with transaction(db) as conn:
            updated = gs.update_label(conn=conn, label_id=row.label_id, expected_poi_id=None, set_poi=True)
        assert updated is not None
        assert updated.expected_poi_id is None

    def test_unknown_label_returns_none(self, db: Path) -> None:
        with transaction(db) as conn:
            assert (
                gs.update_label(conn=conn, label_id="gl_x", expected_poi_id="B1", set_poi=True)
                is None
            )


class TestGoldSet:
    def test_document_with_zero_labels_can_be_marked_done(self, db: Path) -> None:
        """一篇读完全是废话的素材，零标注行恰恰是最重要的标注结果。"""
        doc = _document(db)
        with transaction(db) as conn:
            gs.mark_annotated(conn=conn, document_id=doc, annotated_at=NOW)

        documents = gs.gold_documents(conn=connect(db))
        assert documents[0].in_gold_set is True
        assert documents[0].labeled == 0

    def test_marking_is_idempotent_and_updates(self, db: Path) -> None:
        doc = _document(db)
        with transaction(db) as conn:
            gs.mark_annotated(conn=conn, document_id=doc, annotated_at=NOW)
            gs.mark_annotated(
                conn=conn,
                document_id=doc,
                model_output_seen=True,
                annotator="阿pot",
                annotated_at=NOW,
            )

        documents = gs.gold_documents(conn=connect(db))
        assert len(documents) == 1
        assert documents[0].model_output_seen is True

    def test_unmark_keeps_labels(self, db: Path) -> None:
        doc = _document(db)
        _label(db, doc, quote="故宫现在只有午门能进", polarity="avoid")
        with transaction(db) as conn:
            gs.mark_annotated(conn=conn, document_id=doc, annotated_at=NOW)
            assert gs.unmark_annotated(conn=conn, document_id=doc) is True

        assert gs.gold_documents(conn=connect(db))[0].in_gold_set is False
        assert len(gs.labels_for_document(doc, conn=connect(db))) == 1

    def test_unannotated_documents_come_first(self, db: Path) -> None:
        """工作台要能直接看出「下一篇标哪篇」。"""
        first = _document(db)
        second = _document(db, body=BEIJING_BODY + "\n补充一句不同的尾巴")
        with transaction(db) as conn:
            gs.mark_annotated(conn=conn, document_id=first, annotated_at=NOW)

        documents = gs.gold_documents(conn=connect(db))
        assert [item.document_id for item in documents] == [second, first]

    def test_stats(self, db: Path) -> None:
        doc = _document(db)
        _label(
            db,
            doc,
            quote="故宫现在只有午门能进",
            polarity="avoid",
            expected_poi_id="B000A8UIN8",
        )
        with transaction(db) as conn:
            gs.mark_annotated(conn=conn, document_id=doc, annotated_at=NOW)

        stats = gs.gold_stats(conn=connect(db))
        assert stats == {"documents": 1, "labels": 1, "with_poi": 1, "blind": 1}


class TestPredictionPool:
    def _extracted(self, db: Path, monkeypatch, *claims: dict) -> str:
        _city(db)
        with transaction(db) as conn:
            insert_poi(conn, gugong())
        doc = _document(db)
        _patch_extract(monkeypatch, *claims)
        with connect(db) as conn:
            extract_documents(conn=conn)
        return doc

    def test_predictions_come_from_extraction_output(self, db: Path, monkeypatch) -> None:
        doc = self._extracted(db, monkeypatch, a_claim())
        predictions = ev.predictions_for_document(doc, conn=connect(db))
        assert len(predictions) == 1
        assert predictions[0].subject_name == "故宫"
        assert predictions[0].char_start is not None

    def test_unaligned_predictions_are_still_counted(self, db: Path, monkeypatch) -> None:
        """对不上高德的候选**仍然在预测池里**。

        这正是预测池不能取 `claim` 表的原因：落库的 claim 只是对齐成功的
        那些，拿它当分母会把对齐失败算成抽取错误。
        """
        doc = self._extracted(db, monkeypatch, a_claim())

        with connect(db) as conn:
            align_pending(conn=conn, search=search_returning())  # 高德什么都没给
            assert conn.execute("SELECT COUNT(*) AS n FROM claim").fetchone()["n"] == 0

        assert len(ev.predictions_for_document(doc, conn=connect(db))) == 1

    def test_dropped_claims_are_not_predictions(self, db: Path, monkeypatch) -> None:
        """引文编的候选不算「抽到了」——它已经被校验挡住了。"""
        doc = self._extracted(
            db,
            monkeypatch,
            a_claim(),
            a_claim(text="每天限流八万", quote="故宫每天限流八万人需要提前十天预约"),
        )
        assert len(ev.predictions_for_document(doc, conn=connect(db))) == 1

    def test_missing_run_yields_no_predictions(self, db: Path) -> None:
        doc = _document(db)
        assert ev.predictions_for_document(doc, conn=connect(db)) == []

    def test_predictions_are_rebuilt_from_old_alignment_payloads(
        self, db: Path, monkeypatch
    ) -> None:
        """迁移 9 之前提纯过的篇，候选只留在待对齐队列里，要从那儿捡回来。"""
        doc = self._extracted(db, monkeypatch, a_claim())
        with transaction(db) as conn:
            conn.execute(
                "UPDATE extraction_run SET accepted_json = NULL WHERE source_document_id = ?",
                (doc,),
            )

        predictions = ev.predictions_for_document(doc, conn=connect(db))
        assert len(predictions) == 1
        assert predictions[0].subject_name == "故宫"

    def test_legacy_rebuild_unions_tasks_and_evidence(self, db: Path, monkeypatch) -> None:
        """老数据里候选散在两处：待办（对不上的）与证据行（已落库的）。

        只看待办会漏掉「主体名已人工处置过、因此不再开待办」的那些，
        所以两路都要捡，且要按主体名+文本+位置去重。
        """
        _city(db)
        with transaction(db) as conn:
            insert_poi(conn, gugong())
        doc = _document(db)
        _patch_extract(
            monkeypatch,
            a_claim(),
            a_claim(text="故宫 8 点半开门", quote="故宫早上 8 点半"),
            a_claim(text="不卖现场票", quote="故宫不卖现场票，全部要提前预约，"),
        )
        with connect(db) as conn:
            extract_documents(conn=conn)
            # 对齐一条 → 它进了 claim + claim_evidence
            align_pending(conn=conn, search=search_returning(gugong(), wumen()))

        # 把这一条对应的待办删掉，模拟「已处置过就不再开待办」
        with transaction(db) as conn:
            conn.execute(
                "DELETE FROM alignment_task WHERE source_document_id = ? AND mention_name = '故宫'",
                (doc,),
            )
            conn.execute(
                "UPDATE extraction_run SET accepted_json = NULL WHERE source_document_id = ?",
                (doc,),
            )

        predictions = ev.predictions_for_document(doc, conn=connect(db))
        quotes = {item.quote for item in predictions}
        assert "故宫不卖现场票，全部要提前预约，" in quotes  # 从证据行捡回来的
        assert len(predictions) == len(quotes)  # 没有重复计入

    def test_no_payload_anywhere_yields_nothing(self, db: Path, monkeypatch) -> None:
        doc = self._extracted(db, monkeypatch, a_claim())
        with transaction(db) as conn:
            conn.execute(
                "UPDATE extraction_run SET accepted_json = NULL WHERE source_document_id = ?",
                (doc,),
            )
            conn.execute("DELETE FROM alignment_task WHERE source_document_id = ?", (doc,))
        assert ev.predictions_for_document(doc, conn=connect(db)) == []

    def test_broken_json_falls_back_to_alignment_payloads(
        self, db: Path, monkeypatch
    ) -> None:
        """库里的 JSON 不能盲信：读不出来就退回待对齐队列，别让评测崩掉。"""
        doc = self._extracted(db, monkeypatch, a_claim())
        with transaction(db) as conn:
            conn.execute(
                "UPDATE extraction_run SET accepted_json = ? WHERE source_document_id = ?",
                ("{不是 JSON", doc),
            )
        predictions = ev.predictions_for_document(doc, conn=connect(db))
        assert len(predictions) == 1

    def test_wrong_shape_is_skipped(self, db: Path, monkeypatch) -> None:
        """存成字符串数组这种形状也要能读过去，而不是抛异常。"""
        doc = self._extracted(db, monkeypatch, a_claim())
        with transaction(db) as conn:
            conn.execute(
                "UPDATE extraction_run SET accepted_json = ? WHERE source_document_id = ?",
                (json.dumps(["字符串", 42]), doc),
            )
            conn.execute(
                "UPDATE alignment_task SET extracted_claims_json = ? WHERE source_document_id = ?",
                (json.dumps([{"subject_name": "故宫"}]), doc),
            )
        predictions = ev.predictions_for_document(doc, conn=connect(db))
        assert len(predictions) == 1
        assert predictions[0].quote == ""


class TestRunEvaluation:
    def _labeled_document(self, db: Path, monkeypatch, *claims: dict) -> str:
        """提纯过一遍、并且已记入金标准集的素材。

        标注与「记入金标准集」是两件事：只加标注不标记完成，这篇仍然
        不进评测——测试里必须两步都做，才走得通真实路径。
        """
        _city(db)
        with transaction(db) as conn:
            insert_poi(conn, gugong())
        doc = _document(db)
        _patch_extract(monkeypatch, *claims)
        with connect(db) as conn:
            extract_documents(conn=conn)
        with transaction(db) as conn:
            gs.mark_annotated(conn=conn, document_id=doc, annotated_at=NOW)
        return doc

    def test_end_to_end_three_metrics(self, db: Path, monkeypatch) -> None:
        doc = self._labeled_document(db, monkeypatch, a_claim())

        _label(
            db,
            doc,
            quote="故宫现在只有午门能进，北门（神武门）只出不进。",
            polarity="avoid",
            subject_name="故宫",
            expected_poi_id="B000A8UIN8",
        )
        # 标注了一条模型没抽到的
        _label(
            db,
            doc,
            quote="故宫不卖现场票，全部要提前预约，",
            polarity="avoid",
            subject_name="故宫",
        )

        with connect(db) as conn:
            align_pending(conn=conn, search=search_returning(gugong(), wumen()))
            bundle = ev.run_evaluation(conn=conn)

        report = bundle.report
        assert report.hits == 1
        assert report.gold_total == 2
        assert report.predicted_total == 1
        assert report.recall == pytest.approx(0.5)
        assert report.precision == pytest.approx(1.0)
        # 命中的那条标注给了 POI，预测也确实挂上了，所以判得出来且判对了
        assert report.alignment_accuracy == pytest.approx(1.0)
        assert bundle.stats["documents"] == 1
        assert bundle.ready is False

    def test_alignment_accuracy_uses_the_gold_poi(self, db: Path, monkeypatch) -> None:
        """对齐挂错了 POI：抽取仍然算命中，但准确率掉下来。"""
        doc = self._labeled_document(db, monkeypatch, a_claim())

        _label(
            db,
            doc,
            quote="故宫现在只有午门能进，北门（神武门）只出不进。",
            polarity="avoid",
            subject_name="故宫",
            expected_poi_id="B_WRONG",
        )
        with connect(db) as conn:
            align_pending(conn=conn, search=search_returning(gugong(), wumen()))
            report = ev.run_evaluation(conn=conn).report

        assert report.hits == 1
        assert report.alignment_accuracy == 0.0
        assert report.documents[0].pairing.misaligned
        assert report.unaligned == 0  # 挂上了，只是挂错了

    def test_unaligned_prediction_counts_as_alignment_failure(
        self, db: Path, monkeypatch
    ) -> None:
        """挂不上高德的那种：抽取算命中，对齐算错，但「没挂上」单独报出来。

        挂不上的结论对排程等于不存在，所以它在对齐准确率里必须算错；
        而错因是「高德没有这个 POI」还是「挂到了别处」是两回事，
        `unaligned` 这个计数就是把两者分开看的地方。
        """
        doc = self._labeled_document(db, monkeypatch, a_claim())
        _label(
            db,
            doc,
            quote="故宫现在只有午门能进，北门（神武门）只出不进。",
            polarity="avoid",
            subject_name="故宫",
            expected_poi_id="B000A8UIN8",
        )

        with connect(db) as conn:
            align_pending(conn=conn, search=search_returning())  # 高德什么都没给
            report = ev.run_evaluation(conn=conn).report

        assert report.hits == 1
        assert report.alignment_accuracy == 0.0
        assert report.unaligned == 1

    def test_documents_outside_the_gold_set_are_ignored(self, db: Path, monkeypatch) -> None:
        labeled = self._labeled_document(db, monkeypatch, a_claim())
        _document(db, body=BEIJING_BODY + "\n另一篇没有标注的素材")

        _label(db, labeled, quote="故宫现在只有午门能进", polarity="avoid")
        with transaction(db) as conn:
            gs.mark_annotated(conn=conn, document_id=labeled, annotated_at=NOW)

        with connect(db) as conn:
            bundle = ev.run_evaluation(conn=conn)

        assert bundle.report.sample_size == 1
        assert bundle.report.gold_total == 1

    def test_filter_by_document(self, db: Path, monkeypatch) -> None:
        doc = self._labeled_document(db, monkeypatch, a_claim())
        _label(db, doc, quote="故宫现在只有午门能进", polarity="avoid")
        with transaction(db) as conn:
            gs.mark_annotated(conn=conn, document_id=doc, annotated_at=NOW)

        with connect(db) as conn:
            assert ev.run_evaluation(conn=conn, document_ids=[]).report.sample_size == 0
            assert ev.run_evaluation(conn=conn, document_ids=[doc]).report.sample_size == 1

    def test_stats_include_label_counts(self, db: Path, monkeypatch) -> None:
        doc = self._labeled_document(db, monkeypatch, a_claim())
        _label(db, doc, quote="故宫现在只有午门能进", polarity="avoid")
        with transaction(db) as conn:
            gs.mark_annotated(conn=conn, document_id=doc, annotated_at=NOW)
        with connect(db) as conn:
            bundle = ev.run_evaluation(conn=conn)
        assert bundle.stats["labels"] == 1


class TestAlignmentTally:
    def test_counts_human_dispositions(self, db: Path, monkeypatch) -> None:
        """人工处置过的待办是免费的标注：选择与丢弃都要计数。"""
        _city(db)
        with transaction(db) as conn:
            insert_poi(conn, gugong())
        _document(db)
        _patch_extract(
            monkeypatch,
            a_claim(),
            a_claim(subject_name="排队", text="排队要两小时", quote="这一段路要走二十分钟左右。"),
        )
        with connect(db) as conn:
            extract_documents(conn=conn)
            align_pending(conn=conn, search=search_returning(gugong(), wumen()))

        with transaction(db) as conn:
            task = conn.execute(
                "SELECT id, mention_name FROM alignment_task WHERE status = 'pending'"
            ).fetchone()
            ks.resolve_alignment(
                conn=conn,
                task_id=task["id"],
                resolved_poi_id=None,
                status=ks.ALIGN_STATUS_DISCARDED,
                resolved_at=NOW,
            )

        tally = ev.alignment_tally(conn=connect(db))
        assert tally.resolved == 1
        assert tally.discarded == 1
        assert tally.discard_ratio == pytest.approx(0.5)

    def test_empty_tally_has_no_ratio(self, db: Path) -> None:
        tally = ev.alignment_tally(conn=connect(db))
        assert tally.total == 0
        assert tally.discard_ratio is None


class TestSubPoiAudit:
    """ADR-0009 体检。这条检查是金标准集第一次跑起来时逼出来的。"""

    def _claim_on(self, db: Path, poi_id: str) -> str:
        """直接插一行结论。

        不走 `save_claim`：它强制要求证据（无溯源的结论不得入库），
        而这个体检只看 `claim.poi_id` 与 `poi.parent_poi_id` 的关系，
        造证据只会把测试的重点冲淡。
        """
        claim_id = "clm_audit"
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO claim (id, subject_type, subject_name, poi_id, polarity, facet, "
                "text, confidence, independent_source_count, status, first_seen_at) "
                "VALUES (?, 'poi', '太和殿', ?, 'highlight', 'photo', "
                "'想拍没人的太和殿只能开门就冲', 'single_source', 1, 'active', ?)",
                (claim_id, poi_id, NOW),
            )
        return claim_id

    def test_catches_claim_attached_to_a_sub_poi(self, db: Path) -> None:
        from lushu.domain.poi import CandidatePoi

        sub = CandidatePoi(
            poi_id="B000A9PITC",
            name="故宫博物院-太和殿",
            typecode="110200",
            adcode="110101",
            city_name="北京市",
            parent_id="B000A8UIN8",
            lng_gcj02=116.397,
            lat_gcj02=39.916,
        )
        _city(db)
        with transaction(db) as conn:
            insert_poi(conn, gugong())
            insert_poi(conn, sub)
        self._claim_on(db, sub.poi_id)

        violations = ev.sub_poi_violations(conn=connect(db))
        assert len(violations) == 1
        assert violations[0].kind == "claim"
        assert violations[0].poi_id == "B000A9PITC"
        assert violations[0].root_id == "B000A8UIN8"
        assert violations[0].root_name == "故宫博物院"

    def test_root_attachments_are_clean(self, db: Path) -> None:
        _city(db)
        with transaction(db) as conn:
            insert_poi(conn, gugong())
        self._claim_on(db, gugong().poi_id)
        assert ev.sub_poi_violations(conn=connect(db)) == []

    def test_catches_task_resolved_to_a_sub_poi(self, db: Path) -> None:
        """待办也要查：它解析到子点时，落库出来的结论就是错的。"""
        from lushu.domain.poi import CandidatePoi

        sub = CandidatePoi(
            poi_id="B000A9PITC",
            name="故宫博物院-太和殿",
            typecode="110200",
            adcode="110101",
            city_name="北京市",
            parent_id="B000A8UIN8",
            lng_gcj02=116.397,
            lat_gcj02=39.916,
        )
        _city(db)
        with transaction(db) as conn:
            insert_poi(conn, gugong())
            insert_poi(conn, sub)
            conn.execute(
                "INSERT INTO alignment_task (id, mention_name, candidate_pois_json, status, "
                "resolved_poi_id, created_at, resolved_at) "
                "VALUES ('at_1', '太和殿', '[]', 'resolved', ?, ?, ?)",
                (sub.poi_id, NOW, NOW),
            )

        violations = ev.sub_poi_violations(conn=connect(db))
        assert [item.kind for item in violations] == ["task"]
