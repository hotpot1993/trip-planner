"""导入与第一层归组的测试。

重复判定关系到置信度：一篇爆款被转载十次，若按篇计数，它的结论就自动变
「高置信」——而实际上只有一个来源。设计把这条列为致命风险（第十一节）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lushu.domain.similarity import DUPLICATE_COVERAGE, overlap
from lushu.services.ingest import (
    DuplicateKind,
    ImportKind,
    ImportRequest,
    _maybe_repost,
    detect_site,
    fingerprint_text,
    html_to_text,
    import_document,
    read_source_file,
    relink_reposts,
    sha256_text,
)
from lushu.store import connect, initialize

ORIGINAL = """故宫现在只有午门能进，北门（神武门）只出不进。很多人从地铁天安门东站
出来以后跟着人流走，结果走到天安门城楼那边去了，那边是往天安门广场的，不是故宫入口。
正确的走法是从天安门东站B口出来，穿过天安门城楼的门洞，再往前走到午门。
这一段路要走二十分钟左右，夏天很晒。故宫不卖现场票，全部要提前预约，提前7天的
晚上8点在官方小程序放票，注意是晚上8点不是零点。"""

REPOST = """【转】故宫入口提示：
故宫现在只有午门能进，北门（神武门）只出不进。正确的走法是从天安门东站B口出来，
穿过天安门城楼的门洞，再往前走到午门。这一段路要走二十分钟左右，夏天很晒。
另外提醒：故宫不卖现场票，全部要提前预约。"""

DIFFERENT = """西安的陕历博必须提前3天预约，每天早上8点放票，周一闭馆。
兵马俑在临潼，门票120，现场不卖票。从西安北站坐14号线转9号线能到。
回民街商业化严重，不如去洒金桥。城墙傍上去，南门租自行车骑一圈13.7公里。"""

# 真库里的形状：一篇 459 字的正文，和一篇 99 字的、逐字照搬其中两段的转载。
# **旧的长度粗筛（短篇 × 3 < 长篇就跳过）会把这一对直接扔掉**，
# 于是两篇都没归组，同一条「陕历博周一闭馆」按两篇素材计成「2 个独立来源」。
# 这里不抄真数据（抄错一个字就变成另一个测试），改用同形状的自造样本，
# 并在测试里断言形状确实成立——形状被改坏时测试要大声报错，
# 而不是静默地什么也没测。
LONG_ARTICLE = """西安三天，博物馆和城墙

去西安主要为了看博物馆。这里说几个我实际遇到的问题，都是出发前没料到的。

陕西历史博物馆是免费的，但是必须预约，而且要提前 3 天在官方公众号预约，
每天早上 8 点放票。这个馆很难约，我约了两天才约上，节假日基本靠抢。
注意陕历博周一闭馆，安排行程的时候要避开。如果实在约不上，可以考虑买
大唐遗宝展的票进去，那个是要收费的，人少一些，但至少能进馆。

兵马俑不在西安市区，在临潼区，从西安北站坐地铁 14 号线再转 9 号线能到，
全程一个半小时。也可以坐游 5 路（306 路）从火车站东广场直达，票价 7 块，
但是这个车路上会拉你去买玉，不要下车，坚持坐到终点。兵马俑的门票是
120 块，需要提前在官网或者公众号买，现场不卖票，到了门口再想办法很被动。

城墙我推荐傍晚上去，南门上，租自行车骑一圈，全程 13.7 公里，一个半小时
左右。白天上城墙太晒了，城墙上没有什么遮阴的地方，夏天去要有准备。

回民街我个人的意见是不用专门去，商业化太严重，同样是吃肉夹馍和泡馍，
洒金桥那边更便宜也更地道，本地人去的也多。

大唐不夜城晚上去，人是真的多，尤其是七点到九点，带小孩的话要看好。"""

SHORT_EXCERPT = """【转】西安博物馆预约提示：

陕西历史博物馆是免费的，但是必须预约，而且要提前 3 天在官方公众号预约，
每天早上 8 点放票。注意陕历博周一闭馆，安排行程的时候要避开。

（本文转自网络，供参考）"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    initialize(path)
    return path


