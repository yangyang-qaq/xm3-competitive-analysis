"""压测场景。四个：`read` / `stream` / `create` / `mixed`。

跑法
----
压测**必须**跑在 mock provider 上，理由见 `_require_mock`。所以：

    # 1) 起一个专用后端，把护栏打开
    cd backend
    FORCE_MOCK_PROVIDER=1 DB_PATH=data/loadtest.db \
        .venv/Scripts/python.exe -m uvicorn app.main:app --port 8021

    # 2) 发压（host 指向那个端口）
    cd loadtest
    LOADTEST_SCENARIO=stream locust -f locustfile.py --headless \
        -u 20 -r 4 -t 2m --host http://127.0.0.1:8021 \
        --csv reports/stream --html reports/stream.html

    #    LOADTEST_SCENARIO=read | stream | create | mixed   （默认 mixed）

`--host` 指 8021 而不是 8020：8020 通常是你正在用的开发后端，
它是用真实 provider 起的，**护栏不生效**（护栏读的是那个进程的环境变量），
`_require_mock` 会在开压之前把它拦下来。

为什么场景用环境变量选，不用 Locust 的 `--tags`
--------------------------------------------
因为 `--tags` **不管用**，而且不管用的方式很隐蔽。

Locust 的 `--tags` 过滤的是**任务**，不是用户类。一个类的任务被过滤空之后，
Locust 只警告一句 `ReadUser had no tasks left after filtering,
instantiating it will fail!`，然后**照样把它实例化**——
而它的每一轮迭代都抛 `No tasks defined on ReadUser`。

实测 `-u 20 --tags stream` 跑 20 秒：

    No tasks defined on ReadUser     334 次
    No tasks defined on CreateUser    64 次

而且因为 20 个用户在三个类之间分摊，**真正压流的用户只有 7 个左右**。
于是命令行上写的 20 和实际压的 20 不是一个数，
而两者看起来都对。（这和 `问题记录.md` 40.1 是同一类事：
命令里的标签与实际跑的东西分叉，且没有东西会发现。）
另外那 796 行错误还会混进聚合行，把"0 失败"变成一个没意义的结论。

所以场景在这里**显式选**：不选中的类被标成 `abstract`，Locust 根本不实例化它们，
`-u 20` 就是 20 个该场景的用户。

这个压测在测什么
--------------
**不是**「系统能扛多少请求」——那不是个有意义的问题，因为请求之间的成本
差了三个数量级：列一次报告列表是查一次 SQLite，跑一个任务是几十次 LLM 调用。
把这两件事放进同一个"每秒请求数"里平均，得到的数不回答任何问题。

它测的是两条具体的路径：

1. **SSE 扇出。** 同一个 `task_id` 上挂 N 个订阅者时，首事件时延会不会塌。
   参考实现在请求处理函数里直接跑流水线，于是"第二个订阅者"等于
   "第二次执行任务"；xm3 用 `ensure_runner` 把它变成一次执行 + N 次广播，
   **这个场景就是那句设计的验证**。
   ——所以 `stream` 刻意让所有用户抢**同一批**任务，不是各建各的。
   各建各的测出来的是"N 个任务并行"，那是另一件事，而且不会暴露这个问题。

2. **并发的 SQLite 写路径。** WAL + 线程局部连接 + `busy_timeout` 是设计时
   写下的取舍（见 `docs/DATA_MODEL.md`），而它只有在真有并发写的时候才
   被验证过。出问题的表现是一个具体的字符串：`database is locked`。
   这是个**是/否**，不是百分位，所以它在收尾时单独报一行。

首事件时延单独打点
----------------
Locust 把一次请求的耗时算成"从发出到把响应体读完"。对 SSE 来说，那个数是
**整个任务跑完的时间**，拿它当"响应时间"报出去，会和别人的 200ms 出现在
同一张表里，然后得出一个荒谬的结论。

所以流那一段**绕开 Locust 的请求记账**（直接用 `requests`，不走 `self.client`），
自己量两段并分别上报：

- `SSE 首事件` —— 发出 → 第一个 `event:` 行。这是"按下按钮到看见第一个字"。
- `SSE 全程` —— 整条流读完。这是"这次任务总共多久"。

两个都留着，因为它们回答的是不同的问题。
"""
from __future__ import annotations

