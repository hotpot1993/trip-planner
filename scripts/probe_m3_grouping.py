"""探测 M3 独立来源归组：中文转载与洗稿到底能不能靠正文相似度认出来。

设计里把这一步定为「必须先于置信度计算」（docs/DESIGN.md 4.3），理由是
中文旅游攻略的转载与洗稿极其普遍。风险表里写得更直白：

    独立来源去重失效 → 置信度退化成转发量 → 整个信任模型是假的

所以这里要拿一组**已知答案**的样本去量：哪些对是同一篇的转载/洗稿，
哪些对是两个人写的不同攻略，然后看两个候选指标能不能把它们分开。

候选指标：

1. 字符 4-gram Jaccard —— 中文没有词边界，按字符切 n-gram 不需要分词器。
   对「改标点、合并段落、增删少量句子」应当稳健。
2. SimHash 的海明距离 —— 存储便宜（一个 64 位整数），适合做候选筛选。
   但它对短文本与大幅改写是否可靠，要实测。

要看清的是**阈值落在哪里**，以及两种误判各自的代价：

- 把两篇不同攻略判成转载（假阳性）→ 两个独立来源被算成一个，
  高置信结论被降级。这是保守方向的错误。
- 把转载判成不同来源（假阴性）→ 置信度虚高，用户被骗。这是危险方向的错误。

用法：.venv\\Scripts\\python.exe scripts\\probe_m3_grouping.py
"""

from __future__ import annotations

import hashlib
import itertools

from _bootstrap import setup

setup()


# ─── 样本 ────────────────────────────────────────────────────────
#
# 命名规则：id 里的数字表示「同一篇」的变体，字母表示不同作者。
# same=True 的组合必须被判为同一来源，same=False 的必须判为不同。

ORIGINAL = """故宫现在只有午门能进，北门（神武门）只出不进。很多人从地铁天安门东站
出来以后跟着人流走，结果走到天安门城楼那边去了，那边是往天安门广场的，不是故宫入口。
正确的走法是从天安门东站B口出来，穿过天安门城楼的门洞，再往前走到午门。
这一段路要走二十分钟左右，夏天很晒。故宫不卖现场票，全部要提前预约，提前7天的
晚上8点在官方小程序放票，注意是晚上8点不是零点。周末和节假日的票基本是秒没，
建议提前把同行人的身份证号都填好。想拍没人的太和殿，唯一的办法是开门就冲。
故宫早上8点半开门，8点20左右午门外面就排起队了。故宫周边是禁停区，打车只能停到
比较远的地方，地铁1号线天安门东站和天安门西站都能到。"""

# 1a：另一站全文照搬，只改标题和作者
REPOST_VERBATIM = ORIGINAL.replace("晚上8点不是零点", "晚上八点不是零点")

# 1b：洗稿——换说法、合并段落、调整语序，但事实点全在
REPOST_REWRITTEN = """去故宫前一定要搞清楚入口在哪。现在故宫只能从午门进，神武门是只出不进的。
天安门东站出来别跟着人流乱走，那样容易走到天安门城楼方向，那是去广场的。
从天安门东站B口出来，穿过城楼门洞往前就是午门，走路大概二十分钟，夏天特别晒。
门票方面，故宫现场是不卖票的，必须提前预约，官方小程序提前7天晚上8点放票，
不是零点。周末节假日基本秒没，最好先把同行人的身份证号填好。
拍照的话，想拍空无一人的太和殿只能一开门就冲进去。8点半开门，
8点20分午门外就开始排队了。周边禁停，打车停得远，坐1号线到天安门东或天安门西都可以。"""

# 1c：只摘了其中一段的转载，属于部分重合
REPOST_EXCERPT = """【转】故宫入口提示：
故宫现在只有午门能进，北门（神武门）只出不进。正确的走法是从天安门东站B口出来，
穿过天安门城楼的门洞，再往前走到午门。这一段路要走二十分钟左右，夏天很晒。
另外提醒：故宫不卖现场票，全部要提前预约。"""

