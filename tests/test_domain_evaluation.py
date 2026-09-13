"""金标准评测的匹配规则。

这个模块的测试重点不是「函数跑得通」，而是**规则在两个方向上都不出错**：
既不能因为措辞不同就把命中判成漏抽，也不能因为字面像就把矛盾判成命中。
两条都有真实素材里的例子垫底（docs/M3-PROBE.md）。
"""

from __future__ import annotations

import dataclasses

import pytest

from lushu.domain.evaluation import (
    EvalReport,
    GoldItem,
    Prediction,
    compare,
    evaluate,
    pair,
    same_fact,
    same_subject,
)


def _gold(**kwargs) -> GoldItem:
    base = {
        "label_id": "gl-1",
        "quote": "只有午门能进，北门只出不进",
        "polarity": "avoid",
        "subject_name": "故宫博物院",
    }
    base.update(kwargs)
    return GoldItem(**base)


def _pred(**kwargs) -> Prediction:
    base = {
        "subject_name": "故宫博物院",
        "polarity": "avoid",
        "text": "故宫只能从午门进，北门只出不进",
        "quote": "只有午门能进，北门只出不进",
    }
    base.update(kwargs)
    return Prediction(**base)


# ─── 主体 ────────────────────────────────────────────────────


def test_exact_name_is_same_subject() -> None:
    assert same_subject(_pred(), _gold())


def test_containment_counts_as_same_subject() -> None:
    """「故宫」与「故宫博物院」是同一个地方——对齐层认可，评测层也得认可。"""
    assert same_subject(_pred(subject_name="故宫"), _gold(subject_name="故宫博物院"))


def test_shortname_is_not_guessed_without_poi_id() -> None:
    """「陕历博」对「陕西历史博物馆」字符串上认不出来。

    这是**故意的**：靠字符串猜简称迟早会错，标注时选一下 POI 才是正路。
    """
    assert not same_subject(
        _pred(subject_name="陕历博"), _gold(subject_name="陕西历史博物馆")
    )


def test_shortname_matches_through_poi_id() -> None:
    assert same_subject(
        _pred(subject_name="陕历博", poi_id="B001"),
        _gold(subject_name="陕西历史博物馆", expected_poi_id="B001"),
    )


def test_same_name_different_poi_still_same_subject() -> None:
    """名字一样但挂到了不同的 POI：这仍然是同一个提及。

    挂错只该扣对齐准确率一笔，不该顺手把召回率和精确率也扣掉——
    一个错误出现在两个指标里，指标之间就不再正交。
    """
    prediction = _pred(subject_name="钟楼", poi_id="B001")
    gold = _gold(subject_name="钟楼", expected_poi_id="B002")
    assert same_subject(prediction, gold)
    verdict = compare(prediction, gold)
    assert verdict.hit
    assert verdict.poi_agrees is False


def test_missing_gold_name_falls_back_to_poi_id_only() -> None:
    assert not same_subject(_pred(), _gold(subject_name=None))


# ─── 极性 ────────────────────────────────────────────────────


def test_polarity_must_agree() -> None:
    """同一句话可以抽出避坑也可以抽出打卡，两者不是同一条（Q26）。"""
    verdict = compare(_pred(polarity="highlight"), _gold(polarity="avoid"))
    assert not verdict.hit
    assert "极性" in verdict.reason


# ─── 事实点 ──────────────────────────────────────────────────


def test_same_position_is_same_fact_regardless_of_wording() -> None:
    """措辞完全不同，但引的是原文同一句——这正是要认成命中的情况。"""
    prediction = _pred(
        text="午门是唯一入口",
        quote="只有午门能进",
        char_start=100,
        char_end=106,
    )
    gold = _gold(quote="只有午门能进，北门只出不进", char_start=100, char_end=114)
    assert same_fact(prediction, gold)


def test_different_position_but_same_text_still_counts() -> None:
    """原文把同一件事说了两遍：标注圈前一句、模型摘后一句，仍然算抽到。"""
    prediction = _pred(quote="只有午门能进", char_start=500, char_end=506)
    gold = _gold(quote="只有午门能进", char_start=100, char_end=106)
    assert same_fact(prediction, gold)


def test_contradicting_numbers_are_not_same_fact() -> None:
    """「门票 120」与「门票 100」字面相像却是矛盾的两条，不能算命中。"""
    prediction = _pred(
        text="门票 100 元",
        quote="门票 100 元",
        char_start=10,
        char_end=18,
    )
    gold = _gold(quote="门票 120 元", char_start=200, char_end=208)
    assert not same_fact(prediction, gold)


