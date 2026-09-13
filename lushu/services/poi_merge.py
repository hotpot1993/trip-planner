"""把「同一处地方的两个实体」合成一个。

**这是 ADR-0002 的一次补救，不是常规操作。** 高德是本项目的实体真源，
一个地方只该有一行 `poi`。但 M1/M2 落库时用的是引擎从规划结果里回填的 id，
M3 有了自己的高德适配器之后又按名称搜了一遍，于是同一个地方有了两行。
实测真实库里有五处，其中三处的旧 id 是 `X` 开头的自造值。

不合并的后果是具体的，不是洁癖：

- 预约规则挂在新实体上（种子库按高德搜出来的 id 走），而旧行程的天项指向
  旧实体，于是**规则永远匹配不到那些行程**——用户看不到预约提醒。
- M5 的候选池同理：攻略结论挂在新实体上，行程里的旧景点取不到任何结论。

合并的动作有两步，顺序不能反：**先把指着旧实体的引用改到新实体上，再删旧行**。
反过来的话，那些引用会因为外键失效而被级联删掉——`day_item.poi_id` 的外键
没有 ON DELETE 规则，`claim.poi_id` 也没有，删旧行会直接被外键挡住；
但 `poi.parent_poi_id` 指向它时情况更糟（子点会变成孤儿）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from lushu.domain.poi import PoiIdentity, pick_survivor
from lushu.store.connection import connect

# 所有可能指向 poi 的地方。合并时必须一个不漏——漏掉一个，
# 删旧行时要么撞外键报错，要么更糟：悄悄留下一批指不到实体的引用。
#
# 这张表与 schema 一起维护：日后新增指向 poi 的列，这里要同步加。
_REFERENCES: tuple[tuple[str, str], ...] = (
    ("day_item", "poi_id"),
    ("claim", "poi_id"),
    ("claim", "poi_a_id"),
    ("claim", "poi_b_id"),
    ("booking_rule", "poi_id"),
    ("poi", "parent_poi_id"),
)


@dataclass(frozen=True)
class DuplicateGroup:
    """同一个名字下面的两行（或更多行）。"""

    name: str
    survivor: PoiIdentity
    losers: tuple[PoiIdentity, ...]

    @property
    def summary(self) -> str:
        loser_ids = "、".join(item.poi_id for item in self.losers)
        return f"{self.name}：留 {self.survivor.poi_id}，并掉 {loser_ids}"


@dataclass
class MergeReport:
    """一次合并的结果。改了多少行要说清楚——合并是不可逆的。"""

    merged: int = 0  # 并掉的旧实体数
    moved: dict[str, int] = field(default_factory=dict)  # 表.列 → 改了多少行
    failed: list[tuple[str, str]] = field(default_factory=list)


def _identity(conn: sqlite3.Connection, row: sqlite3.Row) -> PoiIdentity:
    references = 0
    for table, column in _REFERENCES:
        found = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?", (row["amap_poi_id"],)
        ).fetchone()
        references += found["n"]
    return PoiIdentity(
        poi_id=row["amap_poi_id"],
        name=row["name"],
        typecode=row["typecode"],
        type_name=row["type"],
        adcode=row["adcode"],
        address=row["address"],
        parent_id=row["parent_poi_id"],
        raw_json=row["raw_json"],
        fetched_at=row["fetched_at"],
        references=references,
    )


def find_duplicates(
    conn: sqlite3.Connection, *, name: str | None = None
) -> list[DuplicateGroup]:
    """找出同名多行的那些地方。

    **只按名字找。** 更聪明的办法（比对坐标、比对高德返回的父子关系）在这里
    得不偿失：这件事要人看着做，同名两行已经是最该看的信号，
    而「名字不同其实是同一个地方」根本不该由脚本猜。
    """
    clause = "WHERE name = ?" if name else ""
    params = (name,) if name else ()
    rows = conn.execute(
        f"SELECT * FROM poi {clause} ORDER BY name, amap_poi_id", params
    ).fetchall()

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["name"], []).append(row)

    groups: list[DuplicateGroup] = []
    for poi_name, items in grouped.items():
        if len(items) < 2:
            continue
        identities = [_identity(conn, item) for item in items]
        winner = pick_survivor(identities)
        if winner is None:
            continue
        groups.append(
            DuplicateGroup(
                name=poi_name,
                survivor=winner,
                losers=tuple(item for item in identities if item.poi_id != winner.poi_id),
            )
        )
    return groups


def merge_group(conn: sqlite3.Connection, group: DuplicateGroup) -> MergeReport:
    """把一组里的旧实体并到留下的那个上。

    调用方负责事务：这件事要么整组做完，要么一点都不做，
    做到一半留下的是一批「指向已经不存在的实体」的引用。
    """
    report = MergeReport()
    for loser in group.losers:
        for table, column in _REFERENCES:
            cursor = conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                (group.survivor.poi_id, loser.poi_id),
            )
            if cursor.rowcount:
                key = f"{table}.{column}"
                report.moved[key] = report.moved.get(key, 0) + cursor.rowcount

        # 旧实体可能正是留下那行的父节点。改完引用之后，留下那行的
        # parent_poi_id 会指向它自己——一条自己当自己父节点的记录，
        # 会让「沿 parent 链走到根」变成一个死循环（ADR-0009 的归并逻辑）。
        conn.execute(
            "UPDATE poi SET parent_poi_id = NULL WHERE amap_poi_id = parent_poi_id"
        )

        # 旧行可能带着新行没有的字段（地址、评分、开放时间）。ADR-0001 说
        # 硬事实以官方接口为准，而留下的那行正是查过高德的那行，
        # 所以只在它为空时才补——不覆盖。
        conn.execute(
            "UPDATE poi SET "
            "  address = COALESCE(address, ?), "
            "  typecode = COALESCE(typecode, ?), "
            "  type = COALESCE(type, ?), "
            "  rating = COALESCE(rating, ?), "
            "  open_time = COALESCE(open_time, ?), "
            "  photo_url = COALESCE(photo_url, ?), "
            "  raw_json = COALESCE(raw_json, ?) "
            "WHERE amap_poi_id = ?",
            (
                loser.address,
                loser.typecode,
                loser.type_name,
                loser.rating,
                loser.open_time,
                loser.photo_url,
                loser.raw_json,
                group.survivor.poi_id,
            ),
        )
        conn.execute("DELETE FROM poi WHERE amap_poi_id = ?", (loser.poi_id,))
        report.merged += 1
    return report


def merge_duplicates(
    *, conn: sqlite3.Connection | None = None, dry_run: bool = False
) -> tuple[list[DuplicateGroup], MergeReport]:
    """体检并（可选地）执行合并。

    返回（发现的重名组，合并报告）。`dry_run=True` 时只找不改——
    默认就该是这一步：合并是不可逆的，先看清楚要动什么。
    """
    owned = conn is None
    active = conn or connect()
    try:
        groups = find_duplicates(active)
        if dry_run or not groups:
            return groups, MergeReport()

        report = MergeReport()
        for group in groups:
            part = merge_group(active, group)
            report.merged += part.merged
            for key, value in part.moved.items():
                report.moved[key] = report.moved.get(key, 0) + value
        active.commit()
        return groups, report
    finally:
        if owned:
            active.close()


def reference_counts(conn: sqlite3.Connection, poi_id: str) -> dict[str, int]:
    """某个实体被哪些表指着，各多少行。合并前后都用它核对。"""
    counts: dict[str, int] = {}
    for table, column in _REFERENCES:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?", (poi_id,)
        ).fetchone()
        if row["n"]:
            counts[f"{table}.{column}"] = row["n"]
    return counts