# A：另一个作者写的故宫，事实点有重合（这是最难的一类）
DIFFERENT_AUTHOR_A = """故宫门票要提前预约，这个大家都知道。我说点别的。
从午门进去以后，先别急着往太和殿走，右手边的武英殿人少，经常有书画展。
珍宝馆和钟表馆各10块钱，值得看。神武门出来正对景山，爬十分钟能拍到故宫全景，
这个角度比在宫里拍好多了。交通建议坐地铁8号线到金鱼胡同，从东华门进，
不过东华门只能出不能进，得绕到午门。"""

# B：另一个作者写西安，与上面毫无关系
DIFFERENT_AUTHOR_B = """西安的陕历博必须提前3天预约，每天早上8点放票，周一闭馆。
兵马俑在临潼，门票120，现场不卖票。从西安北站坐14号线转9号线能到。
回民街商业化严重，不如去洒金桥。城墙傍晚上去，南门租自行车骑一圈13.7公里。"""

SAMPLES: dict[str, str] = {
    "1a_转载照搬": REPOST_VERBATIM,
    "1b_洗稿改写": REPOST_REWRITTEN,
    "1c_转载节选": REPOST_EXCERPT,
    "A_同类异作者": DIFFERENT_AUTHOR_A,
    "B_无关异作者": DIFFERENT_AUTHOR_B,
}

# 期望：同一篇的变体两两同组
SAME_PAIRS = {("1a_转载照搬", "1b_洗稿改写"), ("1a_转载照搬", "1c_转载节选"), ("1b_洗稿改写", "1c_转载节选")}


# ─── 指标 1：字符 n-gram Jaccard ─────────────────────────────────


def shingles(text: str, n: int) -> set[str]:
    stripped = "".join(c for c in text if not c.isspace())
    if len(stripped) < n:
        return {stripped} if stripped else set()
    return {stripped[i : i + n] for i in range(len(stripped) - n + 1)}


def jaccard(a: str, b: str, n: int = 4) -> float:
    sa, sb = shingles(a, n), shingles(b, n)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def containment_text(a: str, b: str, n: int = 4) -> float:
    """a 有多大比例被 b 覆盖。用于处理「节选转载」这种不对称情况。"""
    sa, sb = shingles(a, n), shingles(b, n)
    if not sa:
        return 0.0
    return len(sa & sb) / len(sa)


def lcs_coverage(a: str, b: str, min_block: int = 12) -> float:
    """a 中有多大比例的字符落在「与 b 共有的长连续片段」里。

    这是对 n-gram 的改进版：n-gram 只认一字不差的短片段，而真实洗稿会把
    一个句子里的个别字换掉、把两句合成一句。这里找出所有长度 ≥ min_block 的
    公共子串，只看短的那一篇被覆盖了多少，对局部改写比 n-gram 宽容得多。
    """
    from difflib import SequenceMatcher

    squashed_a = "".join(c for c in a if not c.isspace())
    squashed_b = "".join(c for c in b if not c.isspace())
    if len(squashed_a) > len(squashed_b):
        squashed_a, squashed_b = squashed_b, squashed_a
    if not squashed_a:
        return 0.0

    matcher = SequenceMatcher(None, squashed_a, squashed_b, autojunk=False)
    covered = sum(block.size for block in matcher.get_matching_blocks() if block.size >= min_block)
    return covered / len(squashed_a)


# ─── 指标 2：SimHash ────────────────────────────────────────────


