"""命令行在 GBK 控制台下不能因为一个装饰符号崩掉。

这条是核对坐标检查时撞出来的：把一处坐标写反之后跑 `ls export --check`，
期望看到 `✗ [coord_impossible] ...`，实际看到的是一串 traceback——
`UnicodeEncodeError: 'gbk' codec can't encode character '\u2717'`。

根因是 Windows 控制台默认 GBK（cp936），而 `✗`（U+2717）不在 GBK 里。
中文本身没问题——GBK 就是中文码表——所以出事的恰恰只有那几个装饰符号：
`✗`、`✅`、`⚠`。而它们全都在**报错与进度**这两条路径上：也就是说，
最该看见消息的时候，消息印不出来。

同一个坑上排着：契约校验（`roadbook.Problem`）、预约规则体检
（`booking.Finding`）、规则列表、金标准进度、评测结果。

装饰性符号可以退化，消息不行。所以修的是 `errors` 而不是 `encoding`。
"""

from __future__ import annotations

import io

import pytest

from lushu.cli import _tolerate_unprintable
from lushu.domain.booking import Finding
from lushu.domain.booking import Severity as BookingSeverity
from lushu.domain.roadbook import Problem
from lushu.domain.roadbook import Severity as RoadbookSeverity

# 两个领域层各自会打 `✗` 的类型。一起测：坑是同一个，出处在两处。
ERROR_MARKS = [
    Problem(
        severity=RoadbookSeverity.ERROR,
        code="coord_impossible",
        where="2026-11-16 陕西历史博物馆",
        message="坐标 108.955044,34.224199 不在中国范围内，多半是经纬度写反了",
    ),
    Finding(
        severity=BookingSeverity.ERROR,
        code="no_channel",
        poi_id="B001D03PEX",
        poi_name="陕西历史博物馆",
        message="知道要预约却没有渠道，用户无处可去",
    ),
]


def _gbk_stream() -> tuple[io.TextIOWrapper, io.BytesIO]:
    """一个真实的 GBK 控制台：编不出来的字符直接抛，不替换。"""
    raw = io.BytesIO()
    return io.TextIOWrapper(raw, encoding="gbk", errors="strict"), raw


def _dump(stream: io.TextIOWrapper, raw: io.BytesIO) -> str:
    stream.flush()
    return raw.getvalue().decode("gbk")


@pytest.mark.parametrize("problem", ERROR_MARKS, ids=lambda item: item.code)
class TestGbkConsole:
    def test_without_the_fix_it_raises(self, problem) -> None:
        """先证明这个坑是真的——不然那条修法就成了没病吃药。"""
        stream, _ = _gbk_stream()
        with pytest.raises(UnicodeEncodeError):
            print(problem, file=stream)

    def test_the_message_survives_the_downgrade(self, problem) -> None:
        """符号可以变成一个问号，**消息必须一个字不少**。"""
        stream, raw = _gbk_stream()
        _tolerate_unprintable(stream)

        print(problem, file=stream)
        written = _dump(stream, raw)

        assert f"[{problem.code}]" in written  # 代号是 ASCII，必须完整
        assert problem.message in written

    def test_only_the_glyph_degrades(self, problem) -> None:
        """放宽的是 errors 不是 encoding：中文照常是中文，退化只发生在符号上。"""
        stream, raw = _gbk_stream()
        _tolerate_unprintable(stream)

        print(problem, file=stream)
        written = _dump(stream, raw)

        assert "陕西历史博物馆" in written
        assert written.startswith("? [")  # 只有那一个符号变成了问号


class TestOtherStreams:
    def test_a_stream_without_reconfigure_is_left_alone(self) -> None:
        """测试捕获、StringIO、不可重配的对象——都可能有、都不该因此崩。"""
        _tolerate_unprintable(io.StringIO())
        _tolerate_unprintable(object())

    def test_a_closed_stream_does_not_blow_up(self) -> None:
        stream = io.StringIO()
        stream.close()

        _tolerate_unprintable(stream)


class TestTheHazardIsPinned:
    def test_the_error_marker_cannot_be_encoded_in_gbk(self) -> None:
        """坑的来源就是这个符号。

        哪天有人把 `✗` 换成 ASCII，这条会红——那时应当连
        `_tolerate_unprintable` 一起重新审视（`✅`、`⚠` 还在别处），
        而不是默默把这条删掉。
        """
        with pytest.raises(UnicodeEncodeError):
            "✗".encode("gbk")

    def test_warnings_are_plain_ascii(self) -> None:
        """`!` 编得进 GBK，所以警告这一路从来不会崩——问题只在错误那一路。"""
        warn = Problem(
            severity=RoadbookSeverity.WARN,
            code="leg_missing",
            where="2026-11-12",
            message="这两处之间没有路段说明",
        )

        stream, raw = _gbk_stream()
        print(warn, file=stream)

        assert _dump(stream, raw).startswith("! [")