import os
import random
import time
import traceback
from typing import Any

import requests
from locust import HttpUser, between, events, task
from locust.env import Environment
from locust.runners import WorkerRunner
from locust.stats import calculate_response_time_percentile

#: 三家 provider 全在 mock 上才肯开压。见 `_require_mock`。
MOCK_REQUIRED = ("llm_provider", "search_provider", "fetch_provider")

#: 种子任务数。`read` / `stream` 需要有东西可读，而"库里本来就有东西"
#: 是个不可复现的前提：换台机器、清一次库，压测就变成"压一个空库"，
#: 而它**照样全绿**——0 个请求失败，因为根本没有请求。
SEED_TASKS = int(os.getenv("LOADTEST_SEED_TASKS", "3"))

#: 种子的需求。刻意短，因为 mock provider 不读它的语义。
SEED_QUERY = "压测种子任务"

#: 单条流的读超时。mock 下一个 quick 任务不到一秒，60 秒是给"卡住了"留的余量。
STREAM_TIMEOUT = float(os.getenv("LOADTEST_STREAM_TIMEOUT", "60"))

#: 池子。`test_start` 填，所有用户共享——`stream` 场景要的就是这个共享。
_TASKS: list[str] = []
_REPORTS: list[str] = []

#: 整轮压测里见过的坏东西。收尾时报出来。
_SEEN: dict[str, int] = {"database is locked": 0, "5xx": 0, "流没跑到 done": 0}

#: 自定义指标的名字前缀。收尾时要按前缀把它们合起来算，
#: 因为同一种指标会带不同的 `[标签]`（扇出 / 自建）。
_FIRST_EVENT = "SSE 首事件"
_FULL_STREAM = "SSE 全程"


# ============================================================
# 前置条件
# ============================================================


def _require_mock(host: str) -> None:
    """确认对端这台服务真的跑在 mock 上。不满足就**拒绝开压**。

    为什么要在客户端再查一遍，服务端不是已经有护栏了吗
    ----------------------------------------------
    护栏（`FORCE_MOCK_PROVIDER`）只在那台**按它的要求起的**服务上生效。
    而压测里真正会发生的事故是：`--host` 写成了 8020，那上面跑着你平时
    用的开发后端，`.env` 里是 deepseek 和博查。

    **20 个并发用户打到真实 provider 上是真金白银**，而这件事没有别的东西
    拦得住——所以判据只能是「对端自报的是什么」，而不是「我以为我起的是什么」。

    这一条是 `问题记录.md` 40.1 那个教训的另一面。那次是**记了一份自述、
    没核对实际**（表头印 `provider=mock`，跑的却是 deepseek 的真适配器）；
    这里是反过来把自述当准入条件——因为在这个方向上，
    自述和实际不会分叉：`/health` 读的是 `settings.llm_provider`，
    而 registry 构造 provider 时读的是**同一个** `get_settings()` 缓存对象。
    （这一点值得说出来，不然"自述能不能信"就成了一个凭感觉的判断。）

    三个都查。抓取也走外网，漏掉它等于漏掉最常降级的那个。
    """
    url = f"{host.rstrip('/')}/health"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    health = resp.json()

    bad = {key: health.get(key) for key in MOCK_REQUIRED if health.get(key) != "mock"}
    if not bad:
        forced = health.get("force_mock_provider")
        why = "FORCE_MOCK_PROVIDER 按下来的" if forced else "这台服务的 .env 里本来就是 mock"
        print(f"\n[压测] 前置条件通过：{host} 上三家 provider 都是 mock（{why}）\n")
        return

    detail = "、".join(f"{key}={value!r}" for key, value in bad.items())
    raise RuntimeError(
        f"拒绝对 {host} 发压：它有三家 provider 不在 mock 上（{detail}）。\n"
        f"20 个并发用户打到真实 provider 上是真金白银，而且测出来的 p95\n"
        f"反映的是对面 API 的排队情况，不是这个系统的。\n"
        f"请另起一个：\n"
        f"    cd backend && FORCE_MOCK_PROVIDER=1 "
        f".venv/Scripts/python.exe -m uvicorn app.main:app --port 8021\n"
        f"然后用 --host http://127.0.0.1:8021。"
    )


