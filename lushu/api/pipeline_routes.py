"""导入与管线的接口。

工作台原先只有读与处置，导不进来也跑不起来——素材要落库、管线要跑一轮，
这两件事之前只能在命令行做。界面要把「标注 30 篇」变成做得完的事，
这两步就必须在界面里能完成。

管线是长任务：提纯一篇 2.5–4.5 秒、对齐一条提及一次高德请求，
几十篇的批量能跑几分钟。所以照 `plan_routes.py` 的形状做 SSE，
**用 POST + 手工解析而不是 EventSource**（EventSource 只支持 GET，
而且失败会自动重连，会把一次管线跑成两次）。

一件必须说清的事：**这些接口跑的是同步服务，靠 `to_thread` 挪出事件循环**。
不留在线程里自己开事件循环，是因为服务层全是同步的 sqlite3 调用；
硬改成异步只会把「同一个连接不能跨线程」这类约束变成一堆难查的偶发错误。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from lushu.api.errors import describe_error
from lushu.api.plan_routes import format_sse
from lushu.services import pipeline
from lushu.services.ingest import ImportKind, ImportRequest, import_document

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["数据管线"])

# 事件名与 plan_routes 保持一致，前端可以复用同一套解析
EVENT_STAGE = "stage"
EVENT_PROGRESS = "progress"
EVENT_DONE = "done"
EVENT_ERROR = "error"


# ─── 导入 ────────────────────────────────────────────────────


class IngestIn(BaseModel):
    """粘贴一篇素材。

    `body` 必填。**不做「给我一个网址你去抓」**：抓取是另一条路径
    （`ls ingest fetch`），放进请求里会让一个 HTTP 请求变成一次不受控的
    外网抓取——超时、重定向、需要登录的页面都会变成接口的失败模式。
    """

    body: str = Field(min_length=1)
    title: str | None = None
    url: str | None = None
    author: str | None = None
    site: str | None = None


class IngestOut(BaseModel):
    """导入结果。

    `duplicate` 三态是设计里的一层归组（ADR-0008）：新素材、转载（归到
    同一来源组）、完全重复（没有重复入库）。界面要按它给不同的提示——
    转载要说明「置信度按组计数，不按篇」。
    """

    document_id: str
    duplicate: str
    site: str
    chars: int
    duplicate_of: str | None = None
    coverage: float | None = None
    group_id: str | None = None


@router.post("/ingest", response_model=IngestOut, status_code=201)
def ingest(payload: IngestIn) -> IngestOut:
    """粘贴导入一篇素材。"""
    try:
        result = import_document(
            ImportRequest(
                body=payload.body,
                title=payload.title,
                url=payload.url,
                author=payload.author,
                site=payload.site,
                kind=ImportKind.PASTE,
            )
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    return IngestOut(
        document_id=result.document_id,
        duplicate=result.duplicate.value,
        site=result.site,
        chars=result.chars,
        duplicate_of=result.duplicate_of,
        coverage=result.coverage,
        group_id=result.group_id,
    )


# ─── 管线 ────────────────────────────────────────────────────


class PipelineIn(BaseModel):
    """跑一轮管线。

    默认跑完整四步。`steps` 可以只跑其中几步——人工处置完待对齐之后
    往往只想补跑归组与合并，不必再问一次模型、也不再打一次高德。
    """

    steps: list[str] = Field(default_factory=lambda: ["extract", "align", "group", "merge"])
    limit: int = Field(default=pipeline.DEFAULT_BATCH_LIMIT, ge=1, le=500)
    document_ids: list[str] | None = None
    force: bool = False

    def validated(self) -> list[str]:
        order = ["extract", "align", "group", "merge"]
        unknown = [item for item in self.steps if item not in order]
        if unknown:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"没有这几种步骤：{'、'.join(unknown)}。可用的是 {'、'.join(order)}",
            )
        # 固定按依赖顺序执行：归组必须在提纯之后、合并必须在归组之后
        return [item for item in order if item in self.steps]


@router.post("/pipeline/run", summary="跑一轮数据管线（带进度）")
async def run_pipeline(payload: PipelineIn) -> StreamingResponse:
    """跑数据管线，实时推送每个步骤与每篇素材的进度。

    事件序列：

    - `stage`    一步开始，`{"step": "extract", "label": "提纯"}`
    - `progress` 一步之内的一条进度，`{"step": "extract", "label": "...", "index": 3, "total": 12}`
    - `done`     成功，带上四步各自的计数
    - `error`    失败，结构与该错误在普通接口下的响应体一致

    进度事件是**有意的冗余**：`stage` 已经说明了在跑哪一步，
    但提纯 12 篇要一分钟，只报「在提纯」与人无益——他要的是「第 3 篇」。
    """
    steps = payload.validated()
    return StreamingResponse(
        _stream_pipeline(payload, steps),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


_STEP_LABELS = {
    "extract": "提纯",
    "align": "对齐",
    "group": "独立来源归组",
    "merge": "合并同义结论并重算置信度",
}


async def _stream_pipeline(payload: PipelineIn, steps: list[str]) -> AsyncIterator[str]:
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(event: str, data: dict) -> None:
        """从工作线程往事件循环里投递。

        服务层在工作线程里跑，**不能直接 `queue.put_nowait`**——
        asyncio 的队列不是线程安全的。`call_soon_threadsafe` 是那个正确的口子。
        """
        loop.call_soon_threadsafe(queue.put_nowait, (event, data))

    def on_progress(item: pipeline.Progress) -> None:
        emit(
            EVENT_PROGRESS,
            {"step": item.step, "label": item.label, "index": item.index, "total": item.total},
        )

    def work() -> dict:
        result: dict[str, dict] = {}
        for step in steps:
            emit(EVENT_STAGE, {"step": step, "label": _STEP_LABELS[step]})
            if step == "extract":
                report = pipeline.extract_documents(
                    limit=payload.limit,
                    document_ids=payload.document_ids,
                    force=payload.force,
                    on_progress=on_progress,
                )
                result["extract"] = {
                    "documents": report.documents,
                    "cached": report.cached,
                    "accepted": report.accepted,
                    "dropped": report.dropped,
                    # 模型编了引文的篇要能点名，那是「提纯不产生新事实」的体检指标
                    "failed": [{"document_id": doc, "error": err} for doc, err in report.failed],
                }
            elif step == "align":
                aligned = pipeline.align_pending(limit=payload.limit, on_progress=on_progress)
                result["align"] = {
                    "mentions": aligned.mentions,
                    "aligned": aligned.aligned,
                    "collapsed": aligned.collapsed,
                    "pending": aligned.pending,
                    "unresolved_subjects": aligned.unresolved_subjects,
                    "failed": [{"mention": name, "error": err} for name, err in aligned.failed],
                }
            elif step == "group":
                grouped = pipeline.group_by_conclusions()
                result["group"] = {
                    "compared": grouped.compared,
                    "merged": grouped.merged,
                    "groups": grouped.groups,
                }
            else:
                merged = pipeline.merge_claims()
                result["merge"] = {
                    "created": merged.created,
                    "extended": merged.extended,
                    "high_confidence": merged.high_confidence,
                    "single_source": merged.single_source,
                }
        result["stats"] = pipeline.pipeline_stats()  # type: ignore[assignment]
        return result

    async def run() -> None:
        try:
            result = await asyncio.to_thread(work)
            queue.put_nowait((EVENT_DONE, result))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 任何失败都要变成一条可读的事件
            status_code, body = describe_error(exc)
            if status_code >= 500:
                logger.exception("管线失败")
            queue.put_nowait((EVENT_ERROR, body))

    task = asyncio.create_task(run())
    try:
        while True:
            event, data = await queue.get()
            yield format_sse(event, data)
            if event in (EVENT_DONE, EVENT_ERROR):
                break
    finally:
        # 客户端断开时把还在跑的管线一并停掉。线程里的 sqlite 写入没法中断，
        # 但至少不再往下跑后面的步骤——半途停下留下的是完整的一步，
        # 每步各自提交（下一轮可以接着跑）。
        if not task.done():
            task.cancel()
