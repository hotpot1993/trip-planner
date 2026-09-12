"""端到端跑一遍 M3 的完整链路，用真实模型与真实高德接口。

**这不是评测。** 评测要金标准集（`ls eval`，还没做），这里只是确认
四步在真实数据上串得起来，并把可以量化的东西量出来：

- 导入的判重与第一层归组（照搬、节选能不能认出来）
- 提纯的引文接地率（能不能全部落回原文）
- 对齐的成功率与归并条数（「午门」有没有挂到故宫博物院上）
- 第二层归组（逐句改写的洗稿能不能并成一个来源）
- 置信度分布（**这一项是整条链路最要紧的输出**）

素材是自写的四篇中文攻略 + 一篇逐句改写 + 两篇节选转载。
它们不构成评测集，但足以把「同一篇被转载多次只算一个来源」这件事
在真实数据上验一遍。

会花钱：每次运行约 8 次模型调用。结果进 `llm_cache`，重跑不再付费。

用法：.venv\\Scripts\\python.exe scripts\\verify_pipeline.py
"""

from __future__ import annotations

from _bootstrap import setup

setup()

from lushu.engine.amap import resolve_city  # noqa: E402
from lushu.services import knowledge_store as ks  # noqa: E402
from lushu.services.ingest import ImportRequest, import_document  # noqa: E402
from lushu.services.pipeline import (  # noqa: E402
    align_pending,
    extract_documents,
    group_by_conclusions,
    merge_claims,
    pipeline_stats,
)
from lushu.services.trip_store import CityRef, upsert_cities  # noqa: E402
from lushu.store import connect, initialize  # noqa: E402

# ─── 素材 ────────────────────────────────────────────────────────
#
# key 是素材标识，值给出标题、正文，以及它是不是某一篇的变体。
# `variant_of` 用来在报告里核对归组结果对不对。

BEIJING = """北京旅行篇章：故宫参观攻略

去年十月我带爸妈去了趟故宫，踩了几个坑，写下来给后面的人省点事。

第一件事是入口。故宫现在只有午门能进，北门（神武门）只出不进。很多人从
地铁天安门东站出来以后跟着人流走，结果走到天安门城楼那边去了，那边是往
天安门广场的，不是故宫入口。正确的走法是从天安门东站B口出来，穿过天安门
城楼的门洞，再往前走到午门。这一段路要走二十分钟左右，夏天很晒。

第二件事是抢票。故宫不卖现场票，全部要提前预约，提前7天的晚上8点在
"故宫博物院"官方小程序放票。注意是晚上8点，不是零点，我第一次等到零点
白等了一场。周末和节假日的票基本是秒没，建议提前把同行人的身份证号都
填好，放票的时候直接选人。

第三件事是路线。故宫太大了，一天走完中轴线就已经一万五千步。如果只有
半天，我建议只走中轴线：午门 → 太和殿 → 乾清宫 → 御花园 → 神武门出。
珍宝馆和钟表馆在东边，要另外买票，各 10 块，值得看。

第四件事是拍照。想拍没人的太和殿，唯一的办法是开门就冲。故宫早上 8 点半
开门，8 点 20 左右午门外面就排起队了。角楼是另一个点，在神武门外面的筒子河边，
下午的光线最好，很多人不知道出了北门还能往西走一段。

最后说交通。故宫周边是禁停区，打车只能停到比较远的地方。地铁 1 号线
天安门东站和天安门西站都能到，8 号线金鱼胡同站离东华门近，
但是东华门只能出不能进。"""

XIAN = """西安三天，博物馆和城墙

去西安主要为了看博物馆。这里说几个我实际遇到的问题。

陕西历史博物馆是免费的，但是必须预约，而且要提前 3 天在官方公众号预约，
每天早上 8 点放票。这个馆很难约，我约了两天才约上，节假日基本靠抢。
注意陕历博周一闭馆，安排行程的时候要避开。如果实在约不上，可以考虑买
大唐遗宝展的票进去，那个是要收费的，人少一些。

兵马俑不在西安市区，在临潼区，从西安北站坐地铁 14 号线再转 9 号线能到，
全程一个半小时。也可以坐游 5 路（306 路）从火车站东广场直达，票价 7 块，
但是这个车路上会拉你去买玉，不要下车，坚持坐到终点。兵马俑的门票是
120 块，需要提前在官网或者公众号买，现场不卖票。

城墙我推荐傍晚上去，南门上，租自行车骑一圈，全程 13.7 公里，一个半小时
左右。白天上城墙太晒了，城墙上没有什么遮阴的地方。

回民街我个人的意见是不用专门去，商业化太严重，同样是吃肉夹馍和泡馍，
洒金桥那边更便宜也更地道。

大唐不夜城晚上去，人是真的多，尤其是七点到九点。"""

