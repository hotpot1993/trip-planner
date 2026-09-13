"""删掉一份行程及其全部下游数据，核对级联。

用法：python scripts/delete_trip.py <trip_id>

验证脚本跑出来的临时行程用它清掉，免得留在真实库里当成真数据。
"""

from __future__ import annotations

import sys

from _bootstrap import setup

setup()

from lushu.store import connect, transaction  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    trip_id = argv[0]

    conn = connect()
    try:
        before = {
            table: conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE trip_id = ?", (trip_id,)
            ).fetchone()["n"]
            for table in ("city_stay", "day", "intercity_transfer", "budget_item")
        }
    finally:
        conn.close()

    with transaction() as conn:
        removed = conn.execute("DELETE FROM trip WHERE id = ?", (trip_id,)).rowcount

    conn = connect()
    try:
        after = {
            table: conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE trip_id = ?", (trip_id,)
            ).fetchone()["n"]
            for table in ("city_stay", "day", "intercity_transfer", "budget_item")
        }
    finally:
        conn.close()

    print(f"删掉 {removed} 行 trip")
    for table, count in before.items():
        print(f"  {table:<20}删前 {count} → 删后 {after[table]}")
    if any(after.values()):
        print("有下游数据没被级联删掉——外键或 ON DELETE 配置有问题")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
