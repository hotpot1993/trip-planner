"""看一眼某篇素材的正文与模型候选，供人工标注时抄引文用。

标注要靠人读原文写结论，而原文在库里。这个脚本把它连同模型抽的候选一起
打出来——`ls eval gold --show` 给的是标注条目，这里给的是标注**材料**。

用法：
    python scripts/probe_gold_material.py                 # 列出所有素材
    python scripts/probe_gold_material.py src_xxx         # 看这一篇
"""

from __future__ import annotations

import sys

from _bootstrap import setup

setup()

from lushu.services import evaluation as ev  # noqa: E402
from lushu.services import gold_store as gs  # noqa: E402
from lushu.store import connect  # noqa: E402


def main(argv: list[str]) -> int:
    documents = gs.gold_documents()
    if not argv:
        for item in documents:
            flag = "已标注" if item.in_gold_set else "未标注"
            print(f"{item.document_id}  [{flag}]  {item.title}  候选 {item.prediction_count} 条")
        return 0

    document_id = argv[0]
    conn = connect()
    try:
        row = conn.execute(
            "SELECT title, body_text FROM source_document WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None:
            print(f"没有这篇素材：{document_id}")
            return 1
        predictions = ev.predictions_for_document(document_id, conn=conn)
        labels = gs.labels_for_document(document_id, conn=conn)
    finally:
        conn.close()

    print(f"《{row['title']}》  {document_id}")
    print("=" * 72)
    print(row["body_text"])
    print("=" * 72)
    print(f"模型抽出的候选 {len(predictions)} 条（**这是模型说的，不是原文**）：")
    for index, item in enumerate(predictions, 1):
        offset = (
            f"{item.char_start}-{item.char_end}"
            if item.char_start is not None
            else "无位置"
        )
        print(f"\n  {index:>2}. [{item.polarity}/{item.facet}] {item.subject_name}  {offset}")
        print(f"      结论：{item.text}")
        print(f"      引文：{item.quote}")

    print()
    print(f"已有标注 {len(labels)} 条：")
    for item in labels:
        print(f"  [{item.polarity}] {item.subject_name or '（未写主体）'}：{item.quote}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