# 逐句改写的洗稿：事实点全在，但没有一句和 XIAN 一样。
# 第一层的文字复制认不出它，要靠第二层的结论重合度（ADR-0008）。
XIAN_REWRITTEN = """西安玩了三天，说几个坑。

先说陕历博。这个馆不要钱，但得预约，官方公众号提前三天放，每天早八点开抢。
我是抢了两天才抢到的，放假的时候基本看运气。周一它不开门，别安排在那天。
要是实在抢不到，买大唐遗宝展的票也能进，那个要花钱，但里面人少。

兵马俑离市区很远，在临潼那边。可以从西安北站坐十四号线换九号线过去，
路上要一个半小时。火车站东广场也有游五路直达，七块钱，不过司机会拉你去
买玉，千万别下车。门票一百二，官网或者公众号上买，现场买不到。

城墙傍晚去最好，从南门上，租个自行车绕一圈是十三点七公里，一个半小时
能骑完。白天去太晒，墙上没地方躲。

回民街我觉得不用特意去，太商业化了。吃肉夹馍和泡馍去洒金桥，便宜而且正宗。

大唐不夜城晚上人特别多，七八九点最挤。"""

# 节选转载：只摘了其中一段，前面加了编者按
XIAN_EXCERPT = """【转】西安博物馆预约提示：

陕西历史博物馆是免费的，但是必须预约，而且要提前 3 天在官方公众号预约，
每天早上 8 点放票。注意陕历博周一闭馆，安排行程的时候要避开。

（本文转自网络，供参考）"""

# 另一座城市，用来验证不会误并
NANJING = """南京周末两天，写给第一次去的人

南京博物院要提前预约，官方公众号放票，周末很难约，建议提前三天盯着。
周一闭馆，全国的博物馆基本都是周一闭馆。

中山陵也是免费但要预约，而且陵园里面很大，从停车场走到台阶下面就要
二十分钟。建议穿运动鞋。

夫子庙晚上去，秦淮河的灯是好看，但是人是真的多，节假日基本走不动。
吃东西我建议去老门东，比夫子庙那边便宜一些。

明孝陵和中山陵可以一天走完，两个地方挨着。明孝陵的石象路秋天最好看，
十一月去正好。"""

MATERIALS: dict[str, dict] = {
    "beijing": {
        "title": "北京旅行篇章：故宫参观攻略",
        "body": BEIJING,
        "url": "https://www.mafengwo.cn/i/1001.html",
    },
    "xian": {
        "title": "西安三天，博物馆和城墙",
        "body": XIAN,
        "url": "https://zhuanlan.zhihu.com/p/2002",
    },
    "xian_rewritten": {
        "title": "西安玩了三天，说几个坑",
        "body": XIAN_REWRITTEN,
        "url": "https://www.qyer.com/a/3003",
        "variant_of": "xian",
    },
    "xian_excerpt": {
        "title": "【转】西安博物馆预约提示",
        "body": XIAN_EXCERPT,
        "url": "https://you.ctrip.com/travels/xian1/4004.html",
        "variant_of": "xian",
    },
    "nanjing": {
        "title": "南京周末两天",
        "body": NANJING,
        "url": "https://www.mafengwo.cn/i/5005.html",
    },
}

# 素材里提到的城市。城市要先入库，对齐时才拿得到 adcode。
CITY_NAMES = ("北京", "西安", "南京")


def seed_cities() -> dict[str, str]:
    """把素材涉及的城市解析出来入库。返回 {城市名: adcode}。"""
    refs: list[CityRef] = []
    mapping: dict[str, str] = {}
    for name in CITY_NAMES:
        match = resolve_city(name)
        if match is None:
            print(f"  城市解析失败：{name}")
            continue
        mapping[name] = match.adcode
        refs.append(
            CityRef(
                adcode=match.adcode,
                name=name,  # 用我们的叫法，不用高德返回的「北京城区」
                lat_gcj02=match.lat_gcj02,
                lng_gcj02=match.lng_gcj02,
            )
        )
    upsert_cities(refs)
    return mapping


