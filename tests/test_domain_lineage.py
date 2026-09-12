"""谱系补全的测试。

这一层存在的理由全部来自实测（docs/M3-PROBE.md 第三节）：

    搜「兵马俑」的十条结果里，六条最终属于同一个本体（秦始皇帝陵博物院），
    而博物院自己排在最后；搜「午门」，本体「故宫博物院」**根本不在结果里**。

父节点不在候选集里就认不出子点，于是候选并列、谁也选不出来。
这里测的就是「去把缺失的祖先查回来」这件事，取数函数是注入的，
所以测试完全离线。
"""

from __future__ import annotations

import pytest

from lushu.adapters.poi_lineage import complete_lineage
from lushu.domain.lineage import MAX_ANCESTOR_HOPS, CandidateSet
from lushu.domain.poi import CandidatePoi


def poi(poi_id: str, name: str, parent_id: str | None = None) -> CandidatePoi:
    return CandidatePoi(
        poi_id=poi_id,
        name=name,
        typecode="110200",
        adcode="110101",
        parent_id=parent_id,
    )


def fetcher(table: dict[str, CandidatePoi], calls: list[str] | None = None):
    """按 id 取祖先的假实现，记录被问过哪些 id。"""

    def fetch(poi_id: str) -> CandidatePoi | None:
        if calls is not None:
            calls.append(poi_id)
        return table.get(poi_id)

    return fetch


class TestCompleteLineage:
    def test_fetches_missing_parent(self) -> None:
        """搜「午门」时本体不在结果里，要补查。"""
        candidates = [poi("B000A84GDN", "故宫博物院-午门", parent_id="B000A8UIN8")]
        table = {"B000A8UIN8": poi("B000A8UIN8", "故宫博物院")}
        calls: list[str] = []

        roots = complete_lineage(candidates, fetch_ancestor=fetcher(table, calls))

        assert calls == ["B000A8UIN8"]
        assert roots.fetched == ("B000A8UIN8",)
        assert roots.root_of("B000A84GDN") == "B000A8UIN8"
        owner = roots.owner_of("B000A84GDN")
        assert owner is not None
        assert owner.name == "故宫博物院"

    def test_walks_multiple_hops(self) -> None:
        """实测的链是三跳：停车场 → 博物馆 → 博物院。"""
        candidates = [
            poi("B001D0095C", "秦始皇兵马俑博物馆第1停车场", parent_id="B0FFGXMLTU")
        ]
        table = {
            "B0FFGXMLTU": poi("B0FFGXMLTU", "秦始皇兵马俑博物馆", parent_id="B001D09OYW"),
            "B001D09OYW": poi("B001D09OYW", "秦始皇帝陵博物院"),
        }
        calls: list[str] = []

        roots = complete_lineage(candidates, fetch_ancestor=fetcher(table, calls))

        assert calls == ["B0FFGXMLTU", "B001D09OYW"]
        assert roots.root_of("B001D0095C") == "B001D09OYW"
        owner = roots.owner_of("B001D0095C")
        assert owner is not None
        assert owner.name == "秦始皇帝陵博物院"

    def test_each_ancestor_is_fetched_once(self) -> None:
        """同一批里多个候选共享一个祖先时不能重复查。"""
        candidates = [
            poi("A1", "故宫博物院-午门", parent_id="B000A8UIN8"),
            poi("A2", "故宫博物院-神武门", parent_id="B000A8UIN8"),
            poi("A3", "故宫博物院检票处", parent_id="B000A8UIN8"),
        ]
        table = {"B000A8UIN8": poi("B000A8UIN8", "故宫博物院")}
        calls: list[str] = []

        complete_lineage(candidates, fetch_ancestor=fetcher(table, calls))

        assert calls == ["B000A8UIN8"]

    def test_missing_ancestor_stops_at_the_break(self) -> None:
        """查不到就停在断点，并把「查过但没查到」记下来。

        不能因为一个祖先查不到就让整批对齐失败——而且断点要和
        「压根没见过这个 id」区分开，否则排查时分不清是网络问题还是数据问题。

        链条断掉时 `root_of` 返回的是那个**够不着的祖先 id**（`NOPE`），
        不是候选自身。这是刻意的：它如实说明「这东西属于某个我们查不到的东西」，
        调用方从 `owner_of` 拿不到对象就知道该退回哪个候选了。
        """
        candidates = [poi("A1", "某景点-某子点", parent_id="NOPE")]
        calls: list[str] = []

        roots = complete_lineage(candidates, fetch_ancestor=fetcher({}, calls))

        assert calls == ["NOPE"]
        assert roots.fetched == ("NOPE",)
        assert "NOPE" in roots.lineage
        assert roots.lineage["NOPE"] is None
        assert roots.root_of("A1") == "NOPE"
        # 够不着的祖先取不到对象，于是退回候选自身——不猜一个本体出来
        assert roots.owner_of("A1") is None
        assert roots.root_poi_of("A1") is None

    def test_fetcher_exception_does_not_break_the_batch(self) -> None:
        """取数抛异常时停在断点，而不是让整批对齐失败。"""
        candidates = [
            poi("A1", "有问题的子点", parent_id="BOOM"),
            poi("A2", "故宫博物院-午门", parent_id="B000A8UIN8"),
        ]
        table = {"B000A8UIN8": poi("B000A8UIN8", "故宫博物院")}

        def fetch(poi_id: str) -> CandidatePoi | None:
            if poi_id == "BOOM":
                raise RuntimeError("高德网络抖动")
            return table.get(poi_id)

        roots = complete_lineage(candidates, fetch_ancestor=fetch)

        # 出问题的那条停在断点
        assert roots.owner_of("A1") is None
        # 其余候选不受影响
        assert roots.root_of("A2") == "B000A8UIN8"
        owner = roots.owner_of("A2")
        assert owner is not None
        assert owner.name == "故宫博物院"

    def test_cycle_does_not_hang(self) -> None:
        """高德的 `parent` 没有无环的保证，成环时要能停下来。"""
        candidates = [poi("A1", "甲", parent_id="A2")]
        table = {
            "A2": poi("A2", "乙", parent_id="A3"),
            "A3": poi("A3", "丙", parent_id="A2"),  # 环
        }

        roots = complete_lineage(candidates, fetch_ancestor=fetcher(table))

        # 不挂住，且停在环里的某个节点上
        assert roots.root_of("A1") in {"A2", "A3"}

    def test_hops_are_bounded(self) -> None:
        """异常深的链条不该无限查下去。"""
        candidates = [poi("P0", "第 0 层", parent_id="P1")]
        table = {f"P{i}": poi(f"P{i}", f"第 {i} 层", parent_id=f"P{i + 1}") for i in range(1, 40)}
        calls: list[str] = []

        complete_lineage(candidates, fetch_ancestor=fetcher(table, calls))

        assert len(calls) <= MAX_ANCESTOR_HOPS

    def test_no_parent_needs_no_fetch(self) -> None:
        candidates = [poi("B000A8UIN8", "故宫博物院")]
        calls: list[str] = []

        roots = complete_lineage(candidates, fetch_ancestor=fetcher({}, calls))

        assert calls == []
        assert roots.fetched == ()
        assert roots.root_of("B000A8UIN8") == "B000A8UIN8"


