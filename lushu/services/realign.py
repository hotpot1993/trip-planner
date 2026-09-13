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
from dataclasses import dataclass, field, replace

from lushu.domain.align import AlignOutcome, Mention, align
from lushu.services import knowledge_store as ks
from lushu.store.connection import connect

# 每条提及之间歇一下。高德对个人 key 有 QPS 限制，实测连着打会返回
# `CUQPS_HAS_EXCEEDED_THE_LIMIT`——第一次 survey 跑完紧接着 --apply 就撞上了。
# 这个命令是偶发的一次性维护，慢几秒无所谓，被限流才麻烦。
PAUSE_SECONDS = 0.4


@dataclass(frozen=True)
class StaleSubject:
    """一个「提及名 + 当前落点」，以及有多少东西指着它。

    `from_claims` 与 `from_items` 是两类**指向同一实体的东西**，
    它们的对齐来源不同、但必须一起搬：

    - 结论（`claim`）：M3 的对齐写下的
    - 行程天项（`day_item`）：引擎排程时写下的

    只搬结论会留下一个更隐蔽的毛病：结论到了新实体上，而行程还指着旧实体，
    于是**那两条结论在行程上完全看不见**。实测中山陵就是这样——
    行程指向 `B000A8UIN0 中山陵`，而结论在 `B0FFHVEECG 中山陵景区` 上。
    """

    subject_name: str
    poi_id: str
    poi_name: str
    claim_count: int = 0
    item_count: int = 0
    city_adcode: str | None = None
    city_name: str | None = None

    @property
    def reference_count(self) -> int:
        return self.claim_count + self.item_count


@dataclass(frozen=True)
class RealignPlan:
    """一条提及重新对齐之后的结果。"""

    subject_name: str
    current_poi_id: str
    current_name: str
    new_poi_id: str | None
    new_name: str | None
    reason: str
    claim_count: int = 0
    item_count: int = 0
    # 重跑对齐时认定的那个候选。搬引用之前要先把它写进 `poi` 表——
    # 「重跑一遍对齐」本来就该有和跑对齐一样的效果，包括把实体落库。
    # 否则会撞上先有鸡还是先有蛋：目标实体没入库，引用就搬不过去。
    resolved: object | None = None
    resolved_city_adcode: str | None = None

    @property
    def reference_count(self) -> int:
        return self.claim_count + self.item_count

    @property
    def changed(self) -> bool:
        """落点变了没有。只有变了的才值得人看一眼。"""
        return self.new_poi_id is not None and self.new_poi_id != self.current_poi_id


@dataclass
class RealignReport:
    moved_subjects: int = 0
    moved_claims: int = 0
    moved_items: int = 0
    wrote_pois: int = 0  # 目标实体原先不在库里、顺手写进去的条数
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (提及名, 原因)


