"""把候选的祖先链补全——`CandidateSet` 的取数那一半。

纯计算在 `lushu/domain/lineage.py`，这里只负责「缺哪个祖先就去查哪个」。
取数函数由调用方注入（通常是 `lushu/adapters/poi.py` 的 `fetch_poi` 包一层），
所以本模块不直接依赖 httpx，可以离线测试。
"""

from __future__ import annotations

from collections.abc import Callable

from lushu.domain.lineage import MAX_ANCESTOR_HOPS, CandidateSet
from lushu.domain.poi import CandidatePoi


def complete_lineage(
    candidates: list[CandidatePoi] | tuple[CandidatePoi, ...],
    *,
    fetch_ancestor: Callable[[str], CandidatePoi | None],
) -> CandidateSet:
    """补齐候选的祖先链，返回可以直接用于对齐的 `CandidateSet`。

    每个缺失的祖先只查一次。查不到时**停在断点而不是抛异常**——
    一条谱系查不到不该让整批对齐失败，而且断点会被 `root_of` 如实反映出来，
    调用方看得见。
    """
    candidate_list = tuple(candidates)
    lineage: dict[str, str | None] = {
        poi.poi_id: (poi.parent_id or "").strip() or None for poi in candidate_list
    }
    ancestors: dict[str, CandidatePoi] = {}
    fetched: list[str] = []

    frontier = [parent for parent in lineage.values() if parent]

    for _ in range(MAX_ANCESTOR_HOPS):
        missing = [poi_id for poi_id in dict.fromkeys(frontier) if poi_id not in lineage]
        if not missing:
            break

        next_frontier: list[str] = []
        for poi_id in missing:
            try:
                ancestor = fetch_ancestor(poi_id)
            except Exception:  # noqa: BLE001
                ancestor = None
            fetched.append(poi_id)

            if ancestor is None:
                # 记成一个没有父的节点：链在这里断，但至少把
                # 「这个 id 存在过、我们查过」与「压根没见过」区分开
                lineage[poi_id] = None
                continue

            ancestors[poi_id] = ancestor
            parent = (ancestor.parent_id or "").strip() or None
            lineage[poi_id] = parent
            if parent:
                next_frontier.append(parent)

        frontier = next_frontier

    return CandidateSet(
        candidates=candidate_list,
        ancestors=ancestors,
        lineage=lineage,
        fetched=tuple(fetched),
    )