class TestCandidateSet:
    def test_from_candidates_has_no_ancestors(self) -> None:
        roots = CandidateSet.from_candidates([poi("A1", "甲", parent_id="A2")])

        assert roots.ancestors == {}
        assert roots.root_of("A1") == "A2"
        # 祖先没查过，所以取不到对象——这正是需要 complete_lineage 的原因
        assert roots.owner_of("A1") is None
        assert roots.root_poi_of("A1") is None

    def test_sub_pois_of_groups_the_candidates(self) -> None:
        """搜「故宫」返回的子点要能归到本体下面。"""
        candidates = [
            poi("B000A8UIN8", "故宫博物院"),
            poi("B000A84GDN", "故宫博物院-午门", parent_id="B000A8UIN8"),
            poi("B0FFKL520U", "故宫博物院检票处", parent_id="B000A8UIN8"),
        ]
        roots = CandidateSet.from_candidates(candidates)

        subs = {item.poi_id for item in roots.sub_pois_of("B000A8UIN8")}

        assert subs == {"B000A8UIN8", "B000A84GDN", "B0FFKL520U"}

    def test_get_finds_ancestors_too(self) -> None:
        candidates = [poi("A1", "某子点", parent_id="A2")]
        roots = complete_lineage(
            candidates, fetch_ancestor=fetcher({"A2": poi("A2", "本体")})
        )

        assert roots.get("A1") is not None
        assert roots.get("A2") is not None
        assert roots.get("A3") is None

    def test_is_sub_of_candidate(self) -> None:
        candidates = [
            poi("B000A8UIN8", "故宫博物院"),
            poi("B000A84GDN", "故宫博物院-午门", parent_id="B000A8UIN8"),
        ]
        roots = CandidateSet.from_candidates(candidates)

        assert roots.is_sub_of_candidate("B000A84GDN")
        assert not roots.is_sub_of_candidate("B000A8UIN8")


@pytest.mark.parametrize(
    ("parent_id", "expected_root"),
    [("A2", "A2"), ("", "A1"), (None, "A1")],
)
def test_root_of_handles_empty_parent(parent_id: str | None, expected_root: str) -> None:
    """`parent` 是空串与 None 都表示根。高德两种都返回过。"""
    roots = CandidateSet.from_candidates([poi("A1", "甲", parent_id=parent_id)])

    assert roots.root_of("A1") == expected_root