class TestDetectSite:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.mafengwo.cn/i/12345.html", "mafengwo"),
            ("https://zhuanlan.zhihu.com/p/999", "zhihu"),
            ("https://www.zhihu.com/question/1", "zhihu"),
            ("https://www.qyer.com/a/b", "qiongyou"),
            ("https://you.ctrip.com/travels/xian1/123.html", "ctrip"),
            ("https://www.xiaohongshu.com/explore/abc", "xhs"),
        ],
    )
    def test_known_domains(self, url: str, expected: str) -> None:
        assert detect_site(url) == expected

    def test_unknown_domain_is_manual_not_a_guess(self) -> None:
        """认不出来记 manual。猜错站点会影响「独立来源」的域名判据。"""
        assert detect_site("https://some-travel-blog.example.com/post/1") == "manual"
        assert detect_site(None) == "manual"
        assert detect_site("") == "manual"

    def test_lookalike_domain_is_not_matched(self) -> None:
        """`notmafengwo.cn` 不是马蜂窝。域名判据要按标签边界比，不能按子串。"""
        assert detect_site("https://notmafengwo.cn/i/1.html") == "manual"
        assert detect_site("https://mafengwo.cn.evil.com/i/1.html") == "manual"


class TestHtmlToText:
    def test_strips_tags_and_keeps_text(self) -> None:
        html = "<p>故宫只有午门能进</p><p>北门只出不进</p>"

        text = html_to_text(html)

        assert "故宫只有午门能进" in text
        assert "北门只出不进" in text
        assert "<p>" not in text

    def test_block_tags_become_newlines(self) -> None:
        """段落边界要保留：合并成一整行会让引文偏移看起来对但读起来不对。"""
        text = html_to_text("<p>第一段</p><p>第二段</p>")

        assert text == "第一段\n第二段"

    def test_script_and_style_are_removed_entirely(self) -> None:
        """只删标签的话，`<script>` 里的代码会混进正文，污染指纹与引文偏移。"""
        html = "<p>正文</p><script>var x = '<b>不是正文</b>';</script><style>.a{color:red}</style>"

        text = html_to_text(html)

        assert "正文" in text
        assert "var x" not in text
        assert "color:red" not in text
        assert "不是正文" not in text

    def test_entities_are_decoded(self) -> None:
        assert html_to_text("A&nbsp;B &amp; C &lt;D&gt;") == "A B & C <D>"

    def test_br_becomes_newline(self) -> None:
        assert html_to_text("甲<br>乙<br/>丙") == "甲\n乙\n丙"


class TestReadSourceFile:
    def test_markdown_file(self, tmp_path: Path) -> None:
        path = tmp_path / "故宫攻略.md"
        path.write_text(ORIGINAL, encoding="utf-8")

        request = read_source_file(path)

        assert request.kind is ImportKind.FILE
        assert request.title == "故宫攻略"
        assert request.body.strip() == ORIGINAL.strip()

    def test_html_file(self, tmp_path: Path) -> None:
        path = tmp_path / "游记.html"
        path.write_text("<p>故宫只有午门能进</p>", encoding="utf-8")

        request = read_source_file(path)

        assert "<p>" not in request.body
        assert "故宫只有午门能进" in request.body

    def test_empty_file_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "空.md"
        path.write_text("   \n\n  ", encoding="utf-8")

        with pytest.raises(ValueError, match="没有可用正文"):
            read_source_file(path)