def stale_subjects(*, conn: sqlite3.Connection | None = None) -> list[StaleSubject]:
    """库里所有「某处地方被谁指着」的组合。

    两步都不判断新旧——它们只是把要检查的东西列出来。判断在 `plan` 里，
    因为那一步要问高德。

    **两类引用都要收**：结论与行程天项。只收结论的话，结论搬走了而行程
    还指着旧实体，那些结论在行程上就永远看不见。
    """
    owned = conn is None
    active = conn or connect()
    try:
        claims = active.execute(
            "SELECT c.subject_name, c.poi_id, p.name AS poi_name, "
            "  COUNT(*) AS claim_count, p.city_adcode, ci.name AS city_name "
            "FROM claim c "
            "JOIN poi p ON p.amap_poi_id = c.poi_id "
            "LEFT JOIN city ci ON ci.adcode = p.city_adcode "
            "WHERE c.poi_id IS NOT NULL AND c.subject_name IS NOT NULL "
            "GROUP BY c.subject_name, c.poi_id",
        ).fetchall()
        items = active.execute(
            "SELECT di.title AS subject_name, di.poi_id, p.name AS poi_name, "
            "  COUNT(DISTINCT di.id) AS item_count, p.city_adcode, ci.name AS city_name "
            "FROM day_item di "
            "JOIN poi p ON p.amap_poi_id = di.poi_id "
            "LEFT JOIN city ci ON ci.adcode = p.city_adcode "
            "WHERE di.kind = 'poi' AND di.poi_id IS NOT NULL AND di.title IS NOT NULL "
            "GROUP BY di.title, di.poi_id",
        ).fetchall()
    finally:
        if owned:
            active.close()

    merged: dict[tuple[str, str], StaleSubject] = {}
    for row in claims:
        key = (row["subject_name"], row["poi_id"])
        merged[key] = StaleSubject(
            subject_name=row["subject_name"],
            poi_id=row["poi_id"],
            poi_name=row["poi_name"],
            claim_count=row["claim_count"],
            city_adcode=row["city_adcode"],
            city_name=row["city_name"],
        )
    for row in items:
        key = (row["subject_name"], row["poi_id"])
        existing = merged.get(key)
        if existing is None:
            merged[key] = StaleSubject(
                subject_name=row["subject_name"],
                poi_id=row["poi_id"],
                poi_name=row["poi_name"],
                item_count=row["item_count"],
                city_adcode=row["city_adcode"],
                city_name=row["city_name"],
            )
        else:
            merged[key] = replace(existing, item_count=row["item_count"])

    return sorted(
        merged.values(), key=lambda item: (-item.reference_count, item.subject_name)
    )


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
            new_poi_id=None,
            new_name=None,
            reason=reason,
            claim_count=subject.claim_count,
            item_count=subject.item_count,
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
        new_poi_id=result.resolved.poi_id,
        new_name=result.resolved.name,
        reason=result.reason,
        claim_count=subject.claim_count,
        item_count=subject.item_count,
        resolved=result.resolved,
        resolved_city_adcode=subject.city_adcode,
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


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def apply_plans(
    plans: list[RealignPlan], *, conn: sqlite3.Connection | None = None
) -> RealignReport:
    """把落点变了的**引用**搬过去。

    `claim.poi_id` 与 `day_item.poi_id` 一起搬。只搬前者会留下一个更隐蔽的
    毛病：结论到了新实体上，而行程还指着旧实体，**那两条结论在行程上完全
    看不见**——实测中山陵就是这样。

    `day_item.title` 不动：那行字是人看到的景点名，与它指向哪个实体是两回事。

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

            # 搬之前确认目标实体真的在库里——两列都有外键，没写进去的话
            # 这里会直接撞约束。**没入库就把它写进去**：「重跑一遍对齐」
            # 本来就该有和跑对齐一样的效果，包括把认定的实体落库。
            exists = active.execute(
                "SELECT 1 FROM poi WHERE amap_poi_id = ?", (plan.new_poi_id,)
            ).fetchone()
            if exists is None:
                if plan.resolved is None:
                    report.skipped.append(
                        (plan.subject_name, f"目标实体 {plan.new_poi_id} 还没入库")
                    )
                    continue
                ks.save_candidate_poi(
                    conn=active,
                    poi=plan.resolved,
                    city_adcode=plan.resolved_city_adcode,
                    now=_now(),
                )
                report.wrote_pois += 1

            rows = active.execute(
                "SELECT id FROM claim WHERE poi_id = ? AND subject_name = ?",
                (plan.current_poi_id, plan.subject_name),
            ).fetchall()
            for row in rows:
                active.execute(
                    "UPDATE claim SET poi_id = ? WHERE id = ?", (plan.new_poi_id, row["id"])
                )
                ks.recount_independent_sources(row["id"], conn=active)
                report.moved_claims += 1

            moved = active.execute(
                "UPDATE day_item SET poi_id = ? WHERE poi_id = ? AND title = ? AND kind = 'poi'",
                (plan.new_poi_id, plan.current_poi_id, plan.subject_name),
            ).rowcount
            report.moved_items += moved

            if rows or moved:
                report.moved_subjects += 1
        active.commit()
    finally:
        if owned:
            active.close()
    return report
