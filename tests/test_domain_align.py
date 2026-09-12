"""实体对齐的离线测试。

样本全部来自 `scripts/probe_m3_align.py` 与 `scripts/probe_m3_poi_parent.py`
的**真实高德返回**，不是编的。字段值与返回顺序都照抄，这样测试才能守住
那些实测出来的规则（城市硬门禁、类型分类、parent 归并）。
"""

from __future__ import annotations

import pytest

from lushu.domain.align import (
    AlignOutcome,
    Mention,
    align,
    name_score,
    score_candidates,
)
from lushu.domain.poi import CandidatePoi, PoiKind


def poi(poi_id: str, name: str, **kwargs: object) -> CandidatePoi:
    """构造一个候选，默认是北京的一个正常景点。"""
    defaults: dict[str, object] = {
        "typecode": "110200",
        "type_name": "风景名胜;风景名胜;风景名胜",
        "adcode": "110101",
        "city_name": "北京市",
    }
    defaults.update(kwargs)
    return CandidatePoi(poi_id=poi_id, name=name, **defaults)  # type: ignore[arg-type]


# 搜「故宫」时高德实际返回的前五条（scripts/probe_m3_poi_parent.py 实测）
GUGONG_RESULTS = [
    poi("B000A8UIN8", "故宫博物院", typecode="110201", type_name="风景名胜;风景名胜;世界遗产"),
    poi("B000A84GDN", "故宫博物院-午门", parent_id="B000A8UIN8"),
    poi("B0FFKL520U", "故宫博物院检票处", typecode="070000", type_name="生活服务;生活服务场所"),
    poi("B0FFFT7UKC", "故宫博物院-文华殿", parent_id="B000A8UIN8"),
    poi("B0FFKP9Q91", "故宫博物院-慈宁宫花园", typecode="110000", parent_id="B000A8UIN8"),
]

BEIJING = Mention(name="故宫", city_name="北京", city_adcode="110100")


class TestNameScore:
    """名称得分的规则。这些断言值就是对 `scripts/probe_m3_align.py` 实测返回的复现。"""

    @pytest.mark.parametrize(
        ("mention", "candidate", "floor"),
        [
            ("故宫", "故宫", 1.0),
            ("故宫", "故宫博物院", 0.90),  # 包含关系，实测首位就是这个
            ("兵马俑", "秦始皇兵马俑博物馆", 0.80),
            ("景山", "景山公园", 0.95),  # 去掉通名后缀后相同
            ("西安城墙", "西安城墙", 1.0),
            ("大唐不夜城", "大唐不夜城(地铁站)", 0.85),
            ("陕西历史博物馆", "陕西体育博物馆", 0.00),  # 只共有一个「博物馆」
        ],
    )
    def test_scores(self, mention: str, candidate: str, floor: float) -> None:
        assert name_score(mention, candidate) >= floor

    def test_name_after_separator_scores_high(self) -> None:
        """搜「午门」，答案是 `故宫博物院-午门`。

        「午门」只有两个字，按整体包含判断会被短词阈值挡掉，
        所以分隔符那一条要单独判（ADR-0009 的归并全靠它）。
        """
        assert name_score("午门", "故宫博物院-午门") >= 0.80

    def test_name_after_separator_does_not_match_a_longer_segment(self) -> None:
        """分隔符后的那一段必须是完整的，否则「午门」会命中「午门西卫生间」。"""
        assert name_score("午门", "故宫博物院-午门西卫生间") < 0.80

    def test_short_containment_does_not_score_high(self) -> None:
        """「西安」出现在几百个候选名里，包含关系对这种短词没有意义。"""
        assert name_score("西安", "西安钟楼") < 0.80

    def test_unrelated_names_score_low(self) -> None:
        assert name_score("回民街", "秦始皇兵马俑博物馆第1停车场") < 0.30

    def test_huimin_street_beats_huimin_street_homestay(self) -> None:
        assert name_score("回民街", "回民街") > name_score("回民街", "西安回民街家庭民宿")

    def test_official_name_of_an_alias_shares_nothing(self) -> None:
        """俗称与官方名的字面重合度可以低到零。

        「兵马俑」对「秦始皇帝陵博物院」的 SequenceMatcher 比例是 0.00，
        而它就是答案。这条断言把「字面像不像 ≠ 是不是它」钉住：
        真要对上它，靠的是别名（高德的排序）或包含关系，不是字符串相似度。
        """
        assert name_score("兵马俑", "秦始皇帝陵博物院") == 0.0
        assert name_score("陕历博", "陕西历史博物馆") < 0.50


