"""提纯适配器：调用批量模型，把攻略原文抽成结构化的候选经验。

三条实测决定的行为（docs/M3-PROBE.md 第一节）：

1. **`method="function_calling"` + `strict=True` 可用**，两篇素材抽出 28 条候选，
   耗时 4.2s / 4.5s。不需要退到 JSON 模式手工解析。
2. **引文是原文的精确连续子串**（28/28），连换行、空格、直角引号都原样保留。
   所以提示词里可以硬性要求「一字不改」，校验也就可以很严。
3. **一条引文可以支撑多条结论**（「城墙推荐傍晚上去…全程 13.7 公里」产出了
   「建议傍晚上」与「一圈 13.7 公里」两条），所以不要按引文去重。

领域模型在 `lushu/domain/extraction.py`（纯函数、可离线测），这里只负责
「把提示词发出去、把结构化结果拿回来、把缓存管好」。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from lushu.domain.extraction import ExtractedClaim
from lushu.domain.knowledge import Facet, Polarity, SubjectType

# 提示词版本。改动下面任何一段提示词文字都要把它加一——
# 缓存键里含版本号，所以改了提示词不会误用旧结果。
PROMPT_VERSION = "extract-v1"

DEFAULT_MODEL = "deepseek-v4-flash"

class ExtractionError(RuntimeError):
    """提纯失败。"""


# ─── 模型输出结构 ────────────────────────────────────────────────
#
# 字段刻意从紧：subject_type / polarity / facet 都做成枚举，
# 让模型只能在既定分类里选，避免它自由发挥出一堆同义标签。


class _ClaimOut(BaseModel):
    """一条候选结论。字段说明就是给模型看的指令。"""

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


class _ExtractionOut(BaseModel):
    claims: list[_ClaimOut] = Field(description="抽出的候选结论，没有就给空列表")


SYSTEM_PROMPT = """你在为一份旅行攻略做资料整理。

你会读到一篇网友写的游记或攻略，请把里面**作者亲身经历或明确断言**的经验
抽成一条条候选结论。

规则：

1. 每条结论必须给出支持它的原文片段，放在 quote 字段里。**quote 必须是原文的
   连续子串，一个字都不能改**——不要补标点，不要改错别字，不要合并两段，
   不要调整语序。原文里的换行、空格、引号都照抄。
2. 只抽「会影响别人行程决定」的信息。纯叙述、心情、天气、个人偏好不必抽。
3. 硬事实（门票多少钱、几点开门、哪个站换乘）如果作者写得很明确，可以抽；
   但不要把作者的猜测当成事实。
