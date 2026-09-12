"""中文近重复检测的测试。

样本与 `scripts/probe_m3_grouping.py` 用的是同一批，**断言值就是实测值**，
见 `docs/M3-PROBE.md` 第二节。这些测试的作用不是「验证代码符合设计」，
而是「把实测结论钉住」——日后有人换成 SimHash 或调阈值，这里会先炸。
"""

from __future__ import annotations

import pytest

from lushu.domain.similarity import (
    DECISIVE_RUN_CHARS,
    DUPLICATE_COVERAGE,
    MIN_RUN_CHARS,
    is_duplicate,
    normalize_body,
    overlap,
)

ORIGINAL = """故宫现在只有午门能进，北门（神武门）只出不进。很多人从地铁天安门东站
出来以后跟着人流走，结果走到天安门城楼那边去了，那边是往天安门广场的，不是故宫入口。
正确的走法是从天安门东站B口出来，穿过天安门城楼的门洞，再往前走到午门。
这一段路要走二十分钟左右，夏天很晒。故宫不卖现场票，全部要提前预约，提前7天的
晚上8点在官方小程序放票，注意是晚上8点不是零点。周末和节假日的票基本是秒没，
建议提前把同行人的身份证号都填好。想拍没人的太和殿，唯一的办法是开门就冲。
故宫早上8点半开门，8点20左右午门外面就排起队了。故宫周边是禁停区，打车只能停到
比较远的地方，地铁1号线天安门东站和天安门西站都能到。"""

# 另一站全文照搬，只把「晚上8点」写成了「晚上八点」
REPOST_VERBATIM = ORIGINAL.replace("晚上8点不是零点", "晚上八点不是零点")

# 逐句改写：事实点全在，但没有一句和原文一样
REPOST_REWRITTEN = """去故宫前一定要搞清楚入口在哪。现在故宫只能从午门进，神武门是只出不进的。
天安门东站出来别跟着人流乱走，那样容易走到天安门城楼方向，那是去广场的。
从天安门东站B口出来，穿过城楼门洞往前就是午门，走路大概二十分钟，夏天特别晒。
门票方面，故宫现场是不卖票的，必须提前预约，官方小程序提前7天晚上8点放票，
不是零点。周末节假日基本秒没，最好先把同行人的身份证号填好。
拍照的话，想拍空无一人的太和殿只能一开门就冲进去。8点半开门，
8点20分午门外就开始排队了。周边禁停，打车停得远，坐1号线到天安门东或天安门西都可以。"""

# 转载时只摘了其中一段，前面加了编者按
REPOST_EXCERPT = """【转】故宫入口提示：
故宫现在只有午门能进，北门（神武门）只出不进。正确的走法是从天安门东站B口出来，
穿过天安门城楼的门洞，再往前走到午门。这一段路要走二十分钟左右，夏天很晒。
另外提醒：故宫不卖现场票，全部要提前预约。"""

# 另一位作者写的故宫，事实点有重合（这类最难判）
DIFFERENT_AUTHOR_A = """故宫门票要提前预约，这个大家都知道。我说点别的。
从午门进去以后，先别急着往太和殿走，右手边的武英殿人少，经常有书画展。
珍宝馆和钟表馆各10块钱，值得看。神武门出来正对景山，爬十分钟能拍到故宫全景，
这个角度比在宫里拍好多了。交通建议坐地铁8号线到金鱼胡同，从东华门进，
不过东华门只能出不能进，得绕到午门。"""

# 另一位作者写西安，与上面毫无关系
DIFFERENT_AUTHOR_B = """西安的陕历博必须提前3天预约，每天早上8点放票，周一闭馆。
兵马俑在临潼，门票120，现场不卖票。从西安北站坐14号线转9号线能到。
回民街商业化严重，不如去洒金桥。城墙傍晚上去，南门租自行车骑一圈13.7公里。"""


class TestDuplicateDetection:
    """同篇必须认出来。"""

    def test_verbatim_repost(self) -> None:
        result = overlap(ORIGINAL, REPOST_VERBATIM)
        assert result.coverage >= 0.95, f"照搬的覆盖率只有 {result.coverage:.3f}"
        assert result.is_duplicate

    def test_excerpt_repost(self) -> None:
        """节选转载：覆盖率的分母是较短一篇，所以「摘了一段」也判得出。"""
        result = overlap(ORIGINAL, REPOST_EXCERPT)
        assert result.coverage >= 0.80, f"节选的覆盖率只有 {result.coverage:.3f}"
        assert result.is_duplicate

    def test_long_decisive_run_alone_is_enough(self) -> None:
        """转载加了很长的编者按，覆盖率被稀释，但那一大段照搬是硬证据。"""
        padding = "编者按：" + "这里写了一大堆与本篇正文无关的背景介绍，" * 20
        padded = padding + REPOST_EXCERPT
        result = overlap(ORIGINAL, padded)

        assert result.longest_run >= DECISIVE_RUN_CHARS
        assert result.is_duplicate, f"覆盖率 {result.coverage:.3f}，最长片段 {result.longest_run}"

    def test_whitespace_differences_are_ignored(self) -> None:
        """同一段话从两个站点复制下来换行位置不同，不该因此判为不同来源。"""
        reflowed = ORIGINAL.replace("\n", "").replace("。", "。\n")
        assert overlap(ORIGINAL, reflowed).coverage >= 0.95


