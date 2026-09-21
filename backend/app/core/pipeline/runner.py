"""任务运行器与注册表。

**这是对参考实现最严重那个问题的结构性修正。**

参考实现的 `GET /api/tasks/{id}/stream` 直接在处理函数里调 `run_pipeline`，
没有注册表。后果是：`EventSource` 断线后自动重连会再跑一遍完整流水线；
用户开第二个标签页也会。两次运行并发写同一个 task 的证据、trace、
专家统计——表现是证据翻倍、成本翻倍，而日志里没有任何异常。

修法不是"在处理器里加个 if"——那样每加一个调用入口就要记得加一次。
这里让**流水线的启动只能通过 `ensure_runner`**，而它是幂等的：
已存在的 runner 直接返回，不会启动第二次。

状态的唯一来源是事件流
----------------------
任务的进度、阶段、状态不另外维护一份。`tasks` 表的行是从事件流
顺带更新的（`TaskRunner._track`），所以"接口返回的状态"和
"工作台看到的状态"必然一致——它们读的是同一批事件。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.core.modes import ModeConfig, get_mode
from app.core.observability.events import EventJournal, make_journal
from app.core.observability.trace import Span, Tracer, use_tracer
from app.core.pipeline.context import PipelineContext
from app.core.pipeline.orchestrator import RunResult, run_pipeline
from app.core.pipeline.stages import STAGE_ORDER, TERMINAL_STAGE, stage_label
from app.db.repo import events as events_repo
from app.db.repo import evidences as evidences_repo
from app.db.repo import reports as reports_repo
from app.db.repo import tasks as tasks_repo
from app.db.repo import traces as traces_repo
from app.providers.errors import is_retryable
from app.providers.registry import get_fetcher, get_llm, get_search

#: 任务的终态。落在这些状态上的任务**永远不会再跑一遍流水线**——
#: 这正是 `ensure_runner` 判断依据的来源。它与 `TERMINAL_STAGE`（"done"，
#: 描述的是阶段）不是一回事：这个描述的是任务行。
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})

log = logging.getLogger(__name__)

#: 等用户回答澄清问题。**刻意不在 `TERMINAL_STATUSES` 里**：
#: 它不是终态，任务马上还要接着跑。放进终态的那个错误很隐蔽——
#: `ensure_runner` 看到终态会走 `recover()`（只回放历史、不启动），
#: 于是用户答完澄清、请求继续，流水线却再也不会动了，
#: 而工作台上显示的历史完整、状态正常。
AWAITING_CLARIFY = "awaiting_clarify"

#: 任务行可能出现的**全部**状态。上面两个集合的并集，再加上建行时的初始值。
#:
#: 这个常量存在是因为它以前不存在，而缺了它的代价很具体：前端的
#: `TaskStatus` 写成了 `'created' | 'clarifying' | 'running' | 'done' | 'failed'`，
#: 与真正会落库的那六个只对得上三个。没有东西会因此报错——
#: 澄清页的 `status === 'clarifying'` 恒为假，于是它永远不跳；
#: 取消掉的任务在列表里渲染成一个空标签。
#:
#: 现在它和 `contracts/task_states.json` 有一份契约，两侧各有测试比对。
TASK_STATUSES = frozenset({"pending", "running", AWAITING_CLARIFY} | TERMINAL_STATUSES)


@dataclass
class RunnerState:
    """给接口用的任务快照。字段与 SSE 事件里的字段同名同义。"""

    task_id: str
    status: str = "pending"
    stage: str = ""
    progress: float = 0.0
    report_id: str = ""
    error: str = ""
    #: 每个阶段的当前状态，前端 DAG 直接读它。
    nodes: dict[str, str] = field(default_factory=dict)
    #: 澄清。`need_clarify` 为真时任务停在 `AWAITING_CLARIFY`，
    #: 等 `POST /api/tasks/{id}/clarify` 把答案送进来。
    #:
    #: 问题清单存在这里而不是只留在 `ctx` 里：澄清页刷新之后要能重新渲染
    #: 那几个问题，而 `ctx` 在进程重启后不存在——那时唯一还在的地方是任务行。
    need_clarify: bool = False
    clarify_questions: list[dict] = field(default_factory=list)
    #: 需求理解阶段确定的调研对象与候选品牌。澄清页要显示它们，
    #: 否则用户只能凭记忆判断"这问题问的是不是我想要的那个东西"。
    subject: str = ""
    brands: list[str] = field(default_factory=list)
    started_monotonic: float = field(default_factory=time.monotonic)


class TaskRunner:
    """一次任务的运行器。

    一个任务一个 runner，一个 runner 一个 asyncio 任务、一个 journal、
    一个 tracer。三者一一对应是刻意的：任意两个任务共享其中任何一个，
    都会让"这个事件属于谁"变得不可回答。
    """

    def __init__(
        self,
        task_id: str,
        query: str,
        *,
        mode: ModeConfig | None = None,
        mode_key: str = "",
        clarify_answers: dict[str, Any] | None = None,
        auto_clarify: bool = False,
        gate_clarify: bool = False,
    ) -> None:
        self.task_id = task_id
        self.query = query
        self.mode = mode or get_mode(mode_key)
        self.clarify_answers = dict(clarify_answers or {})
        self.auto_clarify = auto_clarify
        #: 是否在澄清处停下等用户。CLI 与测试走 False（一条命令跑到底），
        #: HTTP 入口走 True。默认 False 是刻意的：让"没人答就一直等"
        #: 成为一个必须显式打开的行为，而不是一个忘了关就永远挂住的默认。
        self.gate_clarify = gate_clarify

        # 挂两个消费者：一个落库（断线续传靠它），一个同步任务行。
        # 顺序无关，两者互不依赖。
        self.journal: EventJournal = make_journal(
            task_id, sink=events_repo.make_sink()
        )
        self.journal.add_sink(self._track)
        self.tracer = Tracer(task_id, on_span=self._on_span)
        self.state = RunnerState(
            task_id=task_id,
            nodes=dict.fromkeys(STAGE_ORDER, "pending"),
        )

        #: 流水线协程。拿住它是为了能在服务关闭时取消，以及让
        #: "已经启动过"成为一个可判断的事实而不是靠状态推断。
        self._task: asyncio.Task | None = None
        self._result: RunResult | None = None
        self._ctx: PipelineContext | None = None
        self._finished = threading.Event()
        #: 需求理解跑过没有。`proceed()` 要靠它跳过 intake——
        #: 而这个"跳过"不能靠"ctx 里已经有 brands"来推断：
        #: 模型没给出品牌时 `brands` 是空的，那会被误判成"没跑过"，
        #: 于是重跑一遍 intake，把澄清页上那批问题换成另一批。
        self._intake_done = False
        #: 停在澄清处。用它而不是看 `state.status`：
        #: `_finalize` 的 finally 分支也要问这个问题，而它跑的时候
        #: 状态可能已经被别处改过了。
        self._paused = False
        #: 需求理解跑完（或被暂停、或失败）时置位。`begin()` 等它。
        self._intake_ready = asyncio.Event()
        #: 调研对象与品牌是否已经同步到任务行。见 `_sync_identity`——
        #: 只写一次，所以要有这个标记，不能靠"值是不是空"来判断：
        #: 模型真的没解析出品牌时值就是空的，那会被反复重写。
        self._identity_synced = False

    # ============================================================
    # 生命周期
    # ============================================================

    def start(self) -> TaskRunner:
        """从头启动流水线。**重复调用是安全的**：已经在跑就什么都不做。"""
        if self._task is not None and not self._task.done():
            return self
        self._paused = False
        self.state.status = "running"
        self.state.stage = STAGE_ORDER[0]
        tasks_repo.update(
            self.task_id, status="running", stage=STAGE_ORDER[0], progress=0.0
        )
        self._task = asyncio.create_task(self._run())
        return self

    def proceed(self) -> TaskRunner:
        """从澄清处接着跑剩下的阶段。

        与 `start()` 的差别只有一个：`_intake_done` 已经是真，所以
        `_run` 不会重跑需求理解。做成两个方法而不是给 `start` 加一个布尔参数，
        是为了让"从头跑"和"接着跑"在**调用点上**就是两件事——
        传错一个 `False` 的后果是重跑 intake，而重跑会把澄清页上
        那批问题换成另一批，用户答的问题和跑的问题对不上。
        """
        if self._task is not None and not self._task.done():
            return self
        self._paused = False
        self.state.status = "running"
        tasks_repo.update(self.task_id, status="running")
        self._task = asyncio.create_task(self._run())
        return self

    def answer(self, answers: dict[str, Any]) -> TaskRunner:
        """收下澄清答案并继续。

        **刻意不把 `state.need_clarify` 清成 False。** 它记的是一个事实
        （这个需求信息不够，需求理解那一步生成了问题），而"正在等回答"
        是 `status == AWAITING_CLARIFY`。

        第一版在这里顺手清了一下，`need_clarify` 于是变成了
        `awaiting_clarify` 的同义词——后果是答完之后，那份报告就再也
        说不出来"这次调研当初问过用户哪几个问题"。
        """
        self.clarify_answers = {**self.clarify_answers, **answers}
        # ctx 已经建好了（intake 跑过一次），所以答案要同时写回它——
        # `_build_context` 只在第一次生效，光改 self.clarify_answers
        # 不会传下去。
        if self._ctx is not None:
            self._ctx.clarify_answers = dict(self.clarify_answers)
        return self.proceed()

    async def wait(self) -> RunResult:
        """等流水线跑完。CLI 与测试用。"""
        if self._task is None:
            self.start()
        assert self._task is not None
        # 取消是**关闭时的正常路径**（服务关停会取消在跑的协程），
        # 不是失败：结果已经在 `_finalize` 里落定了，照常返回。
        with contextlib.suppress(asyncio.CancelledError):  # pragma: no cover
            await self._task
        return self._result or RunResult(error="任务未产生结果")

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self.state.status = "cancelled"
        tasks_repo.update(self.task_id, status="cancelled")

    @property
    def result(self) -> RunResult | None:
        return self._result

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

    @property
    def context(self) -> PipelineContext | None:
        """流水线上下文。跑完之后接口从它读证据、trace、指标。"""
        return self._ctx

    # ============================================================
    # 内部
    # ============================================================

    async def _run(self) -> None:
        try:
            if self._ctx is None:
                self._ctx = self._build_context()
            # **必须把 tracer 绑进上下文**，否则 `span()` 一律退化成空操作：
            # 每次调用照样跑，但一条埋点都不记，成本、耗时、trace 面板
            # 全部静默为空。这种失败没有任何异常，报告看起来只是"很便宜"。
            #
            # 在这里绑是有效的：`create_task` 在创建时就复制了上下文，
            # 而 `asyncio.to_thread` 在调用时复制——两者都继承到这个绑定。
            with use_tracer(self.tracer):
                self._result = await run_pipeline(
                    self._ctx,
                    intake_done=self._intake_done,
                    # 只在**第一次**跑的时候才可能暂停。`proceed()` 那次
                    # 已经带着答案了，再问一次没有意义。
                    pause_on_clarify=self.gate_clarify and not self._intake_done,
                    on_intake_settled=self._intake_settled,
                )
            # 这里**不再重复调 `_pause()`**：停下来的动作已经在
            # `_intake_settled` 里做完了。两处都调也能跑（它是幂等的），
            # 但那意味着"什么情况算暂停"有两个判断点，将来只改一处
            # 就会出现"状态说是 stopped、界面却在跑"。
        finally:
            # 无论走到哪儿（跑完 / 停住 / 抛异常），需求理解这一步都不会
            # 再来第二遍。放在 `finally` 而不是成功分支上：漏了这条，
            # 一次失败的 intake 会被下一次 `proceed()` 重跑，
            # 而那时上下文已经带着半套结果了。
            self._intake_done = True
            if not self._paused:
                self._finalize()
            # 最后才叫醒等待者，而且只作为**兜底**：正常路径上
            # `_intake_settled` 早就把它叫醒了。放在 `_finalize()` 之后
            # 是为了让这条兜底路径醒来时看到的是一个已经定下来的状态
            # （不然拿到的是"running"，而任务其实已经失败了）。
            self._intake_ready.set()

    def _intake_settled(self, paused: bool) -> None:
        """需求理解有了结论。**`begin()` 唯一的唤醒点，就在这一刻。**

        这个函数存在的原因是它以前不存在：唤醒事件原本只在 `_run` 的
        `finally` 里 `set()`，而那已经是整条流水线的结尾。于是
        `POST /api/tasks` 会在整份报告写完之前一直不响应，
        而它的 docstring 写的是"等需求理解结束"——注释和代码说的不是一回事，
        测试也看不出来（mock provider 跑完整条流水线只要几毫秒）。
        """
        if paused:
            self._pause()
        self._intake_ready.set()

    # 顺序说明：先 `_pause()` 再 `set()`，不是因为现在有竞态。
    # `_pause()` 是同步的，而且它和 `set()` 之间没有 `await`，
    # 所以就算反过来写，被唤醒的协程也要等到本协程让出控制权才会跑——
    # 也就是说"先改状态、再公布"这件事今天是自动成立的。
    # 之所以仍然按这个顺序写：`_pause()` 会写库，它将来完全可能变成
    # `async def`（那时候中间就多了一个 `await`，竞态立刻成真），
    # 而"公布了就必须已经是最终状态"本身是这条函数该有的契约，
    # 不该靠调度器的实现细节来兑现。

    async def begin(self) -> TaskRunner:
        """启动，并**等需求理解结束**再交回控制权。

        HTTP 的建任务入口用它：澄清问题是在需求理解那一步生成的，
        所以"把问题返回给前端"必须等它跑完。CLI 不用它——一条命令
        跑到底的路径上没有必要在中间停一下。

        等的是一个 `Event` 而不是直接 `await` 那个协程：需求理解正常结束时
        协程并不会结束（后面还有六个阶段），所以能等的只有"那一步完成了"
        这件事本身。
        """
        if self._task is None or self._task.done():
            self.start()
        await self._intake_ready.wait()
        return self

    def _pause(self) -> None:
        """停在澄清处。

        **不关 journal**，这是最关键的一处。`close()` 会让所有订阅者结束
        迭代，而此刻工作台的 SSE 连接正等着把那几个问题显示给用户——
        关掉它，前端收到的是一个"流已结束"的信号，看起来像任务已经跑完了，
        于是它既不会显示待回答的问题，也不会在用户答完之后重连。

        任务行同步写库，这样进程重启后 `GET /api/tasks/{id}` 仍能告诉
        前端"在等回答"以及**问的是哪几个问题**。
        """
        self._paused = True
        self.state.status = AWAITING_CLARIFY
        ctx = self._ctx
        if ctx is not None:
            self.state.need_clarify = True
            self.state.clarify_questions = list(ctx.clarify_questions)
            self.state.subject = ctx.subject
            self.state.brands = list(ctx.brands)
        tasks_repo.update(
            self.task_id,
            status=AWAITING_CLARIFY,
            stage=STAGE_ORDER[0],
            progress=self.state.progress,
            need_clarify=1,
            clarify_questions=self.state.clarify_questions,
            subject=self.state.subject,
            brands=self.state.brands,
        )

    def _build_context(self) -> PipelineContext:
        return PipelineContext(
            task_id=self.task_id,
            query=self.query,
            mode=self.mode,
            tracer=self.tracer,
            journal=self.journal,
            llm=get_llm(),
            search=get_search(),
            fetcher=get_fetcher(),
            clarify_answers=self.clarify_answers,
            auto_clarify=self.auto_clarify,
        )

    def _finalize(self) -> None:
        """收尾：落库 trace、更新任务行、关闭 journal。

        `close()` 必须在最后：订阅者靠它结束迭代，否则 SSE 连接会一直挂着，
        而浏览器看到的是一条**永远不结束的流**——它不会报错，只是不再有内容，
        前端于是既不显示"已完成"也不重连。

        是同步方法而不是 async：它做的全是同步 IO（SQLite），
        声明成 async 只会让人以为它会被 await 到别的线程上。
        """
        result = self._result
        error = result.error if result is not None else ""
        report_id = result.report.report_id if (result and result.report) else ""
        status = "failed" if error else "done"

        try:
            traces_repo.save_many(self.task_id, self.tracer.snapshot())
        except Exception as exc:  # noqa: BLE001 - 埋点落库失败不该让任务算失败
            self.journal.publish("error", {
                "stage": "trace",
                "message": f"埋点落库失败：{exc}",
                "kind": type(exc).__name__,
                # 落库失败是本地 SQLite 的问题，不是 provider 的。重试不会好，
                # 得有人去看——所以这里恒为 false 是有信息量的，不是凑数。
                "retryable": is_retryable(exc),
            })

        self.state.status = status
        self.state.error = error
        self.state.report_id = report_id
        self.state.progress = 1.0 if status == "done" else self.state.progress

        # 任务行不存在时忽略：跑到这里说明流水线本身已经结束了，
        # 一个更新不上的任务行不该把整个收尾掀掉。
        with contextlib.suppress(Exception):  # pragma: no cover
            tasks_repo.update(
                self.task_id,
                status=status,
                stage=TERMINAL_STAGE,
                progress=self.state.progress,
                error=error,
            )

        self._finished.set()
        self.journal.close()

    def _track(self, event: Any) -> None:
        """事件 → 任务行。**状态的唯一同步点。**

        顺带更新而不是另开一条"状态更新"通路：两处分别维护状态时，
        它们迟早会不一致，而不一致的表现是**接口说完成了、工作台还在跑**。
        """
        self._apply_event(event, persist=True)
        self._sync_identity()

    def _sync_identity(self) -> None:
        """把需求理解解析出的调研对象与品牌同步到任务行。**整个运行只写一次。**

        为什么不能等到收尾再写
        --------------------
        原先只有**停在澄清处**那条路（`_pause`）写这两个字段，收了澄清闸门
        的路径一个都没写。症状不是报错，是工作台顶上那句
        「（尚未解析出调研对象）」——一个跑到 92% 的任务，界面说不出它在调研什么；
        而报告里明明写着品牌名（那是 `assemble` 从 ctx 取的，走的另一条路）。
        实测这台机器 116 行任务里 `subject` 全是空串，包括 12 条已经 `done` 的。

        为什么不在 `_finalize` 里补一刀
        ---------------------------
        那样只在跑完之后才对，而这句话是**运行期间**要看的。需求理解一结束
        这两个值就定了，所以这里挂在"每次落库事件"上、用标记保证只写一次。

        写失败不影响流水线：它和进度一样是为界面服务的（与 `_bump` 同理）。
        """
        if self._identity_synced:
            return
        ctx = self._ctx
        if ctx is None or not ctx.subject:
            return
        self._identity_synced = True
        self.state.subject = ctx.subject
        self.state.brands = list(ctx.brands)
        with contextlib.suppress(Exception):  # pragma: no cover
            tasks_repo.update(
                self.task_id, subject=ctx.subject, brands=list(ctx.brands)
            )

    def _apply_event(self, event: Any, *, persist: bool) -> None:
        """事件 → 内存状态。`persist` 决定要不要顺带写任务行。

        分成两半是为了让**恢复历史任务**能复用同一段"事件→状态"逻辑
        （`recover` 走 `persist=False`）。回放一遍历史事件如果也写库，
        就变成了"打开一个历史报告会改它的任务行"——一个没人想要、
        也很难发现的行为。
        """
        data = event.data or {}
        if event.type == "node_update":
            # 键名是 `stage` 不是 `node`。事件里那个值就是一个 StageId，
            # 而 thought / progress / error 三种事件也都把它叫 `stage`——
            # 单独一种叫 `node` 会让"这两个字段是不是一回事"要靠读代码回答。
            stage = str(data.get("stage", ""))
            if stage in self.state.nodes:
                self.state.nodes[stage] = str(data.get("status", "done"))
            if stage:
                self.state.stage = stage
            self._bump(data.get("progress"), persist=persist)
        elif event.type == "progress":
            self._bump(data.get("progress"), persist=persist)
        elif event.type == "done":
            self.state.progress = 1.0

    def _bump(self, progress: Any, *, persist: bool = True) -> None:
        """进度只增不减。

        事件从多个线程发布，落库顺序与发生顺序不一定一致（采集是并发扇出的）。
        直接用最新值会让进度条来回跳，而"倒退的进度条"会让人以为任务重启了。
        """
        if not isinstance(progress, (int, float)):
            return
        if float(progress) <= self.state.progress:
            return
        self.state.progress = float(progress)
        if not persist:
            return
        # 进度落库失败不该让流水线失败：它是**为界面服务的**，
        # 界面少一次更新远好过整条流水线因为一次写库抖动而中断。
        with contextlib.suppress(Exception):  # pragma: no cover
            tasks_repo.update(
                self.task_id, stage=self.state.stage, progress=self.state.progress
            )

    def recover(self, record: Any) -> TaskRunner:
        """把一个**已经跑完**的任务接回内存：只回放历史，绝不重跑流水线。

        走这条路的场景是"进程重启之后又打开了这个任务"。它与
        `ensure_runner` 的启动路径的区别在于没有任何流水线协程：
        journal 从 `task_events` 灌满之后立刻关闭，`stream()` 会把
        历史事件全部补发完然后正常结束。

        顺带把 DAG 的节点状态从事件里重建出来。之前这里是直接给空 dict——
        那样工作台刷新后会显示"还没开始"，而事实是"已经跑完了"。
        事件里本来就有全部 `node_update`，重建是免费的。
        """
        self.journal.hydrate(events_repo.list_since(self.task_id, 0))
        for event in self.journal.all():
            self._apply_event(event, persist=False)

        report = reports_repo.get_by_task(self.task_id)
        self.state.status = record.status
        self.state.error = record.error
        self.state.report_id = report.report_id if report else ""
        # 澄清的**事实**跟着恢复（不是状态）：一份"当初问过用户三个问题、
        # 按某某选项跑的"报告，在报告页上应该说得出来。这件事只有任务行还记得——
        # 事件流里只有一句 `needClarify: true`，问题本身不在里面。
        self.state.need_clarify = bool(record.need_clarify)
        self.state.clarify_questions = list(record.clarify_questions or [])
        self.state.subject = record.subject
        self.state.brands = list(record.brands or [])
        # 失败的任务没有 `done` 事件（进度没被推到 1），以任务行为准。
        self.state.progress = max(self.state.progress, float(record.progress or 0.0))
        if not self.state.stage:
            self.state.stage = record.stage or TERMINAL_STAGE

        self._finished.set()
        self.journal.close()
        return self

    def _on_span(self, span: Span) -> None:
        """span 结束 → 推一条 trace 事件。

        在**完成时**推而不是开始时：一个 span 最有用的信息是它花了多久、
        花了多少钱，而这两样在开始时都还不存在。先推一条"开始了"再推一条
        "结束了"，会让前端收到两倍的事件去做一件一条就能做完的事。

        载荷嵌在 `span` 下面。这**不是**风格选择：`Span` 落库那条路
        （`traces._row_to_span`）带 `taskId`，而这个键是信封保留键，
        摊平的话 `publish()` 会直接抛错——正是这个冲突让最初的实现
        在第一个阶段就崩了。
        """
        self.journal.publish("trace", {"span": span.to_event()})

    # ============================================================
    # 订阅
    # ============================================================

    async def stream(self, from_seq: int = 0) -> AsyncIterator[str]:
        """SSE 帧流。`from_seq` 来自 `Last-Event-ID`。"""
        async for event in self.journal.subscribe(from_seq):
            yield event.to_sse()

    def snapshot(self) -> dict:
        return {
            "taskId": self.state.task_id,
            "status": self.state.status,
            "stage": self.state.stage,
            "stageLabel": stage_label(self.state.stage),
            "progress": self.state.progress,
            "reportId": self.state.report_id,
            "error": self.state.error,
            "nodes": dict(self.state.nodes),
            "lastSeq": self.journal.last_seq,
            "elapsedMs": int(
                (time.monotonic() - self.state.started_monotonic) * 1000
            ),
            # 澄清相关。跟着快照一起给，是为了让澄清页刷新后能原样重建——
            # 否则那几个问题只在 POST 的响应里存在过一次，页面一刷新就没了，
            # 而任务还停在那儿等一个用户已经看不到的问题。
            #
            # **两个字段含义不同，别混**：
            #   needClarify     事实——这个需求信息不够，intake 生成了问题。
            #                   跑完的任务也可能是真（它当时确实问了）。
            #   awaitingClarify 状态——现在正停着等回答。
            # 判断"要不要跳澄清页"看 `awaitingClarify`。看 `needClarify`
            # 的话，一个早就跑完的任务会在每次打开时把人拉回澄清页。
            "needClarify": self.state.need_clarify,
            "awaitingClarify": self.state.status == AWAITING_CLARIFY,
            "clarifyQuestions": [dict(q) for q in self.state.clarify_questions],
            "subject": self.state.subject,
            "brands": list(self.state.brands),
        }


# ============================================================
# 注册表
# ============================================================

_RUNNERS: dict[str, TaskRunner] = {}
_RUNNERS_LOCK = threading.Lock()


def ensure_runner(
    task_id: str,
    query: str,
    *,
    mode_key: str = "",
    clarify_answers: dict[str, Any] | None = None,
    auto_clarify: bool = False,
    gate_clarify: bool = False,
    start: bool = True,
) -> TaskRunner:
    """取或创建某个任务的 runner。**同一个任务在本进程里永远只有一个 runner。**

    这个函数是流水线唯一的启动入口。把它做成幂等的，等于把
    "重复订阅导致重跑"从"要记得避免的 bug"变成"写不出来的代码"。

    "跑完了"也算已存在
    ------------------
    这里原先写的是 `if existing is not None and not existing.finished`。
    于是"任务跑完之后再打开一次工作台"会新建一个 runner 并**重跑整条
    流水线**——正是这个模块存在的理由，却漏在了另一个触发点上
    （不是"跑到一半重连"，而是"跑完之后重开"）。

    这个漏法比参考实现那个更难发现：证据不会明显翻倍（evidence_id 由 URL
    摘要生成、跨轮次稳定，`INSERT OR REPLACE` 会把它去重掉），能看见的
    只有**账单翻倍**和报告 id 悄悄换了一个。

    所以现在的规则是：内存里有就一律返回；内存里没有、但任务行已经是
    终态，就走"回放历史"而不是"重新启动"。
    """
    with _RUNNERS_LOCK:
        existing = _RUNNERS.get(task_id)
        if existing is not None:
            return existing
        runner = TaskRunner(
            task_id, query, mode_key=mode_key,
            clarify_answers=clarify_answers, auto_clarify=auto_clarify,
            gate_clarify=gate_clarify,
        )
        _RUNNERS[task_id] = runner

    # 先把历史接上，再决定要不要启动。
    #
    # **这一步必须在 `not start` 那个提前返回之前。** 放在它后面的话，
    # "建好 runner 但先别启动"的调用方（澄清流程重启之后走的就是这条）
    # 会拿到一个 seq 从 0 开始的空 journal，然后 `proceed()` 让流水线接着跑——
    # 新事件叫 1、2、3……与 `task_events` 里已有的行撞主键 `(task_id, seq)`。
    # 落库整批失败，而症状只是"刷新页面少一截历史"。
    # 这条注释是被一条测试逼出来的：第一版就写在 `return runner` 后面。
    #
    # 读库放在锁外（上面那段持锁的代码只碰内存）。空库时是一次廉价的索引查询。
    if runner.journal.last_seq == 0:
        rows = events_repo.list_since(task_id, 0)
        if rows:
            resumed_seq = runner.journal.hydrate(rows)
            log.info(
                "任务 %s 从库里恢复 %d 条历史事件，seq 接到 %d",
                task_id, len(rows), resumed_seq,
            )

    if not start:
        return runner

    # 在锁外启动：`start()` 与 `recover()` 都会读写库，
    # 持锁做 IO 会让所有任务排队。
    record = tasks_repo.get(task_id)
    if record is not None and record.status in TERMINAL_STATUSES:
        # `recover()` 内部也会 hydrate 一次。重复是幂等的（已在那儿的 seq
        # 会被跳过），所以不必为它把上面那段绕开。
        return runner.recover(record)
    return runner.start()


def get_runner(task_id: str) -> TaskRunner | None:
    with _RUNNERS_LOCK:
        return _RUNNERS.get(task_id)


def drop_runner(task_id: str) -> None:
    with _RUNNERS_LOCK:
        _RUNNERS.pop(task_id, None)


def reset_runners() -> None:
    """清空注册表。测试之间必须调用。"""
    with _RUNNERS_LOCK:
        _RUNNERS.clear()


def active_runners() -> list[str]:
    with _RUNNERS_LOCK:
        return [task_id for task_id, runner in _RUNNERS.items() if not runner.finished]


def new_task_id() -> str:
    return f"TK-{uuid.uuid4().hex[:12]}"


def load_task(task_id: str) -> dict:
    """恢复一个任务的状态。工作台刷新页面时用它。

    它是一个**只读**入口：不会启动流水线。这一点很重要——
    `GET /api/tasks/{id}` 被前端在每次打开页面时调用，如果它顺手把
    流水线开起来，"打开一个页面"就等于"执行一次任务"。

    优先读 runner（它是最新的，跑完之后也留着），没有就看任务行是不是
    终态——是的话走"只回放"路径，节点状态从事件流重建；
    不是的话说明是进程重启留下的残局，节点状态无从得知。
    """
    runner = get_runner(task_id)
    if runner is not None:
        return runner.snapshot()

    record = tasks_repo.get(task_id)
    if record is None:
        return {}

    if record.status in TERMINAL_STATUSES:
        # `ensure_runner` 在这个状态下会走 `recover()`（只回放、不启动），
        # 所以这里借它拿到一份带节点状态的历史快照。
        return ensure_runner(task_id, record.query, mode_key=record.mode).snapshot()

    report = reports_repo.get_by_task(task_id)
    stats = evidences_repo.stats(task_id)
    return {
        "taskId": record.task_id,
        "status": record.status,
        "stage": record.stage,
        "stageLabel": stage_label(record.stage),
        "progress": record.progress,
        "reportId": report.report_id if report else "",
        "error": record.error,
        # 库里说它还在跑，但内存里没有 runner：进程重启留下的残局。
        # 事件流里能推断出节点状态，但那要回放全部事件；这里给空 dict
        # 而不是全 pending——全 pending 会让工作台显示"还没开始"，
        # 而事实是"跑过一段但过程不在内存里"。
        "nodes": {},
        "lastSeq": events_repo.last_seq(task_id),
        "evidenceCount": stats.get("total", 0),
        # **这一条正是澄清页刷新后要走的路**：服务重启过、任务停在
        # `awaiting_clarify`，内存里没有 runner。问题清单只能从任务行读——
        # 事件流里只有一个 `needClarify: true`，问题本身不在里面。
        # 漏掉这几行的症状是"任务说在等回答，但屏幕上一个问题都没有"。
        "needClarify": bool(record.need_clarify),
        "awaitingClarify": record.status == AWAITING_CLARIFY,
        "clarifyQuestions": list(record.clarify_questions or []),
        "subject": record.subject,
        "brands": list(record.brands or []),
    }
