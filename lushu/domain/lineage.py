"""POI 谱系：一个候选属于哪个本体。

**为什么必须有这一层**：`lushu/domain/align.py` 判断「本体还是子点」靠的是
「它的父节点是否也在候选里」。实测发现这个判断**在真实候选集上经常失效**：

    搜「兵马俑」返回的前十条，六个被判为「本体」，
    可实际上 `秦兵马俑壹号坑大厅` 属于 `秦始皇帝陵博物院`（博物院自己排第十）

父节点不在结果里，就认不出子点，于是六个候选并列、谁也选不出来。

所以要沿 `parent` 往上走，把**候选集之外**的祖先补进来，直到走到根。

本模块只做**纯计算**：给定一张「谁知道自己的父是谁」的表，回答
「某个 POI 的本体是谁」。取祖先的那一步要发网络请求，在
`lushu/adapters/poi_lineage.py`——那边负责把表填满，这边负责用它。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lushu.domain.poi import CandidatePoi

# 一个 POI 往上最多走几层。
# 实测最长的链是三跳（停车场 → 博物馆 → 博物院），留够余量即可；
# 设上限是为了在数据出现环时能停下来——高德的 `parent` 没有环的保证。
MAX_ANCESTOR_HOPS = 8


@dataclass
class CandidateSet:
    """候选，外加它们查得到的祖先。

    - `candidates` 是高德搜索直接返回的那一批
    - `ancestors` 是为了把层级走完而额外查到的节点（**不在**搜索结果里）
    - `lineage` 把两者合起来，键是所有已知的 POI id，值是父 id（根为 None）

    这么分是因为两者用途不同：候选是「可能被选为答案的地方」，
    祖先是「用来判断候选挂在谁的下面」的。实测里祖先常常不在搜索结果中——
    搜「兵马俑」的十条结果里没有 `秦始皇帝陵博物院`，而本体正是它。
    """

    candidates: tuple[CandidatePoi, ...] = ()
    ancestors: dict[str, CandidatePoi] = field(default_factory=dict)
    lineage: dict[str, str | None] = field(default_factory=dict)
    fetched: tuple[str, ...] = ()

    @classmethod
    def from_candidates(
        cls,
        candidates: list[CandidatePoi] | tuple[CandidatePoi, ...],
    ) -> CandidateSet:
        """只凭候选自身的 `parent` 建一张表，不查任何祖先。

        祖先缺失时判断会退化——这正是 `complete_lineage` 存在的原因。
        这个入口留给「已经知道候选是完整的」场景，以及离线测试。
        """
        return cls(
            candidates=tuple(candidates),
            lineage={poi.poi_id: (poi.parent_id or "").strip() or None for poi in candidates},
        )

    def get(self, poi_id: str) -> CandidatePoi | None:
        """按 id 找一个 POI，候选与祖先都算。"""
        for poi in self.candidates:
            if poi.poi_id == poi_id:
                return poi
        return self.ancestors.get(poi_id)

    def owner_of(self, poi_id: str) -> CandidatePoi | None:
        """这条提及最终该挂到哪个 POI 上。

        从下往上找到**最靠近根的已知节点**，包括查到的祖先。
        搜「午门」时返回的是 `故宫博物院`（查到的祖先），不是
        `故宫博物院-午门`（候选）——行程里的一天项就是故宫博物院（ADR-0009）。
        """
        return self.get(self.root_of(poi_id))

    def sub_pois_of(self, poi_id: str) -> tuple[CandidatePoi, ...]:
        """候选里哪些最终属于这个本体。

        用于把「故宫博物院-午门」「故宫博物院检票处」这些候选收进本体下面，
        而不是让它们各自成为一个只有一条结论的孤立 POI。
        """
        return tuple(
            poi for poi in self.candidates if self.root_of(poi.poi_id) == poi_id
        )

    def root_of(self, poi_id: str) -> str:
        """沿 `parent` 链走到本体，返回本体的 id。

        链断了（祖先是查不到的 id）就停在断点——**不猜**。
        返回断点而非候选自身，这样「属于某个我们没查到的东西」这件事
        仍然能从值上看出来。
        """
        seen: set[str] = set()
        current = poi_id
        for _ in range(MAX_ANCESTOR_HOPS):
            if current in seen:
                break  # 数据里出现环，停下来
            seen.add(current)
            if current not in self.lineage:
                break  # 链条断在这里：这个 id 我们没查到
            parent = self.lineage[current]
            if not parent:
                break  # 走到根了
            current = parent
        return current

    def root_poi_of(self, poi_id: str) -> CandidatePoi | None:
        """**候选集里**的本体对象。本体不在候选集里时返回 None。

        与 `owner_of` 的区别：这个只在候选里找，用于「这一组候选的代表是谁」；
        `owner_of` 会连查到的祖先一起找，用于「这条结论该挂到哪个 POI 上」。
        """
        root_id = self.root_of(poi_id)
        for poi in self.candidates:
            if poi.poi_id == root_id:
                return poi
        return None

    def is_sub_of_candidate(self, poi_id: str) -> bool:
        """它是不是「候选集里另一个 POI」的子点。

        这一条决定「本体还是子点」：是子点就不该被选中，
        选了就会把结论挂错一层（ADR-0009）。
        """
        root_id = self.root_of(poi_id)
        if root_id == poi_id:
            return False
        return any(poi.poi_id == root_id for poi in self.candidates)

    def to_dict(self) -> dict[str, str | None]:
        """给存储用的快照。`fetched` 是查过但没查到的 id，也要留着。"""
        return dict(self.lineage)