# ============================================================
# 种子
# ============================================================


def _seed(host: str) -> tuple[list[str], list[str]]:
    """建几个任务、等它们跑完，返回 `(task_ids, report_ids)`。

    走**公开 API**（`POST /api/tasks` + 轮询），不直接往库里插。
    直接插快得多，但那样压测读的就是一批"长得像任务的行"，
    而不是流水线真正写出来的行——两者的字段完整度不一样，
    而"报告详情接口读不读得动"恰恰是要压的东西。种子必须走同一条路。
    """
    session = requests.Session()
    created: list[str] = []
    for _ in range(SEED_TASKS):
        resp = session.post(
            f"{host}/api/tasks",
            json={"query": SEED_QUERY, "mode": "quick", "autoClarify": True},
            timeout=60,
        )
        resp.raise_for_status()
        created.append(resp.json()["taskId"])

    # 等它们跑完。**必须等**：不等的话用户涌进来时种子还在写库，
    # 那批写会跟压测本身的读写抢锁，测出来的首事件时延里混着"建库"的开销。
    deadline = time.time() + 300
    pending = set(created)
    while pending and time.time() < deadline:
        for task_id in list(pending):
            snap = session.get(f"{host}/api/tasks/{task_id}", timeout=30).json()
            if snap.get("status") in ("done", "failed", "cancelled"):
                pending.discard(task_id)
                if snap.get("status") != "done":
                    print(f"[压测] 警告：种子任务 {task_id} 的状态是 {snap.get('status')}")
        if pending:
            time.sleep(0.5)

    if pending:
        raise RuntimeError(
            f"种子任务在 300 秒内没跑完：{sorted(pending)}。后端日志里应该有原因。"
        )

    items = session.get(f"{host}/api/reports", params={"limit": 50}, timeout=30).json()["items"]
    if not items:
        raise RuntimeError(
            "种子任务跑完了但一份报告都没有。压测读不到东西，"
            "而它**不会因此报错**——只会变成压一个空库，然后全绿地结束。"
        )
    return created, [item["reportId"] for item in items]


# ============================================================
# 事件
# ============================================================


@events.test_start.add_listener
def _prepare(environment: Environment, **_kwargs) -> None:
    # 分布式模式下 worker 也会收到 test_start——每个 worker 都种一遍
    # 就是 N 倍的种子任务。只在 master / standalone 上种。
    if isinstance(environment.runner, WorkerRunner):
        return

    host = environment.host or "http://127.0.0.1:8020"
    print(f"[压测] 场景 {SCENARIO!r}，用户类：{'、'.join(c.__name__ for c in _SELECTED)}")
    try:
        _require_mock(host)
        _TASKS[:], _REPORTS[:] = _seed(host)
    except Exception as exc:  # noqa: BLE001 - 任何前置条件不满足都必须停下
        print(f"\n[压测] 前置条件不满足，**不发压**：\n{exc}\n")
        environment.process_exit_code = 1
        environment.runner.quit()
        return

    print(f"[压测] 池子：{len(_TASKS)} 个任务、{len(_REPORTS)} 份报告\n")


def _merged(environment: Environment, prefix: str) -> tuple[int, int, int, int]:
    """把同一前缀下所有 `[标签]` 的指标合成一份 → `(次数, 中位, p95, 最大)`。

    为什么要合：`SSE 首事件 [扇出]` 和 `SSE 首事件 [自建]` 在 Locust 里是
    **两个不同的条目**，分别看会得到两个 p95，而想问的是"这次压测的首事件
    时延是多少"。合成一份才对得上那个问题。

    为什么要自己合：Locust 的 `StatsEntry.get_response_time_percentile`
    只认自己那一条，跨条目的百分位没有现成 API。所以把它们各自的
    `response_times`（`{耗时: 次数}`）并起来再算——
    `calculate_response_time_percentile` 接的正好是这个形状。

    注意属性名是 `method` 不是 `request_type`：触发事件的**关键字参数**
    叫 `request_type`，而条目上存的那个字段叫 `method`。这里一开始写错成
    `request_type`，代价见 `_summary_lines` 的注释。
    """
    times: dict[int, int] = {}
    total = 0
    worst = 0
    for entry in environment.stats.entries.values():
        if entry.method != "SSE" or not entry.name.startswith(prefix):
            continue
        total += entry.num_requests
        worst = max(worst, entry.max_response_time)
        for value, count in entry.response_times.items():
            times[value] = times.get(value, 0) + count
    if not total:
        return 0, 0, 0, 0
    return (
        total,
        calculate_response_time_percentile(times, total, 0.5),
        calculate_response_time_percentile(times, total, 0.95),
        worst,
    )