4. text 是你归一化之后的一句话结论，用陈述句，不要照抄 quote。
5. 同一件事只抽一条，不要重复。
6. subject_name 用原文里的叫法（可以是「陕历博」这样的简称），不要自己改成官方全称。
7. 一条 quote 可以支撑多条不同的结论，这时请在每条里都写上同一段 quote。
"""


def build_prompt(title: str | None, body: str) -> str:
    """拼用户消息。标题单独给，因为结论常常靠标题定城市与主题。"""
    heading = title.strip() if title and title.strip() else "（无标题）"
    return f"原文标题：{heading}\n\n原文正文：\n{body}"


@dataclass(frozen=True)
class RawExtraction:
    """模型这一次返回的东西。"""

    claims: tuple[ExtractedClaim, ...]
    model: str
    prompt_version: str
    duration_ms: int
    from_cache: bool = False
    raw_json: str | None = None


def build_llm(schema: type[BaseModel], *, model: str | None = None) -> Any:
    """建一个绑定到给定 schema 的模型客户端。

    provider 与 key 的读法沿用引擎那一套（`DEEPSEEK_*`），
    但**不复用引擎的工厂函数**：那是 third_party，只有 `lushu/engine` 能碰
    （ADR-0005 与架构边界测试）。这里直接用 langchain-openai。
    """
    try:
        import httpx
        from langchain_openai import ChatOpenAI
    except ModuleNotFoundError as exc:  # pragma: no cover - 依赖缺失时的兜底
        raise ExtractionError("缺少 httpx 或 langchain-openai，请先安装依赖") from exc

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ExtractionError("缺少 DEEPSEEK_API_KEY，无法提纯。请在 .env.local 中配置")

    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
    chosen = model or os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    # 关掉思考模式：提纯是「照着规则搬运」，不需要推理链，
    # 开着它会让单篇耗时与费用都上去。
    llm = ChatOpenAI(
        model=chosen,
        api_key=api_key,
        base_url=base_url,
        temperature=0,
        http_client=httpx.Client(trust_env=True),
        http_async_client=httpx.AsyncClient(trust_env=True),
        extra_body={"thinking": {"type": "disabled"}},
    )
    return llm.with_structured_output(schema, method="function_calling", strict=True)


def cache_key(*, title: str | None, body: str, model: str) -> str:
    """缓存键：提示词版本 + 模型 + 输入摘要。

    正文与标题一起进摘要——同一篇文章换个标题在不同站点发布，
    标题里常常带着城市名，抽出来的主体可能不同，不能共用结果。
    """
    digest = hashlib.sha256()
    digest.update(PROMPT_VERSION.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(model.encode("utf-8"))
    digest.update(b"\x00")
    digest.update((title or "").encode("utf-8"))
    digest.update(b"\x00")
    digest.update(body.encode("utf-8"))
    return digest.hexdigest()


def _read_cache(conn: sqlite3.Connection | None, key: str) -> str | None:
    if conn is None:
        return None
    row = conn.execute(
        "SELECT response_json FROM llm_cache WHERE cache_key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    return row["response_json"]


def _write_cache(
    conn: sqlite3.Connection | None,
    key: str,
    *,
    model: str,
    response_json: str,
    now: str,
) -> None:
    if conn is None:
        return
    conn.execute(
        "INSERT OR REPLACE INTO llm_cache "
        "(cache_key, prompt_version, model, response_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (key, PROMPT_VERSION, model, response_json, now),
    )
    conn.commit()


def extract(
    *,
    title: str | None,
    body: str,
    model: str | None = None,
    conn: sqlite3.Connection | None = None,
    llm: Any | None = None,
    now: str | None = None,
) -> RawExtraction:
    """把一篇素材交给模型提纯。

    `conn` 给了就用它做缓存——同一篇素材用同一个提示词重复提纯时直接复用。
    批量提纯要花钱，而重跑是常事（改一处解析、加一个字段），不缓存等于每次重付。
    """
    chosen = model or os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    key = cache_key(title=title, body=body, model=chosen)

    cached = _read_cache(conn, key)
    if cached is not None:
        try:
            payload = json.loads(cached)
            return RawExtraction(
                claims=tuple(_to_claims(payload)),
                model=chosen,
                prompt_version=PROMPT_VERSION,
                duration_ms=0,
                from_cache=True,
                raw_json=cached,
            )
        except (ValueError, TypeError, KeyError):
            # 缓存坏了就当没有，重算一次。不抛异常：
            # 一条坏缓存不该让整批提纯停住。
            pass

    active_llm = llm if llm is not None else build_llm(_ExtractionOut, model=chosen)

    started = time.monotonic()
    try:
        result = active_llm.invoke(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(title, body)},
            ]
        )
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"提纯调用失败：{type(exc).__name__}: {exc}") from exc
    duration_ms = int((time.monotonic() - started) * 1000)

    if not isinstance(result, _ExtractionOut):
        result = _ExtractionOut.model_validate(result)

    payload = result.model_dump()
    raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    _write_cache(
        conn,
        key,
        model=chosen,
        response_json=raw_json,
        now=now or time.strftime("%Y-%m-%dT%H:%M:%S"),
    )

    return RawExtraction(
        claims=tuple(_to_claims(payload)),
        model=chosen,
        prompt_version=PROMPT_VERSION,
        duration_ms=duration_ms,
        from_cache=False,
        raw_json=raw_json,
    )


def _to_claims(payload: dict) -> list[ExtractedClaim]:
    """把模型的输出翻成领域对象。

    模型给出枚举之外的值时**逐条丢弃而不是整批失败**：一条写错的 facet
    不该让这一篇的另外十三条好结论一起作废。丢弃的那条会体现为
    「模型吐出的条数」与「校验通过的条数」之间的差额，在数据工作台上看得见。
    """
    claims: list[ExtractedClaim] = []
    for raw in payload.get("claims") or []:
        if not isinstance(raw, dict):
            continue
        try:
            claims.append(
                ExtractedClaim(
                    subject_name=str(raw.get("subject_name") or "").strip(),
                    subject_type=SubjectType(str(raw.get("subject_type") or "").strip()),
                    polarity=Polarity(str(raw.get("polarity") or "").strip()),
                    facet=_facet_or_other(raw.get("facet")),
                    text=str(raw.get("text") or "").strip(),
                    quote=str(raw.get("quote") or ""),
                )
            )
        except (ValueError, KeyError):
            continue
    return claims


def _facet_or_other(value: object) -> Facet:
    """facet 认不出来时归到 other，而不是丢掉这条结论。

    `other` 是设计里就有的类别（复验周期 180 天），归到这里比丢掉安全：
    结论本身仍然有引文、仍然可回查，只是分类粗一点。
    """
    try:
        return Facet(str(value or "").strip())
    except ValueError:
        return Facet.OTHER