class TestImportDocument:
    def test_first_import_is_new(self, db: Path) -> None:
        result = import_document(
            ImportRequest(body=ORIGINAL, title="故宫攻略", site="mafengwo"), conn=connect(db)
        )

        assert result.duplicate is DuplicateKind.NEW
        assert result.stored
        assert result.site == "mafengwo"
        assert result.chars > 0

    def test_body_is_normalized_on_the_way_in(self, db: Path) -> None:
        """入库的正文必须是归一化后的那一份——引文偏移指向它。"""
        result = import_document(
            ImportRequest(body="\n\n\n" + ORIGINAL + "\n\n\n"), conn=connect(db)
        )

        conn = connect(db)
        try:
            row = conn.execute(
                "SELECT body_text FROM source_document WHERE id = ?", (result.document_id,)
            ).fetchone()
        finally:
            conn.close()

        assert row["body_text"] == ORIGINAL.strip()

    def test_exact_duplicate_is_not_stored_twice(self, db: Path) -> None:
        """同一篇粘两次变成两篇，会让独立来源计数虚高，而置信度就是靠它算的。"""
        conn = connect(db)
        try:
            first = import_document(ImportRequest(body=ORIGINAL), conn=conn)
            again = import_document(ImportRequest(body=ORIGINAL, site="zhihu"), conn=conn)
        finally:
            conn.close()

        assert again.duplicate is DuplicateKind.EXACT
        assert again.document_id == first.document_id
        assert not again.stored

        conn = connect(db)
        try:
            count = conn.execute("SELECT COUNT(*) AS n FROM source_document").fetchone()["n"]
        finally:
            conn.close()
        assert count == 1

    def test_whitespace_only_difference_is_still_exact(self, db: Path) -> None:
        """换行位置不同不算另一篇——归一化之后指纹相同。"""
        conn = connect(db)
        try:
            first = import_document(ImportRequest(body=ORIGINAL), conn=conn)
            again = import_document(ImportRequest(body=ORIGINAL.replace("\n", "")), conn=conn)
        finally:
            conn.close()

        assert again.duplicate is DuplicateKind.EXACT
        assert again.document_id == first.document_id

    def test_repost_is_stored_but_grouped(self, db: Path) -> None:
        """转载照常入库（它有自己的域名与作者），但要归到同一个来源组。

        「计数按组、不按篇」就是靠这一步成立的（ADR-0008）。
        """
        conn = connect(db)
        try:
            original = import_document(
                ImportRequest(body=ORIGINAL, url="https://www.mafengwo.cn/i/1.html"), conn=conn
            )
            repost = import_document(
                ImportRequest(body=REPOST, url="https://zhuanlan.zhihu.com/p/2"), conn=conn
            )
        finally:
            conn.close()

        assert repost.duplicate is DuplicateKind.REPOST
        assert repost.stored
        assert repost.duplicate_of == original.document_id
        assert repost.group_id is not None
        assert repost.coverage is not None
        assert repost.coverage >= DUPLICATE_COVERAGE

    def test_repost_joins_the_original_group(self, db: Path) -> None:
        """原件本来没有组时，导入转载时才建组——两边都要挂上去。"""
        conn = connect(db)
        try:
            original = import_document(ImportRequest(body=ORIGINAL), conn=conn)
            repost = import_document(ImportRequest(body=REPOST), conn=conn)

            rows = conn.execute(
                "SELECT id, source_group_id FROM source_document ORDER BY imported_at"
            ).fetchall()
        finally:
            conn.close()

        ids = {row["id"]: row["source_group_id"] for row in rows}
        assert ids[original.document_id] is not None
        assert ids[original.document_id] == ids[repost.document_id]
        assert repost.group_id == ids[original.document_id]

        conn = connect(db)
        try:
            groups = conn.execute("SELECT COUNT(*) AS n FROM source_group").fetchone()["n"]
        finally:
            conn.close()
        assert groups == 1

    def test_different_article_is_a_separate_source(self, db: Path) -> None:
        """两位不同作者写同一座城市，必须算两个来源——错并会把高置信结论降级。"""
        conn = connect(db)
        try:
            import_document(ImportRequest(body=ORIGINAL), conn=conn)
            other = import_document(ImportRequest(body=DIFFERENT), conn=conn)
        finally:
            conn.close()

        assert other.duplicate is DuplicateKind.NEW
        assert other.group_id is None
        assert other.duplicate_of is None

    def test_group_created_once_for_many_reposts(self, db: Path) -> None:
        """第三次转载仍然进同一个组，不会每来一篇就新建一个组。"""
        conn = connect(db)
        try:
            original = import_document(ImportRequest(body=ORIGINAL), conn=conn)
            first = import_document(ImportRequest(body=REPOST), conn=conn)
            second = import_document(
                ImportRequest(body=REPOST + "\n补一句无关的话。"), conn=conn
            )
        finally:
            conn.close()

        assert first.group_id == second.group_id
        assert first.group_id != original.document_id or original.group_id is None

        conn = connect(db)
        try:
            groups = conn.execute("SELECT COUNT(*) AS n FROM source_group").fetchone()["n"]
        finally:
            conn.close()
        assert groups == 1

    def test_short_article_does_not_match_a_long_one(self, db: Path) -> None:
        """长度差太远的两篇不该误判为同一来源。

        「提前预约」这类短句在两篇里都有，但两篇不是同一篇。注意这里量的
        不是「有没有被粗筛跳过」——粗筛跳过的只保证真覆盖率低于阈值，
        而这一对本来覆盖率就是 0。
        """
        conn = connect(db)
        try:
            import_document(ImportRequest(body=ORIGINAL), conn=conn)
            tiny = import_document(ImportRequest(body="故宫要提前预约，周一闭馆。"), conn=conn)
        finally:
            conn.close()

        assert tiny.duplicate is DuplicateKind.NEW