def _summary_lines(environment: Environment) -> list[str]:
    """收尾汇总的正文。**只算，不打印**——打印在 `_report` 里一次做完。

    为什么要把"算"和"打印"分开
    ------------------------
    第一版是边算边打印。`_merged` 里 `entry.request_type` 那个 AttributeError
    抛出之后，`EventHook.fire` 把它吞进日志（`Uncaught exception in event
    handler`），于是**屏幕上留下的是一个标题加一片空白**：

        压测收尾
        ==============================================================

    那看起来不像出错，像"这次没什么可报的"。一个汇总工具最坏的失败方式
    就是它自己安静地少说一半。

    所以现在先算完再打印：算出问题就打印 traceback，标题和正文作为**一整块**
    输出，"有标题没内容"在结构上不可能出现。
    """
    lines: list[str] = []
    seen_sse = False
    for prefix in (_FIRST_EVENT, _FULL_STREAM):
        count, median, p95, worst = _merged(environment, prefix)
        if not count:
            continue
        seen_sse = True
        lines.append(
            f"{prefix:<8} 中位 {median:>6} ms · p95 {p95:>6} ms · "
            f"最大 {worst:>7} ms（{count} 次）"
        )
    if not seen_sse and SCENARIO in ("stream", "mixed"):
        # 采不到 SSE 指标本身就是个结论，不能只是"这几行不打印"。
        lines.append("**一条 SSE 指标都没采到**——这次没有产生任何流数据。")

    if _SEEN["database is locked"]:
        lines.append(f"**出现了 {_SEEN['database is locked']} 次 `database is locked`**")
        lines.append("  这是并发写路径的硬故障，不是性能问题。见 docs/DATA_MODEL.md 的取舍。")
    else:
        lines.append("database is locked   0 次")

    if _SEEN["流没跑到 done"]:
        lines.append(f"流没跑到 done：{_SEEN['流没跑到 done']} 次")
    if _SEEN["5xx"]:
        lines.append(f"5xx：{_SEEN['5xx']} 次")
    return lines


@events.test_stop.add_listener
def _report(environment: Environment, **_kwargs) -> None:
    """收尾。只报数，不解读。"""
    if isinstance(environment.runner, WorkerRunner):
        return

    try:
        body = _summary_lines(environment)
    except Exception:  # noqa: BLE001 - 汇总自己坏了也必须说出来，不能只留个标题
        body = ["汇总本身出错了：", traceback.format_exc()]

    print("\n".join(["", "=" * 64, "压测收尾", "=" * 64, *body, "=" * 64, ""]))


# ============================================================
# 公共部件
# ============================================================


def _classify(status_code: int, text: str) -> str:
    """这次响应坏在哪。返回空串表示没问题。

    记数与上报分成两步：流那条路绕开了 Locust 的请求记账，
    手上没有可以调 `.failure()` 的 Locust 响应对象，
    而"哪些字符串算故障"这件事只该有一份定义——两处各写一遍，
    迟早有一处漏掉 `database is locked`，而那正是最该被看到的一个。
    """
    if status_code >= 500:
        _SEEN["5xx"] += 1
    if "database is locked" in text:
        _SEEN["database is locked"] += 1
        return "database is locked"
    return ""


