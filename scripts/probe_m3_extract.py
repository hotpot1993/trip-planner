"""探测 M3 提纯阶段的两件事：结构化输出能不能用，以及引文是否真的落在原文里。

M3 的第一个致命风险是「提纯质量无金标准集」，第二个更靠前、更硬：
**引文不许是模型编的**。设计里写死了一条规则——凡是在原文里找不到对应片段的
候选一律丢弃（docs/DESIGN.md 4.2）。这条规则能不能执行，取决于模型给出的
引文到底是不是原文的连续子串。

所以这个探测脚本不测「提纯准不准」（那要有标注集），只测三件可以用代码判定的事：

1. 结构化输出在这家模型上通不通（method 用 function_calling 还是 json_schema）
2. 一次调用返回几条候选、耗时多久、token 花了多少
3. 每条候选的引文能否在原文里定位到——精确子串、去空白子串、还是找不到

第 3 条才是关键。如果大量引文对不上，说明「引文必须落在原文里」这条设计
在模型层面就不成立，管线要么换模型要么换约束方式，必须先知道。

用法：.venv\\Scripts\\python.exe scripts\\probe_m3_extract.py
"""

from __future__ import annotations

import os
import time

from _bootstrap import setup

setup()

import httpx  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import lushu.config  # noqa: F401,E402  必须最先导入，引擎会在 import 期读环境变量

# ─── 素材 ────────────────────────────────────────────────────────
#
# 两篇都按中文攻略的真实形状写：一篇北京的故宫，一篇西安的博物馆。
# 里面刻意混入了几类典型内容——避坑、打卡、排队、交通、票价，也混入了
# 不带任何结论的纯叙述（"我们那年春天去的"），用来观察模型会不会硬凑。

MATERIAL_BEIJING = """北京旅行篇章：故宫参观攻略

去年十月我带爸妈去了趟故宫，踩了几个坑，写下来给后面的人省点事。

第一件事是入口。故宫现在只有午门能进，北门（神武门）只出不进。很多人从
地铁天安门东站出来以后跟着人流走，结果走到天安门城楼那边去了，那边是往
天安门广场的，不是故宫入口。正确的走法是从天安门东站 B 口出来，穿过天安门
城楼的门洞，再往前走到午门。这一段路要走二十分钟左右，夏天很晒。

第二件事是抢票。故宫不卖现场票，全部要提前预约，提前 7 天的晚上 8 点在
"故宫博物院"官方小程序放票。注意是晚上 8 点，不是零点，我第一次等到零点
白等了一场。周末和节假日的票基本是秒没，建议提前把同行人的身份证号都
填好，放票的时候直接选人，不然填信息的时间票就没了。

第三件事是路线。故宫太大了，一天走完中轴线就已经一万五千步。如果只有
半天，我建议只走中轴线：午门 → 太和殿 → 乾清宫 → 御花园 → 神武门出。
珍宝馆和钟表馆在东边，要另外买票，各 10 块，值得看，但是加上这两个馆
半天肯定不够。

第四件事是拍照。想拍没人的太和殿，唯一的办法是开门就冲。故宫早上 8 点半
开门，8 点 20 左右午门外面就排起队了。如果起不来，那就下午 4 点以后往
回走的时候拍，那时候大部分人已经往北门去了，中轴线上人会少一些。
角楼是另一个点，在神武门外面的筒子河边，下午的光线最好，很多人不知道
出了北门还能往西走一段。

最后说交通。故宫周边是禁停区，打车只能停到比较远的地方，我们那天被放在
了南池子大街，走进去又是十几分钟。地铁 1 号线天安门东站和天安门西站都能到，
8 号线金鱼胡同站离东华门近，但是东华门只能出不能进。
"""

MATERIAL_XIAN = """西安三天，博物馆和城墙

去西安主要为了看博物馆。这里说几个我实际遇到的问题。

陕西历史博物馆是免费的，但是必须预约，而且要提前 3 天在官方公众号预约，
每天早上 8 点放票。这个馆很难约，我约了两天才约上，节假日基本靠抢。
注意陕历博周一闭馆，全国的博物馆基本都是周一闭馆，安排行程的时候要避开。
如果实在约不上，可以考虑买大唐遗宝展的票进去，那个是要收费的，人少一些。

兵马俑不在西安市区，在临潼区，从西安北站坐地铁 14 号线再转 9 号线能到，
全程一个半小时。也可以坐游 5 路（306 路）从火车站东广场直达，票价 7 块，
但是这个车路上会拉你去买玉，不要下车，坚持坐到终点。兵马俑的门票是
120 块，需要提前在官网或者公众号买，现场不卖票。三个坑里一号坑最大最
震撼，但是人也最多，建议先去二号三号坑，最后回一号坑。

城墙我推荐傍晚上去，南门上，租自行车骑一圈，全程 13.7 公里，一个半小时
左右。白天上城墙太晒了，城墙上没有什么遮阴的地方。租车要押金，记得带
身份证。

回民街我个人的意见是不用专门去，商业化太严重，同样是吃肉夹馍和泡馍，
洒金桥那边更便宜也更地道。袁家村在咸阳，离市区一个多小时，如果时间紧
可以不去。

大唐不夜城晚上去，人是真的多，尤其是七点到九点。不倒翁小姐姐那个表演
现在好像已经没有了，不要专门为了这个去。
"""

MATERIALS = [
    ("beijing", "北京旅行篇章：故宫参观攻略", MATERIAL_BEIJING),
    ("xian", "西安三天，博物馆和城墙", MATERIAL_XIAN),
]


# ─── 提纯的输出结构 ──────────────────────────────────────────────
#
# 字段刻意从紧：subject / polarity / facet 都做成枚举，让模型只能在既定
# 分类里选，避免它自由发挥出一堆同义标签。quote 是唯一的证据字段。