class TestAlign:
    def test_exact_match_on_root(self) -> None:
        result = align(BEIJING, GUGONG_RESULTS)

        assert result.outcome is AlignOutcome.ALIGNED
        assert result.resolved is not None
        assert result.resolved.name == "故宫博物院"
        assert result.resolved_poi_id == "B000A8UIN8"
        assert not result.collapsed_from_sub

    def test_sub_poi_collapses_to_root(self) -> None:
        """搜「午门」时返回的正是子点，要沿 parent 归并到故宫博物院（ADR-0009）。

        不归并的后果是生成一个只有一条结论、无法参与路线排序的孤立 POI。
        """
        candidates = [
            poi("B000A84GDN", "故宫博物院-午门", parent_id="B000A8UIN8"),
            poi("B000A8UIN8", "故宫博物院", typecode="110201"),
            poi("B0FFFT7UKC", "午门文创随展馆", typecode="060101",
                type_name="购物服务;购物相关场所", adcode="110101"),
        ]
        result = align(Mention(name="午门", city_name="北京", city_adcode="110100"), candidates)

        assert result.outcome is AlignOutcome.ALIGNED
        assert result.resolved is not None
        assert result.resolved.poi_id == "B000A8UIN8"
        assert result.matched is not None
        assert result.matched.poi_id == "B000A84GDN"
        assert result.collapsed_from_sub

    def test_collapse_walks_multiple_hops(self) -> None:
        """实测的链是三跳，只有最后一跳的 parent 是空的。

        `秦始皇兵马俑博物馆第1停车场`（B001D0095C）→ `秦始皇兵马俑博物馆`
        （B0FFGXMLTU）→ `秦始皇帝陵博物院`（B001D09OYW）。
        走一步不算归并——只走一步会把本体认成「秦始皇兵马俑博物馆」。
        """
        candidates = [
            poi("B001D09OYW", "秦始皇帝陵博物院", typecode="110202", adcode="610115"),
            poi("B0FFGXMLTU", "秦始皇兵马俑博物馆", typecode="140100", parent_id="B001D09OYW",
                adcode="610115"),
            poi("B001D0095C", "秦始皇兵马俑博物馆第1停车场", typecode="150904",
                parent_id="B0FFGXMLTU", adcode="610115"),
        ]
        result = align(
            Mention(name="秦始皇兵马俑博物馆", city_name="西安", city_adcode="610100"),
            candidates,
        )

        assert result.resolved is not None
        assert result.resolved.poi_id == "B001D09OYW"

    def test_missing_parent_is_not_collapsed_silently(self) -> None:
        """父节点不在候选里时只能留在子点上，但必须让调用方看出来。"""
        candidates = [poi("B000A84GDN", "故宫博物院-午门", parent_id="B000A8UIN8")]
        result = align(Mention(name="午门", city_name="北京", city_adcode="110100"), candidates)

        assert result.outcome is AlignOutcome.ALIGNED
        assert result.resolved is not None
        assert result.resolved.poi_id == "B000A84GDN"
        assert not result.collapsed_from_sub

    def test_cross_city_candidate_is_rejected(self) -> None:
        """「袁家村」限定西安，实际前五全是西安市区的连锁中餐馆。

        这是最危险的失败模式：名称完全一致，城市完全错误。
        """
        restaurants = [
            poi(f"R{i}", "袁家村", typecode="050100", type_name="餐饮服务;中餐厅;中餐厅",
                adcode="610112")
            for i in range(5)
        ]
        result = align(
            Mention(name="袁家村", city_name="西安", city_adcode="610100"),
            restaurants,
        )

        # 名称一致但类型不是目的地，所以进不了候选
        assert result.outcome is AlignOutcome.NO_CANDIDATE
        assert "类型不是目的地" in result.reason

    def test_city_gate_rejects_before_name_score_matters(self) -> None:
        """搜「故宫」传 adcode 时高德返回的是廊坊的故宫文创店（实测）。

        名称包含「故宫」所以得分不低，但城市不对，必须被城市门禁挡掉。
        """
        langfang = [
            poi("B0FFJKHNUU", "故宫文具(北京大兴国际机场店)", typecode="061209",
                type_name="购物服务;专卖店;礼品饰品店", adcode="131003", city_name="廊坊市"),
            poi("B0FFKVBT4K", "故宫博物院文创(故宫礼物专卖店)", typecode="110200",
                type_name="风景名胜;风景名胜;风景名胜", adcode="131003", city_name="廊坊市"),
        ]
        result = align(BEIJING, langfang)

        assert result.outcome is AlignOutcome.NO_CANDIDATE
        assert "不在北京" in result.reason

    def test_ambiguous_when_two_candidates_tie(self) -> None:
        """同名候选咬得紧就交人工，不硬选。"""
        candidates = [
            poi("A1", "洒金桥", typecode="190301", type_name="地名地址信息;交通地名;桥",
                adcode="610104"),
            poi("A2", "洒金桥", typecode="190301", type_name="地名地址信息;交通地名;立交桥",
                adcode="610104"),
        ]
        result = align(
            Mention(name="洒金桥", city_name="西安", city_adcode="610100"),
            candidates,
        )

        assert result.outcome is AlignOutcome.AMBIGUOUS
        assert result.resolved is None
        assert result.outcome.needs_human

    def test_low_score_with_multiple_candidates_is_rejected(self) -> None:
        """多个候选且名称都不像的时候不许硬选。

        实测的教训：搜「袁家村」限定西安，前五全是西安市区的连锁中餐馆，
        名称完全一致而城市完全错误。候选一多，高德的排序就没有权威性了。
        """
        candidates = [
            poi(f"P{i}", f"唐韵不倒翁{i}", typecode="080300", type_name="体育休闲服务",
                adcode="610113")
            for i in range(3)
        ]
        result = align(
            Mention(name="不倒翁小姐姐", city_name="西安", city_adcode="610100"),
            candidates,
        )

        assert result.outcome is AlignOutcome.NO_CANDIDATE
        assert "名称得分" in result.reason

    def test_single_low_score_candidate_is_accepted_as_an_alias(self) -> None:
        """唯一候选时相信高德的排序——别名能力在高德那边，不在字符串里。

        实测：搜「陕历博」，第一条正是陕西历史博物馆，但名称得分只有 0.48。
        按 0.80 卡掉的话，这条提及会白白进待对齐队列。
        """
        candidates = [poi("B001D03PEX", "陕西历史博物馆", typecode="140100", adcode="610113")]
        result = align(
            Mention(name="陕历博", city_name="西安", city_adcode="610100"),
            candidates,
        )

        assert result.outcome is AlignOutcome.ALIGNED
        assert result.resolved is not None
        assert result.resolved.name == "陕西历史博物馆"
        assert "高德排序采信" in result.reason

    def test_single_candidate_below_floor_is_still_rejected(self) -> None:
        """放宽有下限：唯一候选也得沾点边。

        否则搜「不倒翁小姐姐」而高德返回同名小店时，就会被当成景点挂上去。
        """
        candidates = [
            poi("P1", "唐韵不倒翁", typecode="080300", adcode="610113"),
            poi("P2", "唐韵不倒翁精酿小酒馆", typecode="080300", adcode="610113"),
        ]
        result = align(
            Mention(name="不倒翁小姐姐", city_name="西安", city_adcode="610100"),
            candidates,
        )

        assert result.outcome is AlignOutcome.NO_CANDIDATE

    def test_empty_candidates(self) -> None:
        result = align(BEIJING, [])
        assert result.outcome is AlignOutcome.NO_CANDIDATE

    def test_missing_city_adcode_is_unresolved_not_aligned(self) -> None:
        """没有城市线索时不许瞎对齐——那正是跨城错误的温床。"""
        result = align(Mention(name="故宫", city_name="北京"), GUGONG_RESULTS)

        assert result.outcome is AlignOutcome.UNRESOLVED
        assert result.resolved is None
        assert result.outcome.needs_human

    def test_place_name_still_aligns_but_is_marked(self) -> None:
        """回民街、洒金桥这类地名的 typecode 是「地名地址信息」，但不是不可用。

        回民街的实测 typecode 是 **061001**（购物服务;特色商业街;步行街）而
        不是 060101，所以按「购物服务一律排除」会把它丢掉。
        """
        candidates = [
            poi("H1", "回民街", typecode="061001", type_name="购物服务;特色商业街;步行街",
                adcode="610104"),
            poi("H2", "西安回民街家庭民宿", typecode="100000", type_name="住宿服务;宾馆酒店",
                adcode="610104"),
        ]
        result = align(
            Mention(name="回民街", city_name="西安", city_adcode="610100"),
            candidates,
        )

        assert result.outcome is AlignOutcome.ALIGNED
        assert result.resolved is not None
        assert result.resolved.kind is PoiKind.DESTINATION

    def test_inspection_office_is_filtered_out_even_when_name_matches(self) -> None:
        """搜「故宫」时「故宫博物院检票处」排在很前，但它是生活服务场所。

        它是实测里最容易误选的候选：名字里有「故宫博物院」，位置就在故宫里。
        """
        scored = score_candidates(BEIJING, GUGONG_RESULTS)
        by_name = {item.poi.name: item for item in scored}

        assert by_name["故宫博物院"].usable
        assert not by_name["故宫博物院检票处"].usable
        assert "游览对象" in (by_name["故宫博物院检票处"].reject_reason or "")

    def test_candidates_are_reported_for_the_workbench(self) -> None:
        """对不上的条目要带着候选进数据工作台，人工只看结论是没法判断的。"""
        candidates = [
            poi("P1", "唐韵不倒翁", typecode="080300", adcode="610113"),
            poi("P2", "不倒翁(显庆路店)", typecode="060101", adcode="610112"),
        ]
        result = align(
            Mention(name="不倒翁小姐姐", city_name="西安", city_adcode="610100"),
            candidates,
        )

        assert len(result.candidates) == 2
        assert all(item.reject_reason for item in result.candidates if not item.usable)
