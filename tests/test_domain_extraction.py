"""提纯领域模型的测试。

断言值来自 `scripts/probe_m3_extract.py` 的实跑输出：两篇素材抽出 28 条候选，
引文 28/28 是原文的**精确连续子串**。这里的样本照抄那两篇素材的片段。
"""

from __future__ import annotations

import pytest

from lushu.domain.extraction import (
    MIN_QUOTE_CHARS,
    ExtractedClaim,
    QuoteVerdict,
    locate_quote,
    verify_extraction,
)
from lushu.domain.knowledge import Facet, Polarity, SubjectType

# 实测素材的片段，含换行与直角引号，一字未改
BODY = """故宫现在只有午门能进，北门（神武门）只出不进。很多人从地铁天安门东站
出来以后跟着人流走，结果走到天安门城楼那边去了。
提前 7 天的晚上 8 点在
"故宫博物院"官方小程序放票。注意是晚上 8 点，不是零点。"""


def claim(quote: str, **kwargs: object) -> ExtractedClaim:
    defaults: dict[str, object] = {
        "subject_name": "故宫",
        "subject_type": SubjectType.POI,
        "polarity": Polarity.AVOID,
        "facet": Facet.ENTRANCE,
        "text": "只有午门能进",
        "quote": quote,
    }
    defaults.update(kwargs)
    return ExtractedClaim(**defaults)  # type: ignore[arg-type]


class TestLocateQuote:
    def test_exact_substring(self) -> None:
        location = locate_quote("故宫现在只有午门能进", BODY)

        assert location.verdict is QuoteVerdict.EXACT
        assert location.usable
        assert BODY[location.start : location.end] == "故宫现在只有午门能进"

    def test_exact_match_spans_newline_and_spaces(self) -> None:
        """模型保留了原文的换行与空格——实测它是原样照抄的。"""
        quote = "很多人从地铁天安门东站\n出来以后跟着人流走"

        location = locate_quote(quote, BODY)

        assert location.verdict is QuoteVerdict.EXACT

    def test_exact_match_keeps_straight_quotes(self) -> None:
        """原文里的直角引号 `"故宫博物院"` 也要原样保留才命中。"""
        location = locate_quote('"故宫博物院"官方小程序放票', BODY)

        assert location.verdict is QuoteVerdict.EXACT

    def test_loose_match_when_punctuation_differs(self) -> None:
        """模型改了标点、抹掉了数字周围的空格时仍能定位，但判定要如实记为 loose。

        这不是常态化路径：实测 28 条里一条都没有走到这里。
        出现 loose 说明该篇正文清洗有问题，是要看的信号。
        """
        location = locate_quote("注意是晚上8点不是零点", BODY)

        assert location.verdict is QuoteVerdict.LOOSE
        assert location.usable
        # 偏移要指向原文，不能指向抹平后的字符串
        assert BODY[location.start : location.end].startswith("注意是晚上")
        assert BODY[location.start : location.end].endswith("不是零点")

    def test_loose_match_does_not_reorder_content(self) -> None:
        """需要**改写**才能拼出的引文，宽松匹配也不该接受。

        「不是零点」在原文最后一句，「故宫现在只有午门能进」在开头。
        把它们接在一起当引文，是模型在替作者造句，不是改标点——
        这种引文一旦入库，「回查原文」就会指向一句作者没写过的话。
        """
        location = locate_quote("不是零点故宫现在只有午门能进", BODY)

        assert location.verdict is QuoteVerdict.MISSING
        assert not location.usable

    def test_loose_match_ignores_whitespace_differences(self) -> None:
        location = locate_quote("注意是晚上8点，不是零点", BODY)

        assert location.verdict in (QuoteVerdict.EXACT, QuoteVerdict.LOOSE)
        assert location.usable

    def test_missing_quote_is_rejected(self) -> None:
        """模型编了一句原文里没有的话——这是必须挡住的情形。"""
        location = locate_quote("故宫每天限流八万人需要提前十天预约", BODY)

        assert location.verdict is QuoteVerdict.MISSING
        assert not location.usable

    def test_too_short_quote_is_rejected(self) -> None:
        """「提前预约」四个字可以出现在任何一篇攻略里，证明不了什么。"""
        location = locate_quote("午门", BODY)

        assert location.verdict is QuoteVerdict.TOO_SHORT
        assert not location.usable
        assert MIN_QUOTE_CHARS > len("午门")

    def test_quote_is_stripped_before_matching(self) -> None:
        """模型偶尔会在引文首尾带上空白，那不是编造。"""
        location = locate_quote("  故宫现在只有午门能进\n", BODY)

        assert location.verdict is QuoteVerdict.EXACT

    def test_fullwidth_digits_are_not_normalized_away(self) -> None:
        """全角半角不做转换：原文里是两种写法，抹平了就查不出问题。"""
        body = "排队要等４０分钟，非常久"

        assert locate_quote("排队要等40分钟", body).verdict is QuoteVerdict.MISSING
        assert locate_quote("排队要等４０分钟", body).verdict is QuoteVerdict.EXACT


