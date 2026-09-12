"""规划流式接口。

规划要跑一到几分钟，中间走过十来个节点。同步接口让人对着空屏等，这里把每个
节点的开始事件实时推给前端。

**用 POST + 手工解析 SSE，而不是 EventSource**：EventSource 只支持 GET，而这个
请求会创建一份行程，用 GET 语义上不对（还会被浏览器与中间层缓存）。代价是前端
要多写二十行解析，换来的是方法正确、且不会有 EventSource 那种自动重连——
重连在这里是有害的，它会把一次生成变成两次。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from lushu.api.errors import describe_error
from lushu.api.trip_routes import PlanIn
from lushu.engine import PlanStage
from lushu.services import trip_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["规划"])

# 事件名与前端约定一致，改动要同时改 web/src/lib/api.ts
EVENT_STAGE = "stage"
EVENT_DONE = "done"
EVENT_ERROR = "error"


def format_sse(event: str, data: dict) -> str:
    """SSE 帧。中文不转义——它是给人看的进度文案。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/plan/stream", summary="生成行程（带进度）")
async def plan_stream(payload: PlanIn) -> StreamingResponse:
    """跑规划流水线，实时推送阶段事件，最后推送结果或错误。

    事件序列：

    - `stage` 每个节点开始时一条，`{"node": "intent", "label": "理解需求"}`
    - `done`  成功，`{"trip_id": "...", "name": "...", "total_days": N}`
    - `error` 失败，结构与该错误在普通接口下的响应体一致
    """
    request = trip_service.PlanRequest(
        query=payload.query,
        start_date=payload.start_date,
        days=payload.days,
        name=payload.name,
    )
    return StreamingResponse(
        _stream_plan(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 若日后放到反向代理后面，这一条能阻止它缓冲整个响应
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_plan(request: trip_service.PlanRequest) -> AsyncIterator[str]:
    # 阶段回调是同步的，且与流水线在同一个事件循环里，
    # 所以可以安全地往队列里直接投递。
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()

    def on_stage(stage: PlanStage) -> None:
        queue.put_nowait((EVENT_STAGE, {"node": stage.node, "label": stage.label}))

    async def run() -> None:
        try:
            result = await trip_service.plan_and_save(request, on_stage=on_stage)
            queue.put_nowait(
                (
                    EVENT_DONE,
                    {
                        "trip_id": result.trip_id,
                        "name": result.plan.name,
                        "total_days": result.plan.total_days,
                        "city_names": list(result.plan.city_names),
                    },
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 任何失败都要变成一条可读的事件
            status_code, body = describe_error(exc)
            if status_code >= 500:
                logger.exception("规划失败")
            queue.put_nowait((EVENT_ERROR, body))

    task = asyncio.create_task(run())
    try:
        while True:
            event, data = await queue.get()
            yield format_sse(event, data)
            if event in (EVENT_DONE, EVENT_ERROR):
                break
    finally:
        # 客户端断开时生成器会被关闭，把还在跑的流水线一并停掉
        if not task.done():
            task.cancel()