class _SeededUser(HttpUser):
    """公共基类：一个自己的流会话 + 用共享的池子。"""

    abstract = True
    #: 用户之间隔一下。0 会让所有用户在同一瞬间齐步走，测出来的
    #: 是"最坏的那一瞬间"，而稳态才是要看的。
    wait_time = between(0.1, 1.0)

    def on_start(self) -> None:
        # 每个用户一个 session。共享一个的话，连接池在多协程下的行为
        # 需要额外的推理才能确信没问题，而这里没有省这一下的理由。
        self.streams = requests.Session()

    @staticmethod
    def _pick_task() -> str:
        return random.choice(_TASKS)

    @staticmethod
    def _pick_report() -> str:
        return random.choice(_REPORTS)

    def _get(self, path: str, name: str) -> None:
        """一次带故障识别的读。"""
        with self.client.get(path, catch_response=True, name=name) as resp:
            reason = _classify(resp.status_code, resp.text[:2000])
            if reason:
                resp.failure(reason)

    # ---- SSE ----

    def consume(self, task_id: str, *, label: str) -> None:
        """挂上 `task_id` 的流读完，量首事件与全程两个数。"""
        url = f"{self.host}/api/tasks/{task_id}/stream"
        started = time.perf_counter()
        first_ms = -1.0
        count = 0
        saw_done = False
        exc: Exception | None = None

        try:
            with self.streams.get(
                url,
                stream=True,
                timeout=STREAM_TIMEOUT,
                headers={"Accept": "text/event-stream"},
            ) as resp:
                if resp.status_code >= 400:
                    text = resp.text[:2000]
                    reason = _classify(resp.status_code, text)
                    exc = RuntimeError(f"HTTP {resp.status_code}：{reason or text[:120]}")
                else:
                    # 显式定编码：`iter_lines(decode_unicode=True)` 在
                    # Content-Type 没带 charset 时会退回 latin-1，
                    # `data:` 里的中文会变乱码。这里只读 `event:` 行，
                    # 但下一个改读 `data` 的人不该踩这个坑。
                    resp.encoding = "utf-8"
                    for raw in resp.iter_lines(decode_unicode=True):
                        line = (raw or "").strip()
                        # 空行是帧分隔，冒号开头是心跳注释——都不是事件
                        if not line or line.startswith(":"):
                            continue
                        if line.startswith("event:"):
                            if first_ms < 0:
                                first_ms = (time.perf_counter() - started) * 1000
                            count += 1
                            if line[6:].strip() == "done":
                                saw_done = True
                                break
        except Exception as err:  # noqa: BLE001 - 断流要变成数据，不能中断这个用户
            exc = err

        elapsed_ms = (time.perf_counter() - started) * 1000
        # 一个事件都没等到：首事件时延就是全程，而不是 -1。
        # 报 -1 的话它会成为 p95 里最小的那个数，把塌掉的那一次藏起来。
        if first_ms < 0:
            first_ms = elapsed_ms

        # 没跑到 done 又不算异常（比如服务端提前关了连接）——
        # 这种流是"看起来成功"的：HTTP 200、有事件、只是没有结尾。
        if not saw_done and exc is None:
            _SEEN["流没跑到 done"] += 1

        for name, ms in (
            (f"{_FIRST_EVENT} [{label}]", first_ms),
            (f"{_FULL_STREAM} [{label}]", elapsed_ms),
        ):
            events.request.fire(
                request_type="SSE",
                name=name,
                response_time=int(ms),
                response_length=count,
                exception=exc,
            )


# ============================================================
# 三个场景类
# ============================================================


class ReadUser(_SeededUser):
    """读路径：报告详情、列表、任务、仪表盘、专家、证据库。

    `GET /api/reports/{id}` 是这里面最重的一个——它要读整份报告 JSON
    （`reports.data` 一列，见 `docs/DATA_MODEL.md` 的取舍），
    也是文库页打开报告时走的那条路。
    """

    weight = 5

    @task(4)
    def report_detail(self) -> None:
        self._get(f"/api/reports/{self._pick_report()}", "/api/reports/[id]")

    @task(2)
    def report_list(self) -> None:
        self._get("/api/reports?limit=20", "/api/reports")

    @task(1)
    def task_list(self) -> None:
        self._get("/api/tasks?limit=50", "/api/tasks")

    @task(1)
    def dashboard(self) -> None:
        self._get("/api/dashboard", "/api/dashboard")

    @task(1)
    def experts(self) -> None:
        self._get("/api/experts", "/api/experts")

    @task(1)
    def evidences(self) -> None:
        self._get("/api/evidences?limit=30", "/api/evidences")


