"""用真实模型验证提纯适配器：引文能不能全部落回原文。

单测里的模型是假的，只能验证「管道通不通」。这个脚本打真接口，验证三件事：

1. `lushu/adapters/extract.py` 的提示词在实际调用下产出的是不是**精确子串引文**
   ——这是 `lushu/domain/extraction.py` 的 `verify_extraction()` 能执行的前提
2. 缓存第二次调用是否真的不打接口（省钱这件事要能验证，不能只写在注释里）
3. 会花多少 token 与时间——批量提纯的成本要先知道

用法：.venv\\Scripts\\python.exe scripts\\verify_extract.py
"""

from __future__ import annotations

import time

from _bootstrap import setup

setup()

from lushu.adapters.extract import ExtractionError, extract  # noqa: E402
from lushu.domain.extraction import QuoteVerdict, verify_extraction  # noqa: E402
from lushu.store import connect, initialize  # noqa: E402

# 实测素材（与 scripts/probe_m3_extract.py 用的同一批），
# 刻意包含换行、直角引号、全角标点这些容易被「顺手修正」的东西
MATERIALS = [
    (
        "北京旅行篇章：故宫参观攻略",
        """去年十月我带爸妈去了趟故宫，踩了几个坑，写下来给后面的人省点事。

第一件事是入口。故宫现在只有午门能进，北门（神武门）只出不进。很多人从
地铁天安门东站出来以后跟着人流走，结果走到天安门城楼那边去了，那边是往
天安门广场的，不是故宫入口。正确的走法是从天安门东站B口出来，穿过天安门
城楼的门洞，再往前走到午门。这一段路要走二十分钟左右，夏天很晒。

第二件事是抢票。故宫不卖现场票，全部要提前预约，提前7天的晚上8点在
"故宫博物院"官方小程序放票。注意是晚上8点，不是零点，我第一次等到零点
白等了一场。周末和节假日的票基本是秒没，建议提前把同行人的身份证号都
填好，放票的时候直接选人，不然填信息的时间票就没了。

第四件事是拍照。想拍没人的太和殿，唯一的办法是开门就冲。故宫早上8点半
开门，8点20左右午门外面就排起队了。角楼是另一个点，在神武门外面的筒子河边，
下午的光线最好。""",
    ),
    (
        "西安三天，博物馆和城墙",
        """去西安主要为了看博物馆。这里说几个我实际遇到的问题。

陕西历史博物馆是免费的，但是必须预约，而且要提前3天在官方公众号预约，
每天早上8点放票。注意陕历博周一闭馆，安排行程的时候要避开。
如果实在约不上，可以考虑买大唐遗宝展的票进去，那个是要收费的，人少一些。

兵马俑不在西安市区，在临潼区，从西安北站坐地铁14号线再转9号线能到，
全程一个半小时。也可以坐游5路（306路）从火车站东广场直达，票价7块，
但是这个车路上会拉你去买玉，不要下车，坚持坐到终点。

城墙我推荐傍晚上去，南门上，租自行车骑一圈，全程13.7公里，一个半小时
左右。白天上城墙太晒了，城墙上没有什么遮阴的地方。

回民街我个人的意见是不用专门去，商业化太严重，同样是吃肉夹馍和泡馍，
洒金桥那边更便宜也更地道。""",
    ),
]


def main() -> int:
    db = "data/lushu.db"
    initialize(db)
    conn = connect(db)

    print("真实模型验证：提纯适配器")
    print(f"  素材 {len(MATERIALS)} 篇，缓存库 {db}")
    print()

    total_candidates = total_accepted = total_dropped = total_loose = 0
    total_ms = 0

    for title, body in MATERIALS:
        started = time.monotonic()
        try:
            raw = extract(title=title, body=body, conn=conn)
        except ExtractionError as exc:
            print(f"  [{title}] 调用失败：{exc}")
            conn.close()
            return 1
        wall_ms = int((time.monotonic() - started) * 1000)

        outcome = verify_extraction(
            "verify",
            body,
            raw.claims,
            model=raw.model,
            prompt_version=raw.prompt_version,
            duration_ms=raw.duration_ms,
        )

        print(f"  [{title}]  正文 {len(body)} 字")
        print(f"    模型 {raw.model}，提示词 {raw.prompt_version}"
              f"{'（命中缓存，未打接口）' if raw.from_cache else ''}")
        print(f"    模型吐出 {len(raw.claims)} 条，接口耗时 {raw.duration_ms}ms，"
              f"实际等待 {wall_ms}ms")
        print(f"    引文校验：通过 {len(outcome.accepted)}，"
              f"丢弃 {len(outcome.dropped)}，其中宽松命中 {outcome.loose_count}")

        for verified in outcome.accepted:
            claim = verified.claim
            mark = "=" if verified.location.verdict is QuoteVerdict.EXACT else "~"
            print(f"      {mark} [{claim.polarity.value}/{claim.facet.value}] {claim.subject_name}")
            print(f"         {claim.text}")
            print(f"         引文（{verified.location.start}-{verified.location.end}）"
                  f"{claim.quote[:44]}{'…' if len(claim.quote) > 44 else ''}")

        for claim, verdict in outcome.dropped:
            print(f"      ✗ {verdict.value}：{claim.text[:40]}")

        total_candidates += outcome.candidate_count
        total_accepted += len(outcome.accepted)
        total_dropped += len(outcome.dropped)
        total_loose += outcome.loose_count
        total_ms += wall_ms
        print()

    print("=" * 72)
    print(f"  合计 {total_candidates} 条候选：通过 {total_accepted}，丢弃 {total_dropped}")
    if total_accepted:
        print(f"  **精确子串命中率 {100 * (total_accepted - total_loose) / total_accepted:.0f}%**"
              f"（宽松命中 {total_loose} 条）")
    print(f"  总耗时 {total_ms / 1000:.1f}s")

    # 第二次跑应当全部命中缓存
    print()
    print("  再跑一次，验证缓存：")
    cached_ms = 0
    for title, body in MATERIALS:
        started = time.monotonic()
        raw = extract(title=title, body=body, conn=conn)
        cached_ms += int((time.monotonic() - started) * 1000)
        print(f"    [{title}] {'命中缓存' if raw.from_cache else '**又打了接口**'}")
    print(f"    缓存总耗时 {cached_ms}ms（上一次 {total_ms}ms）")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
