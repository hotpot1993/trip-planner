"""提纯适配器的测试。

全部离线：模型客户端是假的，缓存在内存 SQLite 里。
真实调用的行为在 `scripts/verify_extract.py` 里验证（要花 token，不进单测）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from lushu.adapters.extract import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    ExtractionError,
    ExtractionOut,
    build_prompt,
    cache_key,
    extract,
)
from lushu.domain.knowledge import Facet, Polarity, SubjectType
from lushu.store import connect, initialize, transaction

BODY = "故宫现在只有午门能进，北门（神武门）只出不进。提前7天晚上8点放票。"


@dataclass
class FakeLlm:
    """假的模型客户端。记录被调了几次、收过什么提示词。"""

    payload: dict
    calls: list[list[dict]] = field(default_factory=list)
    error: Exception | None = None

    def invoke(self, messages: list[dict]) -> Any:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return ExtractionOut.model_validate(self.payload)


def ok_payload(*claims: dict) -> dict:
    return {"claims": list(claims)}


def a_claim(**overrides: object) -> dict:
    base = {
        "subject_name": "故宫",
        "subject_type": "poi",
        "polarity": "avoid",
        "facet": "entrance",
        "text": "只有午门能进",
        "quote": "故宫现在只有午门能进",
    }
    base.update(overrides)
    return base


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    initialize(path)
    return path


class TestBuildPrompt:
    def test_title_is_included(self) -> None:
        prompt = build_prompt("北京三日", BODY)

        assert "北京三日" in prompt
        assert BODY in prompt

    def test_missing_title_is_marked_not_omitted(self) -> None:
        """标题缺了要说「无标题」，不能悄悄省掉——那会让模型以为正文就是全部。"""
        assert "（无标题）" in build_prompt(None, BODY)
        assert "（无标题）" in build_prompt("   ", BODY)


class TestSystemPrompt:
    def test_requires_verbatim_quotes(self) -> None:
        """这条指令是「提纯不产生新事实」的执行前提，实测模型确实照做了。"""
        assert "连续子串" in SYSTEM_PROMPT
        assert "一个字都不能改" in SYSTEM_PROMPT

    def test_tells_the_model_one_quote_can_support_several_claims(self) -> None:
        """实测：一条引文产出了两条结论（「傍晚上」与「13.7 公里」）。"""
        assert "一条 quote 可以支撑多条" in SYSTEM_PROMPT

    def test_keeps_the_author_wording_for_the_subject(self) -> None:
        """主体名要用原文叫法，俗称的对齐靠高德，不靠改写。"""
        assert "陕历博" in SYSTEM_PROMPT


class TestCacheKey:
    def test_stable_for_same_input(self) -> None:
        assert cache_key(title="t", body=BODY, model="m") == cache_key(
            title="t", body=BODY, model="m"
        )

    def test_changes_with_body(self) -> None:
        assert cache_key(title="t", body=BODY, model="m") != cache_key(
            title="t", body=BODY + "多一句", model="m"
        )

    def test_changes_with_title(self) -> None:
        """同一篇文章换个标题在不同站点发布，标题里的城市名会影响抽出的主体。"""
        assert cache_key(title="北京三日", body=BODY, model="m") != cache_key(
            title="西安三日", body=BODY, model="m"
        )

    def test_changes_with_model(self) -> None:
        assert cache_key(title="t", body=BODY, model="a") != cache_key(
            title="t", body=BODY, model="b"
        )

    def test_includes_prompt_version(self) -> None:
        """改提示词必须让旧缓存失效，否则会拿到按旧规则抽的结果。"""
        import lushu.adapters.extract as module

        original = module.PROMPT_VERSION
        try:
            before = cache_key(title="t", body=BODY, model="m")
            module.PROMPT_VERSION = "extract-v99"
            after = cache_key(title="t", body=BODY, model="m")
        finally:
            module.PROMPT_VERSION = original

        assert before != after
        assert PROMPT_VERSION == original


class TestExtract:
    def test_returns_domain_objects(self) -> None:
        llm = FakeLlm(ok_payload(a_claim()))

        result = extract(title="北京三日", body=BODY, model="m", llm=llm)

        assert len(result.claims) == 1
        claim = result.claims[0]
        assert claim.subject_name == "故宫"
        assert claim.subject_type is SubjectType.POI
        assert claim.polarity is Polarity.AVOID
        assert claim.facet is Facet.ENTRANCE
        assert claim.quote == "故宫现在只有午门能进"
        assert not result.from_cache

    def test_sends_system_and_user_messages(self) -> None:
        llm = FakeLlm(ok_payload())

        extract(title="标题", body=BODY, model="m", llm=llm)

        roles = [message["role"] for message in llm.calls[0]]
        assert roles == ["system", "user"]
        assert SYSTEM_PROMPT in llm.calls[0][0]["content"]
        assert BODY in llm.calls[0][1]["content"]

    def test_empty_claim_list_is_fine(self) -> None:
        """一篇没有可抽内容的素材是正常结果。"""
        result = extract(title=None, body=BODY, model="m", llm=FakeLlm(ok_payload()))

        assert result.claims == ()

    def test_unknown_enum_drops_only_that_claim(self) -> None:
        """一条写错的 facet 不该让这一篇的其它结论一起作废。"""
        llm = FakeLlm(
            ok_payload(
                a_claim(text="好结论"),
                a_claim(subject_type="galaxy", text="主体类型认不出来"),
                a_claim(polarity="无所谓", text="极性认不出来"),
                a_claim(text="另一条好结论"),
            )
        )

        result = extract(title="t", body=BODY, model="m", llm=llm)

        assert [claim.text for claim in result.claims] == ["好结论", "另一条好结论"]

    def test_unknown_facet_falls_back_to_other(self) -> None:
        """facet 认不出来归到 other，而不是丢掉——结论本身仍然可回查。"""
        llm = FakeLlm(ok_payload(a_claim(facet="停车位", text="归到 other")))

        result = extract(title="t", body=BODY, model="m", llm=llm)

        assert result.claims[0].facet is Facet.OTHER

    def test_claim_without_quote_is_dropped(self) -> None:
        """无引文的候选不得入库（DESIGN 4.2），这一层就要挡住。"""
        llm = FakeLlm(ok_payload(a_claim(quote="", text="没有引文")))

        result = extract(title="t", body=BODY, model="m", llm=llm)

        assert result.claims == ()

    def test_llm_failure_raises_readable_error(self) -> None:
        llm = FakeLlm(ok_payload(), error=RuntimeError("连接被重置"))

        with pytest.raises(ExtractionError, match="连接被重置"):
            extract(title="t", body=BODY, model="m", llm=llm)


class TestExtractCache:
    def test_second_call_uses_cache(self, db) -> None:
        """批量提纯要花钱，重跑是常事，不能每次重付。"""
        with connect(db) as conn:
            first = extract(
                title="t", body=BODY, model="m", llm=FakeLlm(ok_payload(a_claim())), conn=conn
            )
            second = extract(
                title="t", body=BODY, model="m", llm=FakeLlm(ok_payload()), conn=conn
            )

        assert not first.from_cache
        assert second.from_cache
        assert second.claims == first.claims

    def test_cache_is_not_used_when_body_differs(self, db) -> None:
        with connect(db) as conn:
            extract(title="t", body=BODY, model="m", llm=FakeLlm(ok_payload(a_claim())), conn=conn)
            again = extract(
                title="t", body=BODY + "多一句", model="m", llm=FakeLlm(ok_payload()), conn=conn
            )

        assert not again.from_cache
        assert again.claims == ()

    def test_cache_is_per_model(self, db) -> None:
        with connect(db) as conn:
            extract(title="t", body=BODY, model="a", llm=FakeLlm(ok_payload(a_claim())), conn=conn)
            other = extract(title="t", body=BODY, model="b", llm=FakeLlm(ok_payload()), conn=conn)

        assert not other.from_cache

    def test_broken_cache_entry_is_recomputed_not_fatal(self, db) -> None:
        """一条坏缓存不该让整批提纯停住。"""
        key = cache_key(title="t", body=BODY, model="m")
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO llm_cache (cache_key, prompt_version, model, response_json, created_at) "
                "VALUES (?, ?, 'm', '这不是 JSON', '2026-09-12')",
                (key, PROMPT_VERSION),
            )

        with connect(db) as conn:
            result = extract(
                title="t", body=BODY, model="m", llm=FakeLlm(ok_payload(a_claim())), conn=conn
            )

        assert not result.from_cache
        assert len(result.claims) == 1

    def test_cached_payload_round_trips(self, db) -> None:
        """写进缓存的是模型原始输出，读回来要能还原成同样的领域对象。"""
        with connect(db) as conn:
            extract(title="t", body=BODY, model="m", llm=FakeLlm(ok_payload(a_claim())), conn=conn)
            row = conn.execute("SELECT response_json FROM llm_cache").fetchone()
            stored = json.loads(row["response_json"])
            cached = extract(title="t", body=BODY, model="m", llm=FakeLlm({}), conn=conn)

        assert stored["claims"][0]["quote"] == "故宫现在只有午门能进"
        assert cached.claims[0].quote == "故宫现在只有午门能进"

    def test_no_conn_means_no_cache(self) -> None:
        """不给连接时不该报错，只是每次都真调。"""
        llm = FakeLlm(ok_payload(a_claim()))

        result = extract(title="t", body=BODY, model="m", llm=llm)

        assert not result.from_cache
        assert len(llm.calls) == 1
