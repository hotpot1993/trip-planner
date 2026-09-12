"""对运行中的服务做一次交付状态核验。不属于测试套件，只是手工冒烟。"""

from __future__ import annotations

import httpx

BASE = "http://127.0.0.1:8756"


def main() -> None:
    health = httpx.get(f"{BASE}/api/health", timeout=10).json()
    print(
        f"  服务        {health['status']} | 表结构版本 {health['schema_version']} "
        f"| 引擎 {'可用' if health['engine']['available'] else '不可用'}"
    )
    print(f"  缺配置      {'、'.join(health['warnings']) or '无'}")
    print(f"  前端        HTTP {httpx.get(BASE + '/', timeout=10).status_code}")

    trips = httpx.get(f"{BASE}/api/trips", timeout=10).json()
    print(f"  行程        {len(trips)} 份")
    for summary in trips:
        response = httpx.get(f"{BASE}/api/trips/{summary['id']}", timeout=10)
        body = response.json()
        items = sum(len(day["items"]) for stay in body["stays"] for day in stay["days"])
        unaligned = sum(
            1
            for stay in body["stays"]
            for day in stay["days"]
            for item in day["items"]
            if item["kind"] == "poi" and not item["poi_id"]
        )
        print(
            f"    {summary['name']:<14} HTTP {response.status_code}  "
            f"{body['total_days']} 天  {len(body['stays'])} 城  {items} 事项  待对齐 {unaligned}"
        )


if __name__ == "__main__":
    main()