def main() -> int:
    initialize()
    print("M3 端到端验证（真实模型 + 真实高德）")
    print()

    print("① 城市解析与入库")
    cities = seed_cities()
    print("  " + "，".join(f"{k}={v}" for k, v in cities.items()))
    print()

    print("② 导入素材")
    before = pipeline_stats()
    document_ids: dict[str, str] = {}
    conn = connect()
    try:
        for key, material in MATERIALS.items():
            result = import_document(
                ImportRequest(
                    body=material["body"],
                    title=material["title"],
                    url=material["url"],
                ),
                conn=conn,
            )
            if result.duplicate.value == "exact":
                # 之前跑过一遍、素材已经在库里了：按指纹找回来
                row = conn.execute(
                    "SELECT id FROM source_document WHERE title = ? LIMIT 1",
                    (material["title"],),
                ).fetchone()
                document_ids[key] = row["id"] if row else result.document_id
                mark = "已在库"
            else:
                document_ids[key] = result.document_id
                mark = {"new": "新", "repost": "转载"}[result.duplicate.value]
            extra = ""
            if result.duplicate.value == "repost" and result.coverage is not None:
                extra = f"，与已有素材重合 {result.coverage:.0%}"
            print(f"  [{mark}] {material['title'][:24]}{extra}")
    finally:
        conn.close()
    print()

    print("③ 提纯")
    extracted = extract_documents(limit=50)
    print(f"  {extracted.documents} 篇，模型吐出 {extracted.candidates} 条候选")
    print(f"  引文校验通过 {extracted.accepted}，丢弃 {extracted.dropped}"
          + (f"（{extracted.drop_ratio:.0%}）" if extracted.candidates else ""))
    print(f"  宽松命中 {extracted.loose} 条")
    for document_id, error in extracted.failed:
        print(f"  失败 {document_id}：{error}")
    print()

    print("④ 对齐")
    aligned = align_pending(limit=50)
    print(f"  处理 {aligned.mentions} 条提及")
    print(f"  对上并落库 {aligned.aligned} 条，其中 {aligned.collapsed} 条由子点归并到本体")
    print(f"  仍待人工 {aligned.pending} 条，缺城市线索 {aligned.unresolved_subjects} 条")
    for name, error in aligned.failed:
        print(f"  失败「{name}」：{error}")

    conn = connect()
    try:
        tasks = ks.pending_alignments(conn=conn, limit=50)
        if tasks:
            print("  待人工的提及：")
            for task in tasks[:10]:
                reason = (task.context_snippet or "")[:56]
                print(f"    「{task.mention_name}」 {reason}")
    finally:
        conn.close()
    print()

    print("⑤ 第二层归组（按结论重合度）")
    grouped = group_by_conclusions()
    print(f"  比对 {grouped.compared} 对，合并 {grouped.merged} 篇，"
          f"现有 {grouped.groups} 个来源组")
    print()

    print("⑥ 合并同义结论并重算置信度")
    merged = merge_claims()
    print(f"  结论 {merged.created} 条：高置信 {merged.high_confidence}，"
          f"待验证个例 {merged.single_source}")
    print()

    print("⑦ 结果")
    conn = connect()
    try:
        print("  来源组：")
        for row in conn.execute(
            "SELECT g.id, g.basis, COUNT(d.id) AS n, "
            "       GROUP_CONCAT(COALESCE(d.title, d.id), ' | ') AS titles "
            "FROM source_group g LEFT JOIN source_document d ON d.source_group_id = g.id "
            "GROUP BY g.id ORDER BY n DESC"
        ):
            print(f"    {row['id']}（{row['basis']}）{row['n']} 篇：{row['titles'][:70]}")

        print()
        print("  独立来源数分布：")
        for row in conn.execute(
            "SELECT independent_source_count AS n, confidence, COUNT(*) AS c "
            "FROM claim GROUP BY n, confidence ORDER BY n DESC"
        ):
            print(f"    {row['n']} 个来源（{row['confidence']}）：{row['c']} 条结论")

        print()
        print("  证据最多的几条结论：")
        for row in conn.execute(
            "SELECT c.text, c.confidence, c.independent_source_count AS n, "
            "       p.name AS poi_name "
            "FROM claim c LEFT JOIN poi p ON p.amap_poi_id = c.poi_id "
            "ORDER BY c.independent_source_count DESC, c.text LIMIT 8"
        ):
            print(f"    [{row['n']} 源/{row['confidence']}] {row['poi_name'] or '?'}："
                  f"{row['text'][:52]}")

        print()
        print("  结论挂到了哪些景点上：")
        for row in conn.execute(
            "SELECT p.name, COUNT(c.id) AS n FROM claim c "
            "JOIN poi p ON p.amap_poi_id = c.poi_id "
            "GROUP BY p.name ORDER BY n DESC LIMIT 12"
        ):
            print(f"    {row['name']}：{row['n']} 条")
    finally:
        conn.close()

    print()
    after = pipeline_stats()
    print("  本次新增：素材 "
          f"{after['documents'] - before['documents']}，结论 "
          f"{after['claims'] - before['claims']}，证据 "
          f"{after['evidence'] - before['evidence']}")
    print()
    print("  注意：这不是评测。抽取精确率与召回率要金标准集（ls eval，还没做）。")
    print("  这里能量的是接地率、对齐成功率、归组是否生效与置信度分布。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