class TestExtractedClaim:
    def test_quote_is_required(self) -> None:
        with pytest.raises(ValueError, match="无引文"):
            claim(quote="   ")

    def test_subject_name_is_required(self) -> None:
        with pytest.raises(ValueError, match="主体名"):
            claim("故宫现在只有午门能进", subject_name="  ")

    def test_text_is_required(self) -> None:
        with pytest.raises(ValueError, match="结论不能为空"):
            claim("故宫现在只有午门能进", text="")


class TestVerifyExtraction:
    def test_keeps_grounded_candidates(self) -> None:
        candidates = [
            claim("故宫现在只有午门能进", facet=Facet.ENTRANCE),
            claim("提前 7 天的晚上 8 点在", facet=Facet.HOURS),
        ]

        outcome = verify_extraction(
            "d1", BODY, candidates, model="deepseek-v4-flash", prompt_version="v1"
        )

        assert len(outcome.accepted) == 2
        assert outcome.dropped == ()
        assert outcome.candidate_count == 2
        assert outcome.loose_count == 0

    def test_drops_fabricated_quotes(self) -> None:
        """引文对不上就丢弃，哪怕结论读起来完全合理。"""
        candidates = [
            claim("故宫现在只有午门能进"),
            claim("故宫每天限流八万人需要提前十天预约", text="每天限流八万"),
        ]

        outcome = verify_extraction(
            "d1", BODY, candidates, model="m", prompt_version="v1"
        )

        assert len(outcome.accepted) == 1
        assert len(outcome.dropped) == 1
        dropped_claim, verdict = outcome.dropped[0]
        assert verdict is QuoteVerdict.MISSING
        assert "限流八万" in dropped_claim.text

    def test_dropped_reasons_are_kept_per_candidate(self) -> None:
        """丢了几条、为什么丢都要留下——否则分不清「本来没内容」与「模型编了」。"""
        candidates = [
            claim("午门"),  # too_short
            claim("故宫每天限流八万人"),  # missing
            claim("故宫现在只有午门能进"),  # 通过
        ]

        outcome = verify_extraction("d1", BODY, candidates, model="m", prompt_version="v1")

        verdicts = {verdict for _, verdict in outcome.dropped}
        assert verdicts == {QuoteVerdict.TOO_SHORT, QuoteVerdict.MISSING}
        assert outcome.candidate_count == 3

    def test_loose_hits_are_counted(self) -> None:
        """loose 命中要能一眼看出有几条——它是正文清洗问题的信号。"""
        candidates = [
            claim("注意是晚上8点不是零点"),
            claim("故宫现在只有午门能进"),
        ]

        outcome = verify_extraction("d1", BODY, candidates, model="m", prompt_version="v1")

        assert outcome.loose_count == 1

    def test_empty_candidates(self) -> None:
        """一篇没有可抽内容的素材是正常结果，不是失败。"""
        outcome = verify_extraction("d1", BODY, [], model="m", prompt_version="v1")

        assert outcome.accepted == ()
        assert outcome.dropped == ()
        assert outcome.candidate_count == 0

    def test_offsets_are_relative_to_normalized_body(self) -> None:
        """偏移指向入库时保存的那份正文，否则回查会指错位置。

        `normalize_body` 会压掉开头与连续的空行，所以偏移必须基于
        归一化之后的结果算。
        """
        raw = "\n\n\n" + BODY + "\n\n\n"

        outcome = verify_extraction(
            "d1", raw, [claim("故宫现在只有午门能进")], model="m", prompt_version="v1"
        )

        verified = outcome.accepted[0]
        normalized = raw.strip()
        assert normalized[verified.location.start : verified.location.end] == "故宫现在只有午门能进"

    def test_run_metadata_is_carried_through(self) -> None:
        """模型与提示词版本要跟着结果走，否则「这条是怎么来的」答不上来。"""
        outcome = verify_extraction(
            "d1", BODY, [], model="deepseek-v4-flash", prompt_version="v3", duration_ms=4200
        )

        assert outcome.model == "deepseek-v4-flash"
        assert outcome.prompt_version == "v3"
        assert outcome.duration_ms == 4200
        assert outcome.document_id == "d1"
