"""复验扫描：哪些结论与规则已经过期，该重新核一遍了。

设计第 4.6 节（Q49）：按结论类型设不同周期——门票与开放时间 90 天、
排队与人流这类慢变量 180 天、拍照机位不设期限。**到期后状态变为待复验，
在界面上标注而不隐藏**。

**为什么这件事要单独做一层。** 到期是个纯粹的静默事件：日期一天天过去，
库里那行数据什么都没变，界面上也看不出任何异样。用户看到的仍然是
「提前 7 天 20:00 放票」，而那条规则可能是三个月前核的、之后景区改了三次。
本项目里其他会静默失败的地方（离线可读、路段描述、放票口径）都已经
变成了校验项，这一个还没有。

两类东西各有各的到期日，来源也不同：

- **预约规则**：复核日加 90 天（`booking.REVIEW_VALID_DAYS`）。22 条规则
  是同一天一起复核的，于是它们会在同一天一起过期——那一天之后，界面上所有
  预约提醒都建立在没人再看过的规则上，而这件事不会有人来告诉你。
- **攻略结论**：按 `facet` 分档（`knowledge.refresh_days_for`），锚点是
  首次见到这条结论的那天。到期的结论状态转成 `needs_reverify`，**不是删掉**
  ——过期的经验仍有参考价值，只是必须让用户知道自己看的是旧信息。

**周期只有一处定义，这里只读结果。** 两条到期日都不是本模块算的：
规则走 `BookingRule.verify_due_at`（复核日 + `REVIEW_VALID_DAYS`），
结论读入库时写下的 `claim.verify_due_at`（`first_seen_at` + `refresh_days_for`）。
本模块一行周期常量都没有，也不该有——否则同一个「90 天」会有两个出处。
这条不变式有个代价：**将来实现「复验通过」时必须同时刷新 `verify_due_at`
列**，否则扫描会一直报已过期。全库目前没有任何 `last_verified_at` 写入，
所以列与实时计算暂时一致，`tests/test_verify.py` 钉住了这一点。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from lushu.domain.booking import RuleStatus
from lushu.services import booking_store
from lushu.store.connection import connect

# 快到期的，提前这么多天就值得看一眼。90 天的周期里留两周余量，
# 不至于到了当天才发现「今天要核 22 条」。
SOON_DAYS = 14


@dataclass(frozen=True)
class DueItem:
    """一件到期或快到期的东西。"""

    kind: str  # rule | claim
    ref_id: str
    label: str
    due_at: date
    days_left: int  # 0 表示今天到期，负数表示已经过去
    detail: str = ""

    @property
    def overdue(self) -> bool:
        """到期当天就算到期。

        与 `BookingRule.is_due_for_review`（`today >= due`）和
        `mark_due_claims` 的 SQL（`verify_due_at <= 今天`）保持同一个判据。
        从前这里写的是 `< 0`，于是同一天里报告说「还有 0 天」、`--apply`
        却把它翻成待复验——一处判断不能有两个答案。
        """
        return self.days_left <= 0

    @property
    def description(self) -> str:
        """给人看的一句话。"""
        if self.days_left < 0:
            return f"已过期 {-self.days_left} 天"
        if self.days_left == 0:
            return "今天到期"
        return f"还有 {self.days_left} 天"


@dataclass
class ScanReport:
    """一次复验扫描的结果。"""

    today: date
    items: list[DueItem] = field(default_factory=list)
    # 没设期限的那一类（拍照机位）：报个数，让「没扫到」与「不设期限」分得开
    timeless: int = 0

    @property
    def overdue(self) -> list[DueItem]:
        return [item for item in self.items if item.overdue]

    @property
    def soon(self) -> list[DueItem]:
        return [item for item in self.items if not item.overdue]

    @property
    def rules(self) -> list[DueItem]:
        return [item for item in self.items if item.kind == "rule"]

    @property
    def claims(self) -> list[DueItem]:
        return [item for item in self.items if item.kind == "claim"]


def _days_left(due: date, today: date) -> int:
    return (due - today).days


def due_rules(
    *,
    conn: sqlite3.Connection | None = None,
    today: date | None = None,
    soon_days: int = SOON_DAYS,
) -> list[DueItem]:
    """已复核、且已经到期或快到期的预约规则。

    **草案状态的规则不在扫描范围里**——它们本来就不对用户可见，
    没有人依赖它们。扫描要盯的是「正在被用户看到、但依据已经旧了」的那些。
    """
    owned = conn is None
    active = conn or connect()
    anchor = today or date.today()
    try:
        rules = booking_store.all_rules(conn=active)
        names = booking_store.poi_names(conn=active)
    finally:
        if owned:
            active.close()

    found: list[DueItem] = []
    for rule in rules:
        if rule.status is not RuleStatus.REVIEWED or rule.verify_due_at is None:
            continue
        left = _days_left(rule.verify_due_at, anchor)
        if left > soon_days:
            continue
        # 「必须预约」与「算得出放票日」是两件事（booking.BookingRule 里写了原因）：
        # 17 个知名景点里 12 个从不公布放票天数。这里照实说，不把未知写成 0。
        timing = (
            f"提前 {rule.advance_days} 天"
            if rule.advance_days is not None
            else "放票口径未公布"
        )
        if rule.release_time:
            timing += f" {rule.release_time}"
        found.append(
            DueItem(
                kind="rule",
                ref_id=rule.poi_id,
                label=names.get(rule.poi_id, rule.poi_id),
                due_at=rule.verify_due_at,
                days_left=left,
                detail=timing,
            )
        )
    found.sort(key=lambda item: item.days_left)
    return found


def due_claims(
    *,
    conn: sqlite3.Connection | None = None,
    today: date | None = None,
    soon_days: int = SOON_DAYS,
) -> tuple[list[DueItem], int]:
    """已到期的结论，以及不设期限的条数。

    读的是入库时写下的 `verify_due_at` 列，不重算——与 `trip_insights`、
    工作台的读法保持一致，界面上标出来的与这里报出来的必须是同一天。

    `retired` 的结论不算：那类是被明确废弃的，不是「该重核了」。
    """
    owned = conn is None
    active = conn or connect()
    anchor = today or date.today()
    try:
        rows = active.execute(
            "SELECT id, poi_id, subject_name, text, facet, verify_due_at, status "
            "FROM claim WHERE status != 'retired'",
        ).fetchall()
        names = booking_store.poi_names(conn=active)
    finally:
        if owned:
            active.close()

    found: list[DueItem] = []
    timeless = 0
    for row in rows:
        if not row["verify_due_at"]:
            timeless += 1
            continue
        due = date.fromisoformat(row["verify_due_at"])
        left = _days_left(due, anchor)
        if left > soon_days:
            continue
        found.append(
            DueItem(
                kind="claim",
                ref_id=row["id"],
                label=_claim_label(row, names),
                due_at=due,
                days_left=left,
                detail=(row["text"] or "")[:40],
            )
        )
    found.sort(key=lambda item: item.days_left)
    return found, timeless


def _claim_label(row: sqlite3.Row, names: dict[str, str]) -> str:
    """结论挂在谁身上。

    优先用原文里的叫法（`subject_name`，如「湖南省博」），它比高德名更接近
    用户的说法；没有就退回实体名——**按 `poi_id` 查，不是按结论 id**。
    """
    return (
        row["subject_name"]
        or names.get(row["poi_id"] or "", "")
        or "（未写主体）"
    )


def scan(
    *,
    conn: sqlite3.Connection | None = None,
    today: date | None = None,
    soon_days: int = SOON_DAYS,
) -> ScanReport:
    """扫描全部到期与快到期的东西。只读。

    `soon_days` 是「提前多久提醒」，默认两周。调大它回答的是另一个问题：
    「出发前哪些会到期」——22 条规则同一天复核、同一天到期，值得提前排期。
    """
    anchor = today or date.today()
    claims, timeless = due_claims(conn=conn, today=anchor, soon_days=soon_days)
    items = due_rules(conn=conn, today=anchor, soon_days=soon_days) + claims
    # 两张表各排各的，拼起来还得再排一次——最该看的是过期最久的
    items.sort(key=lambda item: (item.days_left, item.kind, item.label))
    return ScanReport(today=anchor, items=items, timeless=timeless)


def next_due(*, conn: sqlite3.Connection | None = None, today: date | None = None) -> date | None:
    """最近一次**将来**的到期日；今天之后没有会到期的就返回 None。

    扫描窗口之外的东西也要看：今天没事不代表不用再回来，
    「下一次该在什么时候看一眼」本身就是扫描要回答的问题之一。

    已经过期的那些不算「下一次」——它们要现在处理，而且 `scan` 已经把它们
    单独报出来了。所以 None 有两种含义（全都不设期限、或全都已经过期），
    调用方手里有 `scan` 的结果，分得开这两件事。
    """
    owned = conn is None
    active = conn or connect()
    anchor = today or date.today()
    try:
        dates = [
            rule.verify_due_at
            for rule in booking_store.all_rules(conn=active)
            if rule.status is RuleStatus.REVIEWED
            and rule.verify_due_at is not None
            and rule.verify_due_at >= anchor
        ]
        rows = active.execute(
            "SELECT verify_due_at FROM claim "
            "WHERE status != 'retired' AND verify_due_at IS NOT NULL AND verify_due_at >= ?",
            (anchor.isoformat(),),
        ).fetchall()
        dates += [date.fromisoformat(row["verify_due_at"]) for row in rows]
    finally:
        if owned:
            active.close()
    return min(dates) if dates else None


def mark_due_claims(
    *, conn: sqlite3.Connection | None = None, today: date | None = None
) -> list[str]:
    """把已到期的结论状态转成 `needs_reverify`，返回改动的 id。

    设计 4.6：「到期后状态变为待复验，**在界面上标注而不隐藏**」。
    所以这里只改状态，不删、不改置信度——过期的经验仍有参考价值。

    只动**已经到期**的，不动「快到期」的：提前两周就把状态翻掉，
    会让复验队列里混进一批还没到期的，真正该看的反而被淹没。

    **返回值是 id 列表而不是个数**：这条命令的副作用在界面上看不出来
    （`needs_reverify` 的结论照样展示，只是多一个标注），只报一个数字
    没人能核对它动了哪几条。
    """
    owned = conn is None
    active = conn or connect()
    anchor = today or date.today()
    try:
        rows = active.execute(
            "SELECT id FROM claim "
            "WHERE status = 'active' AND verify_due_at IS NOT NULL AND verify_due_at <= ? "
            "ORDER BY verify_due_at, id",
            (anchor.isoformat(),),
        ).fetchall()
        changed = [row["id"] for row in rows]
        for claim_id in changed:
            active.execute(
                "UPDATE claim SET status = 'needs_reverify' WHERE id = ?", (claim_id,)
            )
        active.commit()
        return changed
    finally:
        if owned:
            active.close()


def stats(*, conn: sqlite3.Connection | None = None, today: date | None = None) -> dict[str, int]:
    """看得见的那几个数。列表页与状态页用它。"""
    report = scan(conn=conn, today=today)
    return {
        "overdue": len(report.overdue),
        "soon": len(report.soon),
        "rules": len(report.rules),
        "claims": len(report.claims),
        "timeless": report.timeless,
    }
