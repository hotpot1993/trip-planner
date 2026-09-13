"""重新对齐：把挂在旧实体上的结论搬到新实体上。

**为什么需要这个。** `align_pending` 只处理待办的提及，一条待办解析之后
就再也不会被重新检查。也就是说**对齐算法的每一次改进都只对新数据生效**，
库里已有的结论留在当初（可能是错的）实体上。

M3 就记过这条，当时它只是一个隐患。M4 改了 ADR-0009 的爬链规则
（祖先只是个容器时不归并）之后就变成看得见的错误了：

```
兵马俑的 7 条结论 → B001D09OYW  秦始皇帝陵博物院     （旧规则归并的结果）
兵马俑的预约规则 → B0FFGXMLTU  秦始皇兵马俑博物馆   （新规则的结果）
```

两者是父子关系却是两行，于是城市页那张卡片有 7 条结论却没有预约提醒，
行程页有预约提醒却挂不到任何结论。

**为什么不能自动做。** 搬一条结论到另一个实体上，等于替用户改了他要去的
地方。所以分成两步：`survey` 摆出「哪些提及的落点可能过时了」，
`plan` 重新对齐一遍并算出差异，人看过之后才 `apply`。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from lushu.domain.align import AlignOutcome, Mention, align
from lushu.services import knowledge_store as ks
from lushu.store.connection import connect

# 每条提及之间歇一下。高德对个人 key 有 QPS 限制，实测连着打会返回
# `CUQPS_HAS_EXCEEDED_THE_LIMIT`——第一次 survey 跑完紧接着 --apply 就撞上了。
# 这个命令是偶发的一次性维护，慢几秒无所谓，被限流才麻烦。
PAUSE_SECONDS = 0.4


@dataclass(frozen=True)
class StaleSubject:
    """一个「提及名 + 当前落点」，以及挂在它下面的结论数。"""

    subject_name: str
    poi_id: str
    poi_name: str
    claim_count: int
    city_adcode: str | None
    city_name: str | None


@dataclass(frozen=True)
class RealignPlan:
    """一条提及重新对齐之后的结果。"""

    subject_name: str
    current_poi_id: str
    current_name: str
    claim_count: int
    new_poi_id: str | None
    new_name: str | None
    reason: str

    @property
    def changed(self) -> bool:
        """落点变了没有。只有变了的才值得人看一眼。"""
        return self.new_poi_id is not None and self.new_poi_id != self.current_poi_id


@dataclass
class RealignReport:
    moved_subjects: int = 0
    moved_claims: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (提及名, 原因)


def stale_subjects(*, conn: sqlite3.Connection | None = None) -> list[StaleSubject]:
    """库里所有「结论挂在哪」的组合。

    这一步不判断新旧——它只是把要检查的东西列出来。判断在 `plan` 里，
    因为那一步要问高德。
    """
    owned = conn is None
    active = conn or connect()
    try:
        rows = active.execute(
            "SELECT c.subject_name, c.poi_id, p.name AS poi_name, "
            "  COUNT(*) AS claim_count, p.city_adcode, ci.name AS city_name "
            "FROM claim c "
            "JOIN poi p ON p.amap_poi_id = c.poi_id "
            "LEFT JOIN city ci ON ci.adcode = p.city_adcode "
            "WHERE c.poi_id IS NOT NULL AND c.subject_name IS NOT NULL "
            "GROUP BY c.subject_name, c.poi_id "
            "ORDER BY claim_count DESC, c.subject_name",
        ).fetchall()
    finally:
        if owned:
            active.close()

    return [
        StaleSubject(
            subject_name=row["subject_name"],
            poi_id=row["poi_id"],
            poi_name=row["poi_name"],
            claim_count=row["claim_count"],
            city_adcode=row["city_adcode"],
            city_name=row["city_name"],
        )
        for row in rows
    ]


def realign_subject(
    subject: StaleSubject,
    *,
    search,
    fetch,
) -> RealignPlan:
    """把一条提及重新对齐一遍，看看它现在会落到哪里。

    用的是与 `align_pending` 同一条路径（`complete_lineage` + `align`），
    所以「现在会落到哪里」就是**新数据会被放到哪里**——这正是要比较的东西。
    """
    from lushu.adapters.poi_lineage import complete_lineage

    def unchanged(reason: str) -> RealignPlan:
        return RealignPlan(
            subject_name=subject.subject_name,
            current_poi_id=subject.poi_id,
            current_name=subject.poi_name,
            claim_count=subject.claim_count,
            new_poi_id=None,
            new_name=None,
            reason=reason,
        )

    if not subject.city_name:
        return unchanged("缺城市线索，搜不了高德")

    found = search(subject.subject_name, subject.city_name)
    if not found.candidates:
        return unchanged("高德现在搜不到这个提及")

    roots = complete_lineage(found.candidates, fetch_ancestor=fetch)
    result = align(
        Mention(
            name=subject.subject_name,
            city_name=subject.city_name,
            city_adcode=subject.city_adcode,
        ),
        found.candidates,
        lineage=roots,
    )
    if result.outcome is not AlignOutcome.ALIGNED or result.resolved is None:
        return unchanged(f"现在对不上了：{result.reason or result.outcome.value}")

    return RealignPlan(
        subject_name=subject.subject_name,
        current_poi_id=subject.poi_id,
        current_name=subject.poi_name,
        claim_count=subject.claim_count,
        new_poi_id=result.resolved.poi_id,
        new_name=result.resolved.name,
        reason=result.reason,
    )


def survey(
    *,
    conn: sqlite3.Connection | None = None,
    search=None,
    fetch=None,
    subjects: list[StaleSubject] | None = None,
    pause: float = PAUSE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> list[RealignPlan]:
    """把可能的落点变化全部算出来。只算不改。

    `pause` / `sleep` 可注入：测试里不该真的等。
    """
    from lushu.adapters.poi import fetch_poi, search_pois

    owned = conn is None
    active = conn or connect()
    try:
        items = subjects if subjects is not None else stale_subjects(conn=active)
    finally:
        if owned:
            active.close()

    searcher = search or (lambda keywords, city: search_pois(keywords, city=city))
    fetcher = fetch or (lambda poi_id: fetch_poi(poi_id))

    plans: list[RealignPlan] = []
    for index, item in enumerate(items):
        if index and pause:
            sleep(pause)
        plans.append(realign_subject(item, search=searcher, fetch=fetcher))
    return plans


def apply_plans(
    plans: list[RealignPlan], *, conn: sqlite3.Connection | None = None
) -> RealignReport:
    """把落点变了的结论搬过去。

    **调用方负责事务**：搬到一半的库比不搬更糟。

    搬完要重算独立来源数——置信度是按「这条结论挂在哪里」的证据算的，
    换了落点就得重算一遍，否则新实体上的那条会带着旧的计数。
    """
    owned = conn is None
    active = conn or connect()
    report = RealignReport()
    try:
        for plan in plans:
            if not plan.changed or plan.new_poi_id is None:
                continue
            rows = active.execute(
                "SELECT id FROM claim WHERE poi_id = ? AND subject_name = ?",
                (plan.current_poi_id, plan.subject_name),
            ).fetchall()
            for row in rows:
                # 搬之前确认目标实体真的在库里——`claim.poi_id` 有外键，
                # 没写进去的话这里会直接撞约束。对齐认定不等于已入库。
                exists = active.execute(
                    "SELECT 1 FROM poi WHERE amap_poi_id = ?", (plan.new_poi_id,)
                ).fetchone()
                if exists is None:
                    report.skipped.append(
                        (plan.subject_name, f"目标实体 {plan.new_poi_id} 还没入库，先跑一次对齐")
                    )
                    break
                active.execute(
                    "UPDATE claim SET poi_id = ? WHERE id = ?", (plan.new_poi_id, row["id"])
                )
                ks.recount_independent_sources(row["id"], conn=active)
                report.moved_claims += 1
            else:
                report.moved_subjects += 1
        active.commit()
    finally:
        if owned:
            active.close()
    return report
