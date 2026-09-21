"""任务生命周期与 SSE 事件流。

**这个文件是对参考实现最严重那个问题的正面回应。**

参考实现的 `GET /api/tasks/{id}/stream` 在处理函数里直接调 `run_pipeline`。
于是"打开一个页面"等于"执行一次任务"，而 `EventSource` 断线后**会自动重连**——
一次网络抖动就再跑一遍完整流水线，重复写证据、trace、专家统计，
日志里什么都没有。

这里的规矩是：**只有 `POST /api/tasks` 能启动流水线**，而且它走
`ensure_runner`（幂等）。`GET .../stream` 是纯读——没有在内存里的 runner
就去库里回放历史，**绝不启动**。

澄清流程为什么要在这里停下来
----------------------------
澄清问题是需求理解那一步用模型生成的，所以 `POST /api/tasks` 必须
**等那一步跑完**才能把问题返回给前端。`runner.begin()` 就是干这个的：
跑到 intake 结束，然后要么停下（`awaiting_clarify`）要么继续跑完。

这个"停"只在 HTTP 路径上发生。CLI 走 `start()`，一条命令跑到底——
没有人会在终端前面回答三个问题。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.models import TaskRecord
from app.core.modes import DEFAULT_MODE, MODE_CONFIG, get_mode
from app.core.observability.events import PipelineEvent
from app.core.pipeline.runner import (
    AWAITING_CLARIFY,
    ensure_runner,
    get_runner,
    load_task,
    new_task_id,
)
from app.core.pipeline.stages import TERMINAL_STAGE, describe_stages
from app.db.repo import events as events_repo
from app.db.repo import tasks as tasks_repo
from app.db.repo import traces as traces_repo

log = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"])

#: SSE 响应头。
#:
#: `X-Accel-Buffering: no` 是给 nginx 的：它默认会把响应缓冲起来再一次性发出，
#: 而 SSE 的全部意义就在于**边发生边发**。少了这一行，本地开发一切正常，
#: 一放到反向代理后面就变成"跑了三分钟，最后所有事件一起出现"。
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

#: 心跳间隔。SSE 是长连接，中间可能几十秒没有任何事件（写作阶段要跑十几章）。
#: 期间代理和负载均衡会按"空闲连接"把它掐掉，而重连虽然能补发，
#: 但用户会看到进度条无缘无故卡一下。
HEARTBEAT_SECONDS = 15.0


# ============================================================
# 请求体
# ============================================================


class CreateTaskRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="一句话调研需求")
    mode: str = Field(default=DEFAULT_MODE, description="quick / deep / expert")
    #: 缺省 False：HTTP 入口是给人用的，默认应该**问**。
    #: CLI 与压测走另一条路，它们把这个打开。
    autoClarify: bool = False  # noqa: N815 - 与前端及 SSE 载荷的驼峰一致


class ClarifyRequest(BaseModel):
    #: `{问题 id: 用户选择}`。允许只答一部分——
    #: 用户对某个问题没有偏好的时候，空着比瞎选一个更诚实。
    answers: dict[str, str] = Field(default_factory=dict)


# ============================================================
# 流水线元信息
# ============================================================


@router.get("/api/pipeline/stages")
def pipeline_stages() -> dict:
    """DAG 节点。前端**不自己写一遍阶段列表**——

    写一遍的话，将来加一个阶段，后端跑了、前端没显示，而没有任何东西会报错。
    顺序的唯一真相源是 `stages.py` 的 `PIPELINE_STAGES`。
    """
    return {"stages": describe_stages(), "terminal": TERMINAL_STAGE}


@router.get("/api/modes")
def list_modes() -> dict:
    """三档模式。首页的模式选择器读它。

    与上面同一个理由：档位的数字（品牌数上限、检索次数预算、返工轮数）
    是**会变的**，前端硬编码一份就会在改了档位之后继续显示旧数字，
    而用户是照着那个数字决定选哪一档的。
    """
    return {
        "default": DEFAULT_MODE,
        "modes": [
            {
                "key": mode.key,
                "label": mode.label,
                "description": mode.description,
                "maxBrands": mode.max_brands,
                "maxDimensions": mode.max_dimensions,
                "maxSearchCalls": mode.max_search_calls,
                # 返工的检索池**单独报**，不并进上面那个数：
                # 并进去的话"最多搜多少次"就变成一个用户没法验证的合成数字，
                # 而这两个池子的行为完全不同（首轮花完就没了；返工只有
                # 补采时才动）。总上界是两者之和。
                "reworkSearchCalls": mode.rework_search_calls,
                "maxReworkRounds": mode.max_rework_rounds,
                "sectionCount": len(mode.sections),
                "enableSentiment": mode.enable_sentiment,
                "enableStructured": mode.enable_structured,
            }
            for mode in MODE_CONFIG.values()
        ],
    }


# ============================================================
# 建任务
# ============================================================


@router.post("/api/tasks", status_code=201)
async def create_task(body: CreateTaskRequest) -> dict:
    """建一个任务，**并等需求理解跑完**，然后把它当前的样子返回。

    为什么要等：澄清问题是需求理解那一步生成的。不等的话只能返回
    "任务已创建，请去轮询"——而澄清页要显示的那几个问题此刻还不存在。

    代价是这个接口会花一次模型调用（快档，约一两秒）。这是刻意的：
    用户点下"开始调研"之后最想看到的就是"它到底要不要问我什么"。

    返回里 `awaitingClarify` 为真就去澄清页，否则去工作台。
    需求理解本身失败时，`status` 会是 `failed`、`error` 里有原因——
    仍然返回 201（任务这一行确实建出来了），由调用方决定怎么显示。
    """
    query = body.query.strip()
    # `min_length=1` 拦不住纯空白：三个空格是三个字符。而一个空白需求
    # 会一路跑到底，产出一份关于"什么都没有"的报告——它看起来完整、
    # 有章节有图表，只是整篇在讲一个不存在的对象。
    if not query:
        raise HTTPException(status_code=422, detail="query 不能是空白")

    mode = get_mode(body.mode)

    task_id = new_task_id()
    tasks_repo.create(
        TaskRecord(task_id=task_id, query=query, mode=mode.key, status="pending")
    )

    runner = ensure_runner(
        task_id,
        query,
        mode_key=mode.key,
        auto_clarify=body.autoClarify,
        # HTTP 入口打开澄清闸门。这是它和 CLI 唯一的行为差别。
        gate_clarify=True,
        start=False,
    )
    await runner.begin()

    return {**runner.snapshot(), "query": query, "mode": mode.key}


# ============================================================
# 读任务
# ============================================================


@router.get("/api/tasks")
def list_tasks(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str = Query("", description="按状态过滤；空表示全部"),
) -> dict:
    """任务列表。

    `total` 是**过滤之后**的总数，不是"已经取回几条"。前端要分页，
    而分页需要的是"一共有多少条符合条件"，用 `len(items)` 当总数
    会得到一个永远小于等于一页的分页器。
    """
    records = tasks_repo.list_recent(limit=limit, offset=offset, status=status)
    return {
        "total": tasks_repo.count(status=status),
        "limit": limit,
        "offset": offset,
        "items": [record.to_dict() for record in records],
    }


@router.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    """任务快照。工作台与澄清页刷新时用它。

    `load_task` 是只读的——它不会启动流水线。这一点是硬要求：
    这个接口是每次打开页面都会被调用的，如果它顺手把任务开起来，
    "打开一个页面"就等于"执行一次任务"。
    """
    snapshot = load_task(task_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return snapshot


@router.get("/api/tasks/{task_id}/trace")
def get_task_trace(task_id: str) -> dict:
    """span 树的原始形态。决策回放页与 trace 面板读它。

    树在服务端组装（`span_tree`），前端拿到就能直接递归渲染：
    回放滑杆要的是"当前 span 的祖先链"，让前端每次自己 fold 一遍纯属浪费。
    """
    if tasks_repo.get(task_id) is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")

    spans = traces_repo.list_by_task(task_id)
    return {
        "taskId": task_id,
        "spans": traces_repo.span_tree(task_id),
        "summary": _summarize_spans(spans),
    }


def _summarize_spans(spans: list[dict]) -> dict:
    """耗时与成本的总计。

    **按 `kind` 分开算**，不是一个总数：`llm` 与 `search` 的成本差三个数量级，
    合成一个数字之后"这次调优省钱了没有"就答不上来了。
    而"钱花在哪一类调用上"正是要回答的那个问题。

    两个耗时字段都不是「任务跑了多久」
    ---------------------------------
    这里**没有**、也不该有一个叫 `durationMs` 的字段。那个名字会被读成
    "这次任务花了多久"，而那个数只在一个地方：**`done.metrics.durationMs`**
    ——它从任务开始算到报告落库，包含出题、调度、写作、落库，span 全都不覆盖。
    在这里再算一个，就是同一件事有两个会不一致的答案。

    那个数**不叫 `elapsedMs`**：`elapsedMs` 是另一个东西，只存在于
    `node_update` 事件与 `runner.snapshot()`（进行中的进度）。
    `done` 顶层就算出现同名键也是被刻意忽略的——
    `tests/unit/test_cli_render.py` 里用 999999 做过诱饵。

    span 自己只知道两件事：每条占用了多久（相加），和它们合起来盖住了哪一段墙钟。
    实测 `TK-1d1ff1f7090b`：总占用 789 秒，窗口 183 秒，4.3 倍——
    采集阶段是 4 路并发的，那个 789 从没在墙上发生过。
    """
    by_kind: dict[str, dict] = {}
    for span in spans:
        kind = span.get("kind") or "other"
        bucket = by_kind.setdefault(
            kind, {"count": 0, "spanDurationMs": 0, "costUsd": 0.0, "totalTokens": 0}
        )
        bucket["count"] += 1
        bucket["spanDurationMs"] += span.get("durationMs", 0)
        bucket["costUsd"] = round(bucket["costUsd"] + span.get("costUsd", 0.0), 6)
        bucket["totalTokens"] += span.get("totalTokens", 0)

    total_duration = sum(span.get("durationMs", 0) for span in spans)
    window_ms = _span_window_ms(spans)

    return {
        "spanCount": len(spans),
        # 所有 span 的耗时**相加**。并发时远大于端到端耗时，见上。
        "spanDurationMs": total_duration,
        # span 覆盖的墙钟窗口：最早开始 → 最晚结束。**仍然不是任务耗时**，
        # 只是 span 自己盖住的那一段。刻意不叫 elapsedMs，见上。
        "spanWindowMs": window_ms,
        # 并发度 = 总占用 ÷ 窗口。≈1 是一条串行链；4.3 是同时开了四路。
        # 用时变长时，"是每一路都变慢了"与"变成串行了"是两种完全不同的病，
        # 而这个数把它们分开。窗口为 0（无 span / 时间戳缺失）时给 0 而不是除爆。
        "concurrency": round(total_duration / window_ms, 2) if window_ms > 0 else 0.0,
        # 最慢的一条。端到端耗时对用户才是感受得到的那个数，
        # 但它不告诉你去优化什么；最慢的一条才指得出来。
        "slowestMs": max((span.get("durationMs", 0) for span in spans), default=0),
        "costUsd": round(sum(span.get("costUsd", 0.0) for span in spans), 6),
        "totalTokens": sum(span.get("totalTokens", 0) for span in spans),
        "cachedPromptTokens": sum(span.get("cachedPromptTokens", 0) for span in spans),
        # 失败与降级**分开数**，与 `Tracer.metrics()` 的口径一致。
        # 合成一个 `failedCount` 会同时错两次：真实数据里 249 条 span
        # 有 24 条 `degraded` 而 `error` 是 0 —— 印成"24 次失败"，
        # 用户看到的是"这个系统不稳"，而实际是"24 个页面抓到了但正文太薄"，
        # 那是采集的正常形态。反过来，把 error 并进 degraded 也会让
        # "有东西真的坏了"被淹没在几十条降级里。
        "errorCount": sum(1 for span in spans if span.get("status") == "error"),
        "degradedCount": sum(1 for span in spans if span.get("status") == "degraded"),
        "byKind": [
            {"kind": kind, **bucket} for kind, bucket in sorted(by_kind.items())
        ],
    }


def _span_window_ms(spans: list[dict]) -> int:
    """最早开始 → 最晚结束，毫秒。

    时间戳是**带时区的 ISO 串**（`2026-09-18T14:47:26.874+00:00`），
    所以用 `fromisoformat` 而不是字符串切片：切片在跨零点、闰秒、
    以及"某个时刻恰好不带毫秒"这三种情况下都会悄悄给出错的值，
    而错的值在这里只表现为"窗口偏小一点"，看不出来。

    单个时间戳解析不了就跳过那一条，不整块放弃——一条脏数据
    不该让整个成本面板消失。
    """
    started: list[datetime] = []
    ended: list[datetime] = []
    for span in spans:
        begin = _parse_ts(span.get("startedAt"))
        finish = _parse_ts(span.get("endedAt"))
        if begin is not None:
            started.append(begin)
        if finish is not None:
            ended.append(finish)
    if not started or not ended:
        return 0
    span_window = max(ended) - min(started)
    # 负数说明时钟回拨过（或库里混进了两个时区）。宁可给 0 也不给负数：
    # 负的窗口会让前面的除法得到一个负的并发度，而那个数在界面上
    # 看起来像"比串行还串行"，只会带着人去查一个不存在的问题。
    return max(int(span_window.total_seconds() * 1000), 0)


def _parse_ts(value: Any) -> datetime | None:
    """ISO 时间戳 → datetime。拿不准就返回 None（调用方跳过这一条）。

    **必须把 naive 的时间戳补成 UTC**，不能只 `fromisoformat` 就完事：
    一旦列表里同时有带时区与不带时区的值，`max()`/`min()` 比较它们会抛
    `TypeError: can't compare offset-naive and offset-aware datetimes`，
    整个接口 500。而混合的来源很现实——历史行由不同的脚本写进去过。
    补 UTC 与 `core/evidence/credibility.py` 里 `_parse_dt` 的做法一致：
    库里存的一律是 UTC，缺时区就当 UTC。
    """
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp


# ============================================================
# 澄清
# ============================================================


@router.post("/api/tasks/{task_id}/clarify")
async def clarify_task(task_id: str, body: ClarifyRequest) -> dict:
    """交答案并让任务接着跑。

    **只对停在 `awaiting_clarify` 的任务有效**。对别的状态返回 409 而不是
    默默接受：一个已经在跑（或已经跑完）的任务收到答案时，正确答案不是
    "记下来但不用"，而是"告诉调用方这个请求没有意义"。
    """
    record = tasks_repo.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")

    runner = get_runner(task_id)
    if runner is None:
        # 服务重启过，内存里没有它。带着答案重新接上——
        # `ensure_runner` 会先把库里的历史事件灌回 journal，
        # 让新的 seq 接在最大值后面而不是从 1 重新开始
        # （从 1 开始会与 `task_events` 的主键 `(task_id, seq)` 撞车，
        # 而症状只是"刷新页面看不到新事件"）。
        #
        # 代价：需求理解那一步会重跑一次。它已经拿到答案，所以不会再问，
        # 但确实多花一次快档调用——这是"进程重启"这个前提换来的，
        # 不是常规路径的成本。
        runner = ensure_runner(
            task_id,
            record.query,
            mode_key=record.mode,
            clarify_answers=body.answers,
            gate_clarify=False,
            start=False,
        )
        log.info("任务 %s 在重启后恢复，带着 %d 项澄清答案继续", task_id, len(body.answers))
    elif runner.snapshot()["status"] != AWAITING_CLARIFY:
        raise HTTPException(
            status_code=409,
            detail=f"任务当前状态是 {runner.snapshot()['status']}，不在等澄清",
        )

    runner.answer(body.answers)
    return runner.snapshot()


# ============================================================
# SSE
# ============================================================


@router.get("/api/tasks/{task_id}/stream")
async def stream_task(
    task_id: str,
    request: Request,
    from_seq: int = Query(0, ge=0, description="从哪条之后开始；0 表示全量"),
    last_event_id: str = Header("", alias="Last-Event-ID"),
) -> StreamingResponse:
    """事件流。**纯读接口，永远不会启动流水线。**

    续传起点的优先级：`Last-Event-ID` 头 **高于** `?from_seq=`。

    反直觉但正确：浏览器自动重连时会把**记着的水位**放进头里，
    而 URL 还是原来那个（可能带着 `from_seq=0`）。如果查询参数优先，
    每次重连都会从头重发整段历史——去重能挡住重复显示，
    但代价是每断一次网就重传一遍全部事件，而断网是会反复发生的。

    要"从头再来"就新开一个不带该头的连接，那时 `from_seq=0` 生效。
    """
    start = _resume_from(last_event_id, from_seq)
    runner = get_runner(task_id)

    if runner is not None:
        # 有在内存里的 runner：补发历史 + 接上实时推送。
        # 任务跑完之后 journal 是关着的，`subscribe` 会把历史发完然后正常结束，
        # 于是这条连接会自己关掉——而不是永远挂着一条不再有内容的流。
        return StreamingResponse(
            _with_heartbeat(runner.stream(start)),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    if tasks_repo.get(task_id) is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")

    # 没有 runner：任务存在但它的运行过程不在这个进程的内存里
    # （服务重启过，或者这是一个只落了库的旧任务）。
    #
    # 回放库里的事件然后结束。**刻意不去 `ensure_runner`**：
    # 那会顺带启动流水线，而这个接口是 GET——浏览器重连、开第二个标签页、
    # 甚至爬虫抓一下，都会调它。
    log.info("任务 %s 不在内存中，改为回放库里的历史事件", task_id)
    return StreamingResponse(
        _replay_from_db(task_id, start),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


def _resume_from(last_event_id: str, from_seq: int) -> int:
    """算出从哪条事件之后开始补发。

    头部解析失败时**退回查询参数**而不是从 0 开始：从 0 开始意味着
    重发全部历史，而一个畸形的水位值更可能是"客户端记错了"，
    不是"客户端什么都要"。
    """
    raw = (last_event_id or "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            log.warning("Last-Event-ID 不是整数（%r），退回 from_seq=%d", raw, from_seq)
    return max(0, from_seq)


async def _replay_from_db(task_id: str, from_seq: int) -> AsyncIterator[str]:
    for row in events_repo.list_since(task_id, from_seq):
        yield PipelineEvent.from_row(row).to_sse()


async def _with_heartbeat(
    source: AsyncIterator[str], *, seconds: float = HEARTBEAT_SECONDS
) -> AsyncIterator[str]:
    """在事件之间插入心跳注释帧。

    写法上有一点必须小心：`__anext__()` 的 future **要在多轮之间复用**。
    每轮重新调一次 `it.__anext__()` 会在上一次还没返回时对同一个
    异步生成器发起第二次迭代，Python 直接报
    "already running"——而那个错误只在"上一次正好超时了"的时候才出现。

    心跳帧是 SSE 注释行（以 `:` 开头），浏览器会忽略它，
    但连接上的空闲计时器会因此重置。
    """
    iterator = source.__aiter__()
    pending: asyncio.Future | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=seconds)
            if not done:
                yield ": keep-alive\n\n"
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield item
    finally:
        # 客户端断开时 `StreamingResponse` 会把这个生成器关掉。
        # 那个还没落地的 `__anext__` 得一起取消，否则它会在后台
        # 一直等下一个事件——每个断开的连接都留一个。
        if pending is not None and not pending.done():
            pending.cancel()


__all__ = ["router"]
