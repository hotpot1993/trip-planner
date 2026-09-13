"""核对一篇素材的候选在三个地方各有多少条。

评测的预测池取自 `extraction_run.accepted_json`（迁移 9 之后），老数据要
从待对齐队列与证据行里捡回来。这个脚本把三处的条数并排列出来，
用来确认「捡回来的」与「当初抽出来的」是不是同一批东西。

用法：python scripts/probe_gold_coverage.py
"""

from __future__ import annotations

import json

from _bootstrap import setup

setup()

from lushu.store import connect  # noqa: E402


def main() -> int:
    conn = connect()
    try:
        docs = conn.execute(
            "SELECT id, COALESCE(title, '(无标题)') AS title FROM source_document "
            "ORDER BY imported_at"
        ).fetchall()
        for doc in docs:
            runs = conn.execute(
                "SELECT id, accepted_count, dropped_count, created_at, accepted_json "
                "FROM extraction_run WHERE source_document_id = ? AND status = 'ok' "
                "ORDER BY created_at, rowid",
                (doc["id"],),
            ).fetchall()
            tasks = conn.execute(
                "SELECT extracted_claims_json FROM alignment_task WHERE source_document_id = ?",
                (doc["id"],),
            ).fetchall()
            evidence = conn.execute(
                "SELECT COUNT(*) AS n FROM claim_evidence WHERE source_document_id = ?",
                (doc["id"],),
            ).fetchone()["n"]

            in_tasks = 0
            for task in tasks:
                try:
                    payload = json.loads(task["extracted_claims_json"] or "[]")
                except json.JSONDecodeError:
                    continue
                in_tasks += len(payload) if isinstance(payload, list) else 0

            accepted = runs[-1]["accepted_count"] if runs else 0
            payload_len = 0
            if runs and runs[-1]["accepted_json"]:
                payload = json.loads(runs[-1]["accepted_json"])
                payload_len = len(payload) if isinstance(payload, list) else 0

            print(f"\n{doc['id']}  《{doc['title']}》")
            print(f"  提纯运行 {len(runs)} 次，最后一次 accepted_count = {accepted}")
            print(f"  accepted_json 条数 {payload_len}")
            print(f"  待对齐队列载荷合计 {in_tasks}（{len(tasks)} 张待办）")
            print(f"  证据行 {evidence} 条")
            if accepted != payload_len and payload_len:
                print(f"  ⚠ accepted_count({accepted}) 与载荷({payload_len}) 对不上")
            if not payload_len and accepted:
                print("  （老数据：预测池要从待办与证据行里捡）")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