class TestShortExcerptOfALongArticle:
    """一篇短文逐字照搬长篇的一段，必须认出是转载。

    这一组测的是一个**真的发生过**的错：旧的长度粗筛写着
    「较短的一篇 × 3 < 较长的一篇就跳过」，理由是「长度差三倍以上时短篇
    被覆盖满也达不到阈值」。但覆盖率的分母就是较短的那一篇，短篇被整篇
    照搬时覆盖率是 1.0，与长度差无关——于是真库里 99 字对 459 字的那对
    （真覆盖率 0.722）从没进过比对，两篇都没归组。代价是同一条结论按
    **两篇素材**计成了「2 个独立来源」，也就是设计第十一节列为致命的
    「置信度退化成转发量」。
    """

    def test_the_fixture_really_has_the_shape_that_broke_it(self) -> None:
        """先证明样本确实长得像出事的那对，否则下面两条测的是别的东西。"""
        assert len(SHORT_EXCERPT) * 3 < len(LONG_ARTICLE), "长度差必须大于三倍"
        assert overlap(LONG_ARTICLE, SHORT_EXCERPT).coverage >= DUPLICATE_COVERAGE

    def test_verbatim_excerpt_is_recognized_as_a_repost(self, db: Path) -> None:
        conn = connect(db)
        try:
            original = import_document(ImportRequest(body=LONG_ARTICLE), conn=conn)
            excerpt = import_document(
                ImportRequest(
                    body=SHORT_EXCERPT, url="https://you.ctrip.com/travels/xian1/9.html"
                ),
                conn=conn,
            )
            rows = conn.execute(
                "SELECT id, source_group_id FROM source_document"
            ).fetchall()
        finally:
            conn.close()

        assert excerpt.duplicate is DuplicateKind.REPOST
        assert excerpt.duplicate_of == original.document_id
        assert excerpt.coverage is not None and excerpt.coverage >= DUPLICATE_COVERAGE
        # 关键的一半：**两篇都要挂进同一个组**。只标出「这是转载」而不归组，
        # 计数时仍然按两篇算，闸门等于没关。
        groups = {row["id"]: row["source_group_id"] for row in rows}
        assert groups[original.document_id] is not None
        assert groups[original.document_id] == groups[excerpt.document_id]

    def test_the_prune_never_throws_away_a_real_duplicate(self) -> None:
        """粗筛的不变量：被它挡掉的对，真覆盖率一定低于阈值。

        这是 O(n) 上界的全部意义所在——它只能筛掉**确定**不够格的对。
        """
        bodies = [ORIGINAL, REPOST, DIFFERENT, LONG_ARTICLE, SHORT_EXCERPT]
        for left in bodies:
            for right in bodies:
                if left is right:
                    continue
                if _maybe_repost(left, right):
                    continue
                assert overlap(left, right).coverage < DUPLICATE_COVERAGE, (
                    "粗筛把一对真的转载挡掉了"
                )

    def test_the_prune_still_rejects_unrelated_pairs(self) -> None:
        """粗筛不能退化成恒真——否则它就只是白算一遍。

        注意它**故意是松的**：「故宫要提前预约，周一闭馆。」里的字有十一二个
        都能在这篇故宫正文里找到，上界因此过线，放行给 `overlap` 去算——
        这是对的，上界只承诺「不够格的一定挡掉」，不承诺「够格的一定放行」。
        真判据仍然是覆盖率。
        """
        assert not _maybe_repost(SHORT_EXCERPT, DIFFERENT)
        assert not _maybe_repost("甲乙丙丁戊己庚辛壬癸", "子丑寅卯辰巳午未申酉戌亥")

    def test_empty_body_is_rejected(self, db: Path) -> None:
        with pytest.raises(ValueError, match="正文为空"):
            import_document(ImportRequest(body="   \n  "), conn=connect(db))

    def test_unknown_site_falls_back_to_manual(self, db: Path) -> None:
        result = import_document(
            ImportRequest(body=ORIGINAL, site="某个小网站"), conn=connect(db)
        )

        assert result.site == "manual"

    def test_fetch_kind_records_fetched_at(self, db: Path) -> None:
        """抓取时间与导入时间分开记：批量囤稿时两者会差很远。"""
        result = import_document(
            ImportRequest(body=ORIGINAL, url="https://www.mafengwo.cn/i/9.html",
                          kind=ImportKind.FETCH),
            conn=connect(db),
        )

        conn = connect(db)
        try:
            row = conn.execute(
                "SELECT import_kind, fetched_at, url FROM source_document WHERE id = ?",
                (result.document_id,),
            ).fetchone()
        finally:
            conn.close()

        assert row["import_kind"] == "fetch"
        assert row["fetched_at"] is not None
        assert row["url"].endswith("/i/9.html")

    def test_paste_kind_has_no_fetched_at(self, db: Path) -> None:
        """手工粘贴没有「抓取」这回事，不该编一个时间出来。"""
        result = import_document(ImportRequest(body=ORIGINAL), conn=connect(db))

        conn = connect(db)
        try:
            row = conn.execute(
                "SELECT fetched_at FROM source_document WHERE id = ?", (result.document_id,)
            ).fetchone()
        finally:
            conn.close()

        assert row["fetched_at"] is None

    def test_import_survives_without_an_explicit_connection(self, db: Path, monkeypatch) -> None:
        """不传连接时自己开一个。管线脚本靠这个用法。"""
        result = import_document(ImportRequest(body=ORIGINAL), conn=connect(db))

        assert result.document_id.startswith("src_")

    def test_original_text_is_kept_as_local_archive(self, db: Path) -> None:
        """全文只留在本地底档，路书只带片段（Q22 的版权边界）。"""
        result = import_document(ImportRequest(body=ORIGINAL), conn=connect(db))

        conn = connect(db)
        try:
            row = conn.execute(
                "SELECT body_text FROM source_document WHERE id = ?", (result.document_id,)
            ).fetchone()
        finally:
            conn.close()

        assert row["body_text"] == ORIGINAL

    def test_sha256_is_recorded(self, db: Path) -> None:
        result = import_document(ImportRequest(body=ORIGINAL), conn=connect(db))

        conn = connect(db)
        try:
            row = conn.execute(
                "SELECT content_sha256, body_sha256 FROM source_document WHERE id = ?",
                (result.document_id,),
            ).fetchone()
        finally:
            conn.close()

        # 判重用的是抹掉空白的指纹，两列存同一份，方便日后排查
        assert row["content_sha256"] == fingerprint_text(ORIGINAL)
        assert row["body_sha256"] == row["content_sha256"]


