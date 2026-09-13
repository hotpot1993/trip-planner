"""查一条提及为什么挂到了它挂的那个 POI 上。

评测报出「挂错」之后，下一步就是问「为什么」。这个脚本把一条结论的
完整来路打出来：claim 挂在哪、它的父节点是谁、当初那张待办里高德给了
哪些候选、每个候选的拒绝理由是什么。

用法：
    python scripts/probe_alignment_trace.py 太和殿
    python scripts/probe_alignment_trace.py --claim clm_xxx
"""

from __future__ import annotations

import sys

from _bootstrap import setup

setup()

from lushu.services import knowledge_store as ks  # noqa: E402
from lushu.store import connect  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    conn = connect()
    try:
        if argv[0] == "--claim":
            claims = [row for row in ks.list_claims(limit=1000) if row.claim_id == argv[1]]
        else:
            claims = [row for row in ks.list_claims(limit=1000) if row.subject_name == argv[0]]

        if not claims:
            print(f"没找到提及为「{argv[0]}」的结论")
            return 1

        for claim in claims:
            print(f"\n结论 {claim.claim_id}  「{claim.subject_name}」")
            print(f"  挂在       {claim.poi_id}")
            print(f"  poi 行：{_poi_line(conn, claim.poi_id)}")
            print(f"  父节点：  {_poi_line(conn, _parent_of(conn, claim.poi_id))}")
            print(f"  极性/facet {claim.polarity}/{claim.facet}")
            print(f"  结论      {claim.text}")
            print(f"  独立来源  {claim.independent_source_count}，证据 {claim.evidence_count} 条")

        name = claims[0].subject_name
        tasks = conn.execute(
            "SELECT * FROM alignment_task WHERE mention_name = ? ORDER BY created_at",
            (name,),
        ).fetchall()
        print(f"\n提及「{name}」的待办 {len(tasks)} 张：")
        for task in tasks:
            print(f"\n  {task['id']}  状态 {task['status']}  解析到 {task['resolved_poi_id']}")
            print(f"    城市线索 {task['city_adcode']}")
            for item in ks.dict_items(task["candidate_pois_json"]):
                flag = "" if item.get("usable") else f"  ✗ {item.get('reject_reason')}"
                print(f"    {item.get('poi_id')}  {item.get('name')}{flag}")
    finally:
        conn.close()
    return 0


def _parent_of(conn, poi_id: str | None) -> str | None:
    if not poi_id:
        return None
    row = conn.execute(
        "SELECT parent_poi_id FROM poi WHERE amap_poi_id = ?", (poi_id,)
    ).fetchone()
    return row["parent_poi_id"] if row else None


def _poi_line(conn, poi_id: str | None) -> str:
    if not poi_id:
        return "（无）"
    row = conn.execute(
        "SELECT name, typecode, parent_poi_id FROM poi WHERE amap_poi_id = ?", (poi_id,)
    ).fetchone()
    if row is None:
        return f"{poi_id}（poi 表里没有这一行）"
    return f"{poi_id}  {row['name']}  类型 {row['typecode']}  父 {row['parent_poi_id']}"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
