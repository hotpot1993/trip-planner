"""导入与第一层归组的测试。

重复判定关系到置信度：一篇爆款被转载十次，若按篇计数，它的结论就自动变
「高置信」——而实际上只有一个来源。设计把这条列为致命风险（第十一节）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lushu.domain.similarity import DUPLICATE_COVERAGE
from lushu.services.ingest import (
    DuplicateKind,
    ImportKind,
    ImportRequest,
    detect_site,
    fingerprint_text,
    html_to_text,
    import_document,
    read_source_file,
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
        """长度差太远时不必算相似度，也不该误判。

        「提前预约」这类短句在两篇里都有，但两篇不是同一篇。
        """
        conn = connect(db)
        try:
            import_document(ImportRequest(body=ORIGINAL), conn=conn)
            tiny = import_document(ImportRequest(body="故宫要提前预约，周一闭馆。"), conn=conn)
        finally:
            conn.close()

        assert tiny.duplicate is DuplicateKind.NEW

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


class TestFingerprint:
    def test_whitespace_layout_does_not_change_the_fingerprint(self) -> None:
        """同一段话从两个站点复制下来换行位置不同，指纹必须一致。"""
        assert fingerprint_text("甲 乙\n丙") == fingerprint_text("甲乙丙")
        assert fingerprint_text("甲 乙\n丙") == fingerprint_text("甲\n乙 丙")

    def test_content_difference_does_change_the_fingerprint(self) -> None:
        assert fingerprint_text("甲 乙\n丙") != fingerprint_text("甲乙丁")

    def test_plain_sha256_is_layout_sensitive(self) -> None:
        """对照：直接哈希对空白敏感，所以不能拿它判重。"""
        assert sha256_text("甲 乙\n丙") != sha256_text("甲乙丙")