def simhash(text: str, n: int = 4, bits: int = 64) -> int:
    weights = [0] * bits
    for gram in shingles(text, n):
        h = int.from_bytes(hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest(), "big")
        for i in range(bits):
            weights[i] += 1 if (h >> i) & 1 else -1
    value = 0
    for i in range(bits):
        if weights[i] > 0:
            value |= 1 << i
    return value


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def main() -> int:
    print("M3 独立来源归组探测")
    print(f"  样本 {len(SAMPLES)} 篇，需要分出的「同篇」组合 {len(SAME_PAIRS)} 对")
    print()
    print("  正文长度：" + "，".join(f"{k} {len(v)}字" for k, v in SAMPLES.items()))
    print()

    hashes = {k: simhash(v) for k, v in SAMPLES.items()}

    header = (f"  {'组合':<26}{'同篇?':<7}{'Jaccard(2)':>12}{'Jaccard(3)':>12}"
              f"{'Jaccard(4)':>12}{'LCS覆盖':>10}{'SimHash':>9}")
    print(header)
    print("  " + "─" * (len(header) - 2))

    rows = []
    for a, b in itertools.combinations(SAMPLES, 2):
        expected = (a, b) in SAME_PAIRS
        metrics = {
            "j2": jaccard(SAMPLES[a], SAMPLES[b], 2),
            "j3": jaccard(SAMPLES[a], SAMPLES[b], 3),
            "j4": jaccard(SAMPLES[a], SAMPLES[b], 4),
            "lcs": lcs_coverage(SAMPLES[a], SAMPLES[b]),
            "ham": hamming(hashes[a], hashes[b]),
        }
        rows.append((a, b, expected, metrics))
        print(f"  {a + ' ↔ ' + b:<26}{'是' if expected else '否':<7}"
              f"{metrics['j2']:>12.3f}{metrics['j3']:>12.3f}{metrics['j4']:>12.3f}"
              f"{metrics['lcs']:>10.3f}{metrics['ham']:>9d}")

    print()
    same = [r for r in rows if r[2]]
    diff = [r for r in rows if not r[2]]
    print(f"  同篇组合 {len(same)} 对，异篇组合 {len(diff)} 对")
    for key, label in (("j2", "Jaccard(2)"), ("j3", "Jaccard(3)"),
                       ("j4", "Jaccard(4)"), ("lcs", "LCS覆盖")):
        lo, hi = min(r[3][key] for r in same), max(r[3][key] for r in same)
        dlo, dhi = min(r[3][key] for r in diff), max(r[3][key] for r in diff)
        gap = lo / dhi if dhi else float("inf")
        print(f"  {label:<12} 同篇 [{lo:.3f}, {hi:.3f}]  异篇 [{dlo:.3f}, {dhi:.3f}]  "
              f"分离度 {gap:.1f}×")
    lo, hi = min(r[3]["ham"] for r in same), max(r[3]["ham"] for r in same)
    dlo, dhi = min(r[3]["ham"] for r in diff), max(r[3]["ham"] for r in diff)
    print(f"  {'SimHash':<12} 同篇 [{lo}, {hi}]  异篇 [{dlo}, {dhi}]  "
          f"{'区间重叠，无判别力' if lo <= dhi else '可分离'}")

    # 用几个阈值检验，看哪个能同时把好两关
    print()
    print("  阈值扫描（判为同篇的依据）：")
    print(f"  {'策略':<34}{'同篇全中':<12}{'异篇误判':<12}")
    print("  " + "─" * 56)
    for key in ("j2", "j3", "j4", "lcs"):
        for t in (0.20, 0.30, 0.40, 0.50, 0.60):
            hit = sum(1 for r in same if r[3][key] >= t)
            bad = sum(1 for r in diff if r[3][key] >= t)
            print(f"  {key + ' ≥ ' + str(t):<34}{f'{hit}/{len(same)}':<12}{f'{bad}/{len(diff)}':<12}")
    for t in (3, 6, 10, 14):
        hit = sum(1 for r in same if r[3]["ham"] <= t)
        bad = sum(1 for r in diff if r[3]["ham"] <= t)
        print(f"  {'ham ≤ ' + str(t):<34}{f'{hit}/{len(same)}':<12}{f'{bad}/{len(diff)}':<12}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