class TestDifferentSources:
    """异篇必须分开。这是危险方向的反面：错并会让两个独立来源算成一个。"""

    def test_same_city_different_author(self) -> None:
        result = overlap(ORIGINAL, DIFFERENT_AUTHOR_A)
        assert not result.is_duplicate, (
            f"两位不同作者被错并了：覆盖率 {result.coverage:.3f}，"
            f"最长片段 {result.longest_run}"
        )

    def test_unrelated_city(self) -> None:
        result = overlap(ORIGINAL, DIFFERENT_AUTHOR_B)
        assert result.coverage == pytest.approx(0.0, abs=0.01)
        assert not result.is_duplicate

    def test_real_world_measurements_are_not_regressed(self) -> None:
        """把实测的分离度钉住：异篇的覆盖率必须是 0。

        实测区间是同篇 [0.051, 0.852]、异篇 [0.000, 0.000]。
        这里只断言异篇那一侧，因为它是危险方向。
        """
        others = [DIFFERENT_AUTHOR_A, DIFFERENT_AUTHOR_B]
        samples = [ORIGINAL, REPOST_VERBATIM, REPOST_EXCERPT]
        for left in samples:
            for right in others:
                result = overlap(left, right)
                assert result.coverage == pytest.approx(0.0, abs=0.001), (
                    f"异篇出现了 {result.coverage:.4f} 的覆盖率"
                )


class TestKnownBlindSpot:
    """已知盲区要写成测试，不能只写在文档里。

    逐句改写的洗稿，LCS 覆盖与异篇一样低——本模块认不出来。
    这不是 bug，是这一层指标的边界；第二层（结论同源）才负责它。
    如果哪天这个测试失败了，说明指标变强了，应该去更新 ADR-0008。
    """

    def test_rewritten_repost_is_not_detected(self) -> None:
        result = overlap(ORIGINAL, REPOST_REWRITTEN)

        assert not result.is_duplicate, (
            "逐句改写被认出来了——这是好消息，但 ADR-0008 与 docs/M3-PROBE.md "
            "都记着它认不出来，请一并更新"
        )
        # 记下实际值，方便日后对比
        assert result.coverage < DUPLICATE_COVERAGE
        assert result.longest_run < MIN_RUN_CHARS * 2


class TestOverlapShape:
    def test_empty_text(self) -> None:
        assert overlap("", ORIGINAL).coverage == 0.0
        assert not is_duplicate("", ORIGINAL)

    def test_both_empty(self) -> None:
        result = overlap("", "")
        assert result.shorter_chars == 0
        assert not result.is_duplicate

    def test_identical(self) -> None:
        result = overlap(ORIGINAL, ORIGINAL)
        assert result.coverage == pytest.approx(1.0)
        assert result.longest_run >= len(ORIGINAL.replace("\n", "").replace(" ", "")) - 1

    def test_order_does_not_matter(self) -> None:
        """长短两篇谁在前都得到同一个判定。"""
        forward = overlap(ORIGINAL, REPOST_EXCERPT)
        backward = overlap(REPOST_EXCERPT, ORIGINAL)
        assert forward.coverage == pytest.approx(backward.coverage)
        assert forward.shorter_chars == backward.shorter_chars

    def test_short_shared_phrase_is_not_a_run(self) -> None:
        """十几个字的措辞偶合不算转载。实测异篇的公共片段都在 12 字以下。"""
        left = "西安的博物馆周一闭馆，安排行程的时候要避开这一天。"
        right = "西安的博物馆周一闭馆，所以我改去了城墙。"
        assert not is_duplicate(left, right)


class TestNormalizeBody:
    def test_collapses_blank_lines(self) -> None:
        assert normalize_body("第一段\n\n\n\n第二段") == "第一段\n\n第二段"

    def test_strips_trailing_whitespace_per_line(self) -> None:
        assert normalize_body("第一行   \n第二行\t\n") == "第一行\n第二行"

    def test_normalizes_crlf(self) -> None:
        assert normalize_body("甲\r\n乙\r丙") == "甲\n乙\n丙"

    def test_drops_leading_and_trailing_blank_lines(self) -> None:
        assert normalize_body("\n\n正文\n\n") == "正文"

    def test_does_not_touch_fullwidth_or_typos(self) -> None:
        """引文校验要求原文的连续子串，正文一旦被「顺手修正」就会失配。

        所以归一化刻意只做空白处理，不做全角半角转换，也不改错别字。
        """
        text = "从A口出来，走二十分种就到（２０分钟）"
        assert normalize_body(text) == text
