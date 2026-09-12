"""对运行中的服务跑一次真实的流式规划，把事件序列打出来。

用法：
    python scripts/plan_smoke.py ["需求原话"] [出发日期 YYYY-MM-DD] [天数]

不带参数时用一组默认值。这个脚本会真的调用高德与模型，跑一次一到几分钟。
"""

from __future__ import annotations

import json
import sys

import httpx

BASE = "http://127.0.0.1:8756"

DEFAULT_QUERY = "想去南京看博物馆和老建筑，节奏别太赶，别安排爬山"
DEFAULT_START = "2026-10-01"
DEFAULT_DAYS = 3


def main(argv: list[str]) -> int:
    query = argv[0] if argv else DEFAULT_QUERY
    start_date = argv[1] if len(argv) > 1 else DEFAULT_START
    days = int(argv[2]) if len(argv) > 2 else DEFAULT_DAYS

    print(f"需求      {query}")
    print(f"出发日期  {start_date}    天数 {days}")
    print()
    print("POST /api/plan/stream")
    print()

    stages = 0
    terminal: tuple[str, dict] | None = None

    # 一个节点可能要跑几十秒，读超时给足
    timeout = httpx.Timeout(connect=10, read=300, write=30, pool=10)
    with httpx.stream(
        "POST",
        f"{BASE}/api/plan/stream",
        json={"query": query, "start_date": start_date, "days": days},
        timeout=timeout,
    ) as response:
        print(f"  HTTP {response.status_code}  {response.headers.get('content-type')}")
        print()

        name = ""
        for line in response.iter_lines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
                if name == "stage":
                    stages += 1
                    print(f"  [{stages:>2}] {payload.get('label')}", flush=True)
                elif name in ("done", "error"):
                    terminal = (name, payload)

    print()
    if terminal is None:
        print("没有收到终止事件——连接可能中途断了")
        return 1

    name, payload = terminal
    if name == "done":
        print(f"完成：{payload.get('name')}  {payload.get('total_days')} 天  "
              f"{'、'.join(payload.get('city_names') or [])}")
        print(f"行程标识：{payload.get('trip_id')}")
        print()
        print(f"共 {stages} 个阶段事件")
        return 0

    print(f"失败：error={payload.get('error')}")
    print(f"      {payload.get('message')}")
    for key in ("missing_fields", "city_names", "missing_keys"):
        if payload.get(key):
            print(f"      {key}={payload[key]}")
    print()
    print(f"共 {stages} 个阶段事件")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