def test_nearby_facts_in_one_sentence_are_not_confused() -> None:
    """同段里两件事：排队时长与门票价格，位置不同就该分开判。"""
    prediction = _pred(quote="排队要两小时", char_start=10, char_end=16)
    gold = _gold(quote="排队要三小时", char_start=300, char_end=306)
    assert not same_fact(prediction, gold)


def test_uncited_prediction_falls_back_to_text() -> None:
    """没有位置的预测（老数据）只能靠文字比，重合度够就算命中。"""
    prediction = _pred(quote="只有午门能进，北门只出不进")
    assert same_fact(prediction, _gold())


# ─── 一对一配对 ──────────────────────────────────────────────


def test_pairing_counts_duplicate_extraction_as_spurious() -> None:
    """同一件事抽了两遍：一条算命中，另一条算多抽，精确率因此下降。"""
    gold = _gold()
    once = _pred()
    twice = _pred(text="午门是唯一入口")
    result = pair([once, twice], [gold])
    assert result.hits == 1
    assert len(result.spurious) == 1
    assert "重复" in result.spurious[0].reason


def test_pairing_reports_which_gate_failed() -> None:
    result = pair([_pred(subject_name="天安门")], [_gold()])
    assert result.hits == 0
    assert len(result.missed) == 1
    assert "主体不同" in result.spurious[0].reason


def test_pairing_without_predictions_is_all_missed() -> None:
    result = pair([], [_gold(), _gold(label_id="gl-2")])
    assert result.hits == 0
    assert len(result.missed) == 2
    assert not result.spurious


def test_spurious_without_gold_says_so() -> None:
    result = pair([_pred()], [])
    assert result.spurious[0].reason == "这篇素材没有任何标注"


# ─── 对齐准确率 ──────────────────────────────────────────────


def test_alignment_accuracy_counts_wrong_poi() -> None:
    gold = _gold(expected_poi_id="B001")
    good = _pred(poi_id="B001")
    report = evaluate({"d1": [good]}, {"d1": [gold]})
    assert report.alignment_accuracy == 1.0

    bad = _pred(poi_id="B999")
    report = evaluate({"d1": [bad]}, {"d1": [gold]})
    assert report.alignment_accuracy == 0.0
    assert report.documents[0].pairing.misaligned


def test_alignment_is_unjudged_when_gold_has_no_poi() -> None:
    """标注没给 POI 时判不了对齐——如实返回 None，不当成 0 也不当成 1。"""
    report = evaluate({"d1": [_pred(poi_id="B001")]}, {"d1": [_gold()]})
    assert report.alignment_accuracy is None
    assert report.documents[0].judged == 0


# ─── 三个指标 ────────────────────────────────────────────────


def test_precision_and_recall_are_micro_averaged() -> None:
    """一篇标 3 条抽到 1 条，一篇标 1 条抽到 1 条：按条数汇总，不按篇平均。"""
    golds = {
        "big": [_gold(label_id=f"gl-{i}") for i in range(3)],
        "small": [_gold(label_id="gl-s")],
    }
    predictions = {
        "big": [_pred()],
        "small": [_pred()],
    }
    report = evaluate(predictions, golds)
    assert report.hits == 2
    assert report.gold_total == 4
    assert report.predicted_total == 2
    assert report.recall == pytest.approx(0.5)
    assert report.precision == pytest.approx(1.0)


def test_unlabeled_documents_are_not_counted() -> None:
    """没进金标准集的素材不进分母——它有几条真结论没人知道。"""
    report = evaluate({"d1": [_pred()], "d2": [_pred()]}, {"d1": [_gold()]})
    assert report.sample_size == 1
    assert report.predicted_total == 1


def test_empty_gold_set_reports_none() -> None:
    report = evaluate({}, {})
    assert report.recall is None
    assert report.precision is None
    assert report.alignment_accuracy is None


def test_document_without_predictions_has_no_precision() -> None:
    report = evaluate({}, {"d1": [_gold()]})
    assert report.recall == 0.0
    assert report.precision is None
    assert report.documents[0].precision is None


def test_evaluation_is_reproducible() -> None:
    """同一份数据跑两次结果一致：配对是贪心且顺序固定的。"""
    golds = {"d1": [_gold(label_id="a"), _gold(label_id="b", quote="门票 120 元")]}
    predictions = {"d1": [_pred(), _pred(quote="门票 120 元", text="门票一百二")]}
    first = evaluate(predictions, golds)
    second = evaluate(predictions, golds)
    assert first == second


def test_report_is_immutable() -> None:
    report = EvalReport(documents=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.documents = ()  # type: ignore[misc]