class StreamUser(_SeededUser):
    """SSE 扇出：所有用户抢**同一批**任务。

    这个文件里最要紧的一个类。参考实现里 `/stream` 在处理函数内跑流水线，
    所以第二个订阅者 = 第二次执行；xm3 的 `ensure_runner` 让 N 个订阅者
    共用一次执行。**这个场景就是那句设计的验证**：
    要是 `ensure_runner` 失效了，这里的首事件时延会塌成单用户时的 N 倍，
    同时库里会多出 N 份证据。
    """

    weight = 3

    @task
    def stream_task(self) -> None:
        self.consume(self._pick_task(), label="扇出")


class CreateUser(_SeededUser):
    """建任务 → 消费完整条流。

    每个循环都往库里写一份完整报告，所以权重最低。
    写路径正是 `database is locked` 唯一可能出现的地方。
    """

    weight = 1

    @task
    def create_and_stream(self) -> None:
        task_id: Any = None
        with self.client.post(
            "/api/tasks",
            json={
                "query": f"{SEED_QUERY} {random.randint(0, 9999)}",
                "mode": "quick",
                "autoClarify": True,
            },
            catch_response=True,
            name="/api/tasks [POST]",
        ) as resp:
            if resp.status_code >= 400:
                reason = _classify(resp.status_code, resp.text[:2000])
                resp.failure(reason or f"HTTP {resp.status_code}")
            else:
                task_id = resp.json().get("taskId")
        if task_id:
            self.consume(task_id, label="自建")


# ============================================================
# 场景选择
#
# 这是文件末尾的一段**可执行**代码，不是常量——因为它的作用就是
# 决定 Locust 能看见哪些用户类，而那发生在 import 之后、收集之前。
# 为什么不用 `--tags`，见文件头的"为什么场景用环境变量选"。
# ============================================================

SCENARIOS: dict[str, list[type[_SeededUser]]] = {
    "read": [ReadUser],
    "stream": [StreamUser],
    "create": [CreateUser],
    # mixed 就是三个一起上，按 weight 配比（读 5 : 流 3 : 建 1）。
    "mixed": [ReadUser, StreamUser, CreateUser],
}

SCENARIO = os.getenv("LOADTEST_SCENARIO", "mixed").strip().lower()

if SCENARIO not in SCENARIOS:
    raise SystemExit(
        f"未知的 LOADTEST_SCENARIO={SCENARIO!r}。可选：{'、'.join(SCENARIOS)}"
    )


def _select(scenario: str) -> list[type[_SeededUser]]:
    """把没选中的场景类标成 `abstract`，返回选中的那些。

    **为什么这段必须待在一个函数里。**
    Locust 收集用户类的方式是 `vars(module).items()` —— 扫**模块全局的每一个名字**，
    凡是绑到 `User` 子类的都算（`load_locustfile.py`）。
    第一版这段是写在模块顶层的 `for _cls in {...}`，
    于是循环结束后 `_cls` 这个**名字还在模块里**，绑着最后一个类。
    Locust 于是把那个类收集了两遍，`mixed` 场景直接起不来：

        ValueError: The following user classes have the same class name:
            locustfile.ReadUser, locustfile.StreamUser,
            locustfile.CreateUser, locustfile.ReadUser

    （`read` / `stream` 两个场景反而是好的——那时被重复的那个类恰好是
    abstract 的，于是被跳过了。**一个在部分场景下正确的 bug ，
    比一个在所有场景下都错的更难发现。**）

    函数里的局部变量不会进模块全局，这个坑就不存在了。
    """
    chosen = SCENARIOS[scenario]
    for cls in {c for group in SCENARIOS.values() for c in group}:
        if cls not in chosen:
            cls.abstract = True
    return chosen


_SELECTED = _select(SCENARIO)