class ExtractedClaim(BaseModel):
    """一条待验证的经验。产出不是结论，只是候选。"""

    subject_name: str = Field(description="这条结论针对的景点、路线或城市，用原文里的叫法")
    subject_type: str = Field(description="poi 景点级 / route 路线级 / city 城市级")
    polarity: str = Field(description="avoid 避坑 / highlight 打卡建议")
    facet: str = Field(
        description=(
            "queue 排队 / entrance 入口 / hours 时段 / crowd 人流 / photo 拍照 / "
            "transit 交通 / price_diff 票价差异 / closure 闭馆 / other 其它"
        )
    )
    text: str = Field(description="归一化后的结论，一句话，不要照抄原文")
    quote: str = Field(description="支持这条结论的原文片段，必须是原文的连续子串，一字不改")


class Extraction(BaseModel):
    """一篇素材的提纯结果。"""

    claims: list[ExtractedClaim] = Field(description="抽出的候选结论，没有就给空列表")


PROMPT = """你在为一份旅行攻略做资料整理。下面是一篇网友写的游记或攻略。

请把里面**作者亲身经历或明确断言**的经验抽出来，归成一条条候选结论。

规则：

1. 每条结论必须给出支持它的原文片段，放在 quote 字段里。**quote 必须是原文的
   连续子串，一个字都不能改**——不要补标点，不要改错别字，不要合并两段。
2. 只抽「会影响别人行程决定」的信息。纯叙述、心情、天气、个人偏好不必抽。
3. 硬事实（门票多少钱、几点开门、哪个站换乘）如果作者写得很明确，可以抽；
   但不要把作者的猜测当成事实。
4. text 是你归一化之后的一句话结论，用陈述句，不要照抄 quote。
5. 同一件事只抽一条，不要重复。

原文标题：{title}

原文正文：
{body}
"""


def locate(quote: str, body: str) -> tuple[str, int]:
    """在原文里定位引文。返回（判定, 位置）。

    三档判定：
      exact    —— 一字不差的连续子串，最理想
      loose    —— 去掉所有空白与常见标点后能找到，属于可接受（模型改了标点）
      missing  —— 找不到，这条候选按设计规则必须丢弃
    """
    if not quote.strip():
        return "empty", -1
    pos = body.find(quote)
    if pos >= 0:
        return "exact", pos

    strip_chars = set(" \t\r\n　，。、；：！？\u201c\u201d\u2018\u2019（）《》…—·")
    squashed_body = "".join(c for c in body if c not in strip_chars)
    squashed_quote = "".join(c for c in quote if c not in strip_chars)
    pos = squashed_body.find(squashed_quote)
    if pos >= 0:
        return "loose", pos
    return "missing", -1


def build_model(schema: type[BaseModel], method: str):
    from langchain_openai import ChatOpenAI

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()

    llm = ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0,
        http_client=httpx.Client(trust_env=True),
        http_async_client=httpx.AsyncClient(trust_env=True),
        extra_body={"thinking": {"type": "disabled"}},
    )
    if method == "plain":
        return llm, llm.with_structured_output(schema)
    return llm, llm.with_structured_output(schema, method=method, strict=True)


def run_method(method: str) -> bool:
    print(f"\n{'=' * 70}")
    print(f"  结构化输出方式：{method}")
    print("=" * 70)

    try:
        llm, structured = build_model(Extraction, method)
    except Exception as exc:  # noqa: BLE001
        print(f"  建模失败：{type(exc).__name__}: {exc}")
        return False

    total_exact = total_loose = total_missing = 0

    for key, title, body in MATERIALS:
        prompt = PROMPT.format(title=title, body=body)
        started = time.time()
        try:
            result: Extraction = structured.invoke(prompt)
        except Exception as exc:  # noqa: BLE001
            print(f"\n  [{key}] 调用失败：{type(exc).__name__}: {exc}")
            return False
        elapsed = time.time() - started

        claims = result.claims
        print(f"\n  [{key}] {title}")
        print(f"    正文 {len(body)} 字，抽出 {len(claims)} 条，耗时 {elapsed:.1f}s")

        counts = {"exact": 0, "loose": 0, "missing": 0, "empty": 0}
        for claim in claims:
            verdict, _ = locate(claim.quote, body)
            counts[verdict] += 1
            mark = {"exact": "✓", "loose": "~", "missing": "✗", "empty": "-"}[verdict]
            print(f"      {mark} [{claim.subject_type}/{claim.polarity}/{claim.facet}] "
                  f"{claim.subject_name}")
            print(f"         结论：{claim.text}")
            print(f"         引文：{claim.quote[:60]}{'…' if len(claim.quote) > 60 else ''}")

        total_exact += counts["exact"]
        total_loose += counts["loose"]
        total_missing += counts["missing"]

        print(f"    引文命中：精确 {counts['exact']} / 宽松 {counts['loose']} / "
              f"找不到 {counts['missing']} / 空 {counts['empty']}")

    total = total_exact + total_loose + total_missing
    if total:
        print(f"\n  合计 {total} 条，精确命中 {total_exact} "
              f"({total_exact / total:.0%})，找不到 {total_missing} "
              f"({total_missing / total:.0%})")
    return True


def main() -> int:
    print("M3 提纯探测：结构化输出 + 引文可定位性")
    print(f"  模型 {os.getenv('DEEPSEEK_MODEL', '(默认)')}，"
          f"共 {len(MATERIALS)} 篇素材")

    for method in ("function_calling", "json_schema"):
        if run_method(method):
            return 0
        print(f"\n  {method} 不通，换下一种。")

    print("\n  两种结构化输出方式都不通。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
