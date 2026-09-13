"""把一份行程导出成单个 HTML 路书，并跑一遍契约校验。

用法：
    python scripts/export_roadbook.py <trip_id> [输出路径]
    python scripts/export_roadbook.py --list

设计第八节说「生成后必须跑一次契约校验，有错误必须修复后重跑」——
这个脚本就是那句话的执行点：**有错误就不写文件**，免得一份不能用的路书
被导到手机上，而人到了当地才发现。
"""

from __future__ import annotations

import sys
from pathlib import Path

from _bootstrap import setup

setup()

from lushu.domain.roadbook import Severity, errors, validate  # noqa: E402
from lushu.services import roadbook_render as render  # noqa: E402
from lushu.services import roadbook_service  # noqa: E402
from lushu.store import connect  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv or argv[0] == "--list":
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT id, name, start_date FROM trip ORDER BY created_at DESC"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            print("库里还没有行程")
            return 0
        for row in rows:
            print(f"{row['id']}  {row['name']}  {row['start_date']}")
        return 0

    trip_id = argv[0]
    try:
        book, warnings = roadbook_service.assemble(trip_id)
    except ValueError as exc:
        print(f"装配失败：{exc}")
        return 1

    problems = validate(book)
    fatal = errors(problems)

    print(f"《{book.name}》{book.total_days} 天、{book.item_count} 个天项、"
          f"{len(book.bookings)} 条预约")

    if warnings:
        print()
        print("装配时的提醒：")
        for item in warnings:
            print(f"  ! {item}")

    if problems:
        print()
        print("契约校验：")
        for item in problems:
            print(f"  {item}")

    if fatal:
        print()
        print(f"有 {len(fatal)} 处错误，**没有写出文件**——")
        print("一份在地点上打不开的路书比没有更糟：人会以为带上了。")
        return 1

    html = render.render(book)

    # 离线可读是硬要求，这里是它最后的把关：产物里不能有**页面自动加载**的
    # 外部资源。导航深链不算——那是用户主动点才会跳转的，断网时最多点了没反应。
    refs = render.external_resources(html)
    if refs:
        print(f"产物里有 {len(refs)} 处会自动加载的外部资源，不能算离线可读：")
        for ref in refs[:5]:
            print(f"  {ref}")
        return 1

    target = Path(argv[1]) if len(argv) > 1 else Path("data") / f"{trip_id}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")

    size_kb = target.stat().st_size / 1024
    print()
    print(f"已写出 {target}（{size_kb:.0f} KB，零外部引用，可离线打开）")
    other = [item for item in problems if item.severity is not Severity.ERROR]
    if other:
        print(f"契约提醒 {len(other)} 处（不影响打开，见上）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