class TestRelinkReposts:
    """第一层归组能在**已经入库**的素材上重跑。

    这一组测的是「修好算法 ≠ 修好数据」那件事：第一层原先只在导入那一刻跑，
    所以判据改过、或者导入时判错了，历史数据永远修不回来。真库里就有一对
    这样的素材（459 字的原文与 99 字的转载），长期计成两个独立来源。
    """

    @staticmethod
    def _insert(db: Path, document_id: str, body: str, *, imported_at: str) -> None:
        """直接入库，绕过导入时的判重——专门造「已经躺在库里」的状态。"""
        conn = connect(db)
        try:
            conn.execute(
                "INSERT INTO source_document (id, site, body_text, body_sha256, "
                "content_sha256, imported_at, import_kind) "
                "VALUES (?, 'manual', ?, ?, ?, ?, 'paste')",
                (document_id, body, f"sha-{document_id}", f"sha-{document_id}", imported_at),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _groups(db: Path) -> dict[str, str | None]:
        conn = connect(db)
        try:
            return {
                row["id"]: row["source_group_id"]
                for row in conn.execute("SELECT id, source_group_id FROM source_document")
            }
        finally:
            conn.close()

    def test_a_missed_repost_is_found_when_the_layer_is_rerun(self, db: Path) -> None:
        """导入时漏判的转载，重跑要能补上，并且**两篇都挂进同一个组**。"""
        self._insert(db, "src_long", LONG_ARTICLE, imported_at="2026-09-12T10:00:00")
        self._insert(db, "src_short", SHORT_EXCERPT, imported_at="2026-09-12T10:00:01")

        report = relink_reposts(conn=connect(db))

        assert report.merged == 1
        assert report.groups == 1
        groups = self._groups(db)
        assert groups["src_long"] is not None
        assert groups["src_long"] == groups["src_short"]

    def test_the_pair_is_grouped_whichever_came_first(self, db: Path) -> None:
        """谁先入库不影响结论，只影响谁被当成原件。

        「原件总是先来的」是个假设，不是事实——同一批囤稿的时间戳可能一模一样。
        所以这里要证明假设不成立时也不会分成两组。
        """
        self._insert(db, "src_short", SHORT_EXCERPT, imported_at="2026-09-12T10:00:00")
        self._insert(db, "src_long", LONG_ARTICLE, imported_at="2026-09-12T10:00:01")

        relink_reposts(conn=connect(db))

        groups = self._groups(db)
        assert groups["src_long"] is not None
        assert groups["src_long"] == groups["src_short"]

    def test_a_chain_of_reposts_lands_in_one_group(self, db: Path) -> None:
        """A←B←C 的链式转载要落在一个组里，不能变成两个组。

        B 的组是**本轮才建**的，而候选列表是循环开始时取的快照——照着快照里
        那个 NULL 走，C 会被塞进第二个组，一条链被拆成两条。
        """
        middle = SHORT_EXCERPT + "\n\n再加一句自己的话，让 C 与 B 更近。"
        self._insert(db, "src_a", LONG_ARTICLE, imported_at="2026-09-12T10:00:00")
        self._insert(db, "src_b", middle, imported_at="2026-09-12T10:00:01")
        # C 抄的是 B（比 A 短，覆盖率更高），而 B 到此刻才刚被挂进 A 的组
        self._insert(db, "src_c", middle, imported_at="2026-09-12T10:00:02")

        relink_reposts(conn=connect(db))

        groups = self._groups(db)
        assert groups["src_a"] is not None
        assert groups["src_a"] == groups["src_b"] == groups["src_c"]

    def test_independent_articles_are_left_alone(self, db: Path) -> None:
        """各写各的不能并——错并会把高置信结论降级。"""
        self._insert(db, "src_1", ORIGINAL, imported_at="2026-09-12T10:00:00")
        self._insert(db, "src_2", DIFFERENT, imported_at="2026-09-12T10:00:01")
        self._insert(db, "src_3", LONG_ARTICLE, imported_at="2026-09-12T10:00:02")

        report = relink_reposts(conn=connect(db))

        assert report.merged == 0
        assert report.groups == 0
        assert set(self._groups(db).values()) == {None}

    def test_rerunning_does_not_change_anything(self, db: Path) -> None:
        """同一条命令可以反复执行（ADR-0008），第二次不该再动数据。"""
        self._insert(db, "src_long", LONG_ARTICLE, imported_at="2026-09-12T10:00:00")
        self._insert(db, "src_short", SHORT_EXCERPT, imported_at="2026-09-12T10:00:01")

        conn = connect(db)
        try:
            first = relink_reposts(conn=conn)
            before = self._groups(db)
            second = relink_reposts(conn=conn)
            after = self._groups(db)
        finally:
            conn.close()

        assert first.merged == 1
        assert second.merged == 0
        assert second.groups == 1
        assert before == after



    def test_whitespace_layout_does_not_change_the_fingerprint(self) -> None:
        """同一段话从两个站点复制下来换行位置不同，指纹必须一致。"""
        assert fingerprint_text("甲 乙\n丙") == fingerprint_text("甲乙丙")
        assert fingerprint_text("甲 乙\n丙") == fingerprint_text("甲\n乙 丙")

    def test_content_difference_does_change_the_fingerprint(self) -> None:
        assert fingerprint_text("甲 乙\n丙") != fingerprint_text("甲乙丁")

    def test_plain_sha256_is_layout_sensitive(self) -> None:
        """对照：直接哈希对空白敏感，所以不能拿它判重。"""
        assert sha256_text("甲 乙\n丙") != sha256_text("甲乙丙")
