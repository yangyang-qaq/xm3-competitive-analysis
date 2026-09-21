"""任务状态与快照契约：`contracts/task_states.json` 是被执行的那一份。

SSE 事件有契约文件守着，**任务状态这一半当时没有**——于是它就是烂掉的那一半。
前端 `domain.ts` 的 `TaskStatus` 写的是
`'created' | 'clarifying' | 'running' | 'done' | 'failed'`，
而后端会写进任务行的是 `pending / running / awaiting_clarify / done / failed /
cancelled`。六个里只对得上三个，而没有任何东西会因此报错：

- 澄清页的 `status === 'clarifying'` 恒为假 → 那一页永远不跳，
  用户答完问题之后停在一个什么都不做的界面上；
- `cancelled` 不在联合类型里 → 取消掉的任务走到 `switch` 的 default，
  渲染成一个没有标签的空状态；
- `awaiting_clarify` 同理 → 正在等回答的任务显示成未知状态。

三条断言，各自挡一类漂移
------------------------
1. **常量与契约逐字相等**。`TASK_STATUSES` / `TERMINAL_STATUSES` /
   `AWAITING_CLARIFY` 都是。多一个少一个都红。
2. **`snapshot()` 的真实键集合**落在契约声明的 required ∪ optional 里，
   且 required 一个不少。这条挡的是"给快照加了个字段但那只是个字段"——
   加了不问一句"前端要不要用、重启后有没有"，两边就会长出不同的形状。
3. **两个生产者的差异被显式承认**。`runner.snapshot()` 给 `elapsedMs`、
   `load_task()` 的兜底分支给 `evidenceCount`，各自都只有一个。
   这条断言把这个差异钉住：将来有人给兜底分支补上 `elapsedMs`
   （比如用一个假的起始时间算），契约会红，逼他先想清楚
   "编一个 elapsedMs 出来"是不是真比缺着好。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.pipeline import runner as runner_module
from app.core.pipeline.runner import (
    AWAITING_CLARIFY,
    TASK_STATUSES,
    TERMINAL_STATUSES,
    TaskRunner,
    load_task,
)
from app.core.pipeline.stages import PIPELINE_STAGES, TERMINAL_STAGE

#: `backend/tests/contract/` → 仓库根
CONTRACT_PATH = Path(__file__).resolve().parents[3] / "contracts" / "task_states.json"


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


# ============================================================
# 1. 常量
# ============================================================


def test_the_status_set_is_exactly_what_the_contract_says():
    """**这条是整份契约的核心。** 它红的时候，前端那个联合类型一定也错了。"""
    declared = set(_contract()["statuses"]["required"])

    assert set(TASK_STATUSES) == declared, (
        "runner.TASK_STATUSES 与契约不一致；"
        "改状态集合必须同时改 contracts/task_states.json "
        "和 frontend/src/types/domain.ts 的 TaskStatus"
    )


def test_the_terminal_set_is_exactly_what_the_contract_says():
    declared = set(_contract()["statuses"]["terminal"]["values"])

    assert set(TERMINAL_STATUSES) == declared


def test_awaiting_clarify_is_declared_and_is_not_terminal():
    """单独一条，因为这两个是**在同一件事上**最容易写错的两处。"""
    contract = _contract()

    assert contract["statuses"]["awaiting"]["value"] == AWAITING_CLARIFY
    assert AWAITING_CLARIFY not in TERMINAL_STATUSES, (
        "把 awaiting_clarify 放进终态：ensure_runner 会走 recover()（只回放），"
        "于是用户答完澄清、请求继续，流水线再也不会动了"
    )
    assert AWAITING_CLARIFY in TASK_STATUSES


def test_every_status_the_runner_can_write_is_declared():
    """跑一遍真实的断言：模块里出现的状态字面量必须都在声明的集合里。

    不是在源码里搜字符串（那会被注释里的示例误伤），而是核对
    `RunnerState` 的默认值与几条会改状态的路径——改状态的地方就那么几处，
    漏一处就会写出一个前端不认识的 status。
    """
    import inspect
    import re

    source = inspect.getsource(runner_module)
    # 只认 `status="..."` / `status='...'` 这种**赋值**形态，
    # 不认注释与文档字符串里的散文（那些地方提到状态是在解释它们）。
    assigned = set(re.findall(r"""\bstatus\s*=\s*["']([a-z_]+)["']""", source))
    # `_finalize` 里是 `status = "failed" if error else "done"`，正则只抓得到
    # 引号里那一段，所以那两个值是手列出来的。这条注释是刻意的：
    # 手列的清单必须能被核对，否则它就成了一个悄悄失效的白名单。
    assigned |= {"failed", "done"}
    # `status=AWAITING_CLARIFY` 是标识符不是字面量，正则抓不到。
    # 它是这个模块里唯一一个用常量写的状态，所以必须单独确认它落进来了。
    from app.core.pipeline.runner import RunnerState

    assigned.add(AWAITING_CLARIFY)
    assigned.add(RunnerState(task_id="TK-000000000000").status)

    undeclared = assigned - TASK_STATUSES
    assert not undeclared, f"runner.py 会写出契约里没有声明的状态：{sorted(undeclared)}"


# ============================================================
# 2. 快照的键集合
# ============================================================


def _live_snapshot_keys() -> set[str]:
    """不跑流水线，直接看 `snapshot()` 会返回哪些键。

    用一个没启动过的 runner：`snapshot()` 是纯粹的字段读，不依赖流水线跑过。
    """
    runner = TaskRunner(task_id="TK-000000000000", query="对比 A 与 B")
    return set(runner.snapshot())


def test_the_snapshot_has_every_required_key():
    required = set(_contract()["snapshot"]["required"])

    missing = required - _live_snapshot_keys()
    assert not missing, f"snapshot() 少了契约声明的键：{sorted(missing)}"


def test_the_snapshot_declares_no_undeclared_keys():
    """多出来的键也要红。

    它挡的不是"多了个字段"这件小事，而是"加字段时没想过前端要不要用"。
    真正需要的键加进契约只要一行，而漏掉这一步的表现是：
    前端类型里没有它，于是没人读它——那个字段就一直白算着。
    """
    contract = _contract()["snapshot"]
    declared = set(contract["required"]) | set(contract["optional"]["keys"])

    extra = _live_snapshot_keys() - declared
    assert not extra, (
        f"snapshot() 返回了契约未声明的键：{sorted(extra)}；"
        "要么加进 contracts/task_states.json，要么别返回它"
    )


def test_the_post_response_is_the_snapshot_plus_exactly_two_keys(mock_pipeline_db):
    """`POST /api/tasks` 在快照之外多给 `query` 与 `mode`，**多给的不止这两个也红**。

    这条比"契约里写了这两个键"强的地方在于它真的发一次请求：
    契约与实现是两个可以各自写对、却互不相干的东西。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    declared = set(_contract()["snapshot"]["extra_from_post"]["keys"])

    with TestClient(app) as client:
        body = client.post(
            "/api/tasks",
            json={"query": "对比 Notion 与 Obsidian 的定价与协作能力差异", "mode": "quick"},
        ).json()

    extra = set(body) - _live_snapshot_keys()
    assert extra == declared, (
        f"POST 的响应比快照多出 {sorted(extra)}，而契约声明的是 {sorted(declared)}"
    )
    # 快照该给的键一个都不能少——POST 的响应前端直接当快照用。
    assert not set(_contract()["snapshot"]["required"]) - set(body)


# ============================================================
# 3. 两个生产者的差异
# ============================================================


def test_the_two_snapshot_producers_differ_only_where_declared(mock_pipeline_db):
    """兜底分支的键集合要落在 required ∪ optional 里，且**给足 required**。

    这条是这份契约里唯一需要真库的一条：`load_task` 的兜底分支
    只有"库里有一行、内存里没有 runner"时才走得到，而那正是
    「服务重启之后刷新页面」——一个测试很少覆盖、用户经常遇到的路径。
    """
    from app.core.models import TaskRecord
    from app.core.pipeline.runner import drop_runner, new_task_id
    from app.db.repo import tasks as tasks_repo

    task_id = new_task_id()
    # 状态给一个**非终态**的：终态会让 `load_task` 走 `ensure_runner`
    # 那条路（能重建 runner），而这里要测的正是"重建不了"的那条。
    tasks_repo.create(
        TaskRecord(task_id=task_id, query="对比 A 与 B", mode="quick", status="pending")
    )
    drop_runner(task_id)

    keys = set(load_task(task_id))

    contract = _contract()["snapshot"]
    declared = set(contract["required"]) | set(contract["optional"]["keys"])
    assert not keys - declared, f"兜底分支多给了未声明的键：{sorted(keys - declared)}"
    assert not set(contract["required"]) - keys, (
        f"兜底分支少了必需键：{sorted(set(contract['required']) - keys)}；"
        "重启后打开的页面拿到的形状与正常页面不同，而前端不会知道"
    )


def test_elapsed_ms_is_only_where_it_is_declared(mock_pipeline_db):
    """`elapsedMs` 只该出现在内存快照里。

    兜底分支**编不出**它：那个数是 `time.monotonic()` 减启动时刻，
    而进程重启之后启动时刻已经不存在了。编一个出来比缺这个字段更坏——
    一个看起来精确、其实是从"任务行创建时间"凑出来的耗时，
    会被当成真实数字读进报告。
    """
    from app.core.models import TaskRecord
    from app.core.pipeline.runner import drop_runner, new_task_id
    from app.db.repo import tasks as tasks_repo

    task_id = new_task_id()
    tasks_repo.create(
        TaskRecord(task_id=task_id, query="对比 A 与 B", mode="quick", status="pending")
    )
    drop_runner(task_id)

    keys = set(load_task(task_id))
    declared = _contract()["snapshot"]["optional"]["producers"]

    assert "elapsedMs" not in keys, "兜底分支不该给 elapsedMs"
    assert "evidenceCount" not in _live_snapshot_keys(), "内存快照不该给 evidenceCount"
    assert declared["elapsedMs"] == "runner.snapshot()"
    assert declared["evidenceCount"] == "load_task() 兜底分支"


# ============================================================
# 4. 阶段
# ============================================================


def test_the_stage_order_matches_the_contract():
    """顺序的唯一真相源是 `stages.py`；这里只是让它和契约对得上。

    前端 DAG **不读这个文件**，它读 `/api/pipeline/stages`。契约里的这一份，
    存在的理由是让 TypeScript 的 `StageId` 联合类型有个可比对的对象——
    `nodes` 的键如果超出那个联合类型，读它的 switch 会静默走 default。
    """
    contract = _contract()["stages"]

    assert list(contract["order"]) == [stage.key for stage in PIPELINE_STAGES]
    assert contract["terminal"] == TERMINAL_STAGE
    assert contract["optional"] == [
        stage.key for stage in PIPELINE_STAGES if stage.optional
    ]


def test_the_declared_optional_stage_is_the_one_that_can_be_skipped():
    """可选阶段在 DAG 上要能显示成「没走这一步」而不是「卡住了」。

    这条断言本身只核对声明，但它红的时候要问的问题是行为层面的：
    一次没触发返工的顺利调研，它的 rework 节点停在哪？
    """
    contract = _contract()["stages"]
    optional = set(contract["optional"])

    assert optional == {"rework"}
    assert optional < set(contract["order"])
    assert "rework" not in contract["order"][: contract["order"].index("audit") + 1], (
        "返工接在质检之后——这是整个设计里最值钱的一个位置选择："
        "写作是最贵的一步，把质检放在它前面，返工的代价是补采而不是重写"
    )
