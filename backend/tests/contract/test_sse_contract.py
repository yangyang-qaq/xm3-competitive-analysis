"""SSE 事件契约：`contracts/sse_events.json` 是被执行的那一份。

**这个文件守的是一种不会报错的故障。**

后端把 `node_update` 的 `node` 改名成 `stage`、前端还读 `e.node`，
结果是 `undefined`——DAG 画不出任何节点，页面空着，控制台干净，
服务端日志干净。review 里那个改动看起来完全无害（"改名更一致"）。
这类不一致只能靠一条**读同一份契约**的测试挡住，靠人眼盯不住。

两侧各有一条：这里跑一遍真实的 Mock 流水线，断言推出去的字节与契约
逐键一致；`frontend/src/types/events.contract.test.ts` 断言手写的
TypeScript 样例与契约一致。改字段名必须两边一起改。

三条断言，各自挡一类漂移
------------------------
1. **契约里声明的键一个不少**。少一个 → 前端读它得到 undefined。
2. **推出去的事件里没有未声明的键**。多一个 → 前端类型里没有它，
   而"多出来的东西"从来不会被谁发现并清掉。
3. **契约里声明的每种事件都真的出现过**。这条挡的是另一类问题：
   某个事件类型（比如 `chart`）声明了、前端为它写了分支，
   而后端从来没有发过——那个分支永远走不到，也永远测不到。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.observability.events import EVENT_TYPES, RESERVED_KEYS
from tests.support import Outcome

#: `backend/tests/contract/` → 仓库根
CONTRACT_PATH = Path(__file__).resolve().parents[3] / "contracts" / "sse_events.json"

#: 事件类型 → 它携带的领域对象放在哪个键下（`None` 表示载荷摊平）。
#:
#: 刻意再写一遍（契约文件里已经写过一次）：两边写岔了，
#: `test_the_contract_names_the_shape_of_every_event` 会红。
#: 从契约文件读出来就会失去这个交叉验证。
CARRIED_KEY: dict[str, str | None] = {
    # 携带领域对象的六种：对象嵌在自己的键下
    "thought": "thought",
    "message": "message",
    "evidence": "evidence",
    "trace": "span",          # 键叫 span，因为载荷里装的是 TraceSpan
    "chart": "chart",
    "image": "image",
    # 事件自身的事实的五种：载荷摊平
    "node_update": None,
    "progress": None,
    "report_ready": None,
    "done": None,
    "error": None,
}

#: 健康运行里**必然**出现的类型。
#:
#: `error` 刻意不在里面：它只在失败时出现，健康运行里出现它反而是 bug。
#: 那个豁免必须是有主的——`test_error_is_covered_by_a_real_failure` 真的
#: 把流水线跑挂一次来覆盖它，`test_the_failure_only_exemption_holds_exactly_one_type`
#: 保证豁免名单不会自己长出来。
HEALTHY_RUN_TYPES = frozenset(EVENT_TYPES) - {"error"}


@pytest.fixture(scope="module")
def contract() -> dict:
    assert CONTRACT_PATH.exists(), f"契约文件不在：{CONTRACT_PATH}"
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


@pytest.fixture
async def outcome(run_mock_pipeline) -> Outcome:
    return await run_mock_pipeline()


# ============================================================
# 契约文件自身的完整性
# ============================================================


def test_the_contract_covers_exactly_the_event_types(contract: dict) -> None:
    """契约与 `EVENT_TYPES` 不能各说各话。

    契约里多一种：前端会为它写一个后端不发的事件。
    契约里少一种：那种事件推出去时没有任何断言碰得到它。
    """
    assert set(contract["events"]) == set(EVENT_TYPES)


def test_the_contract_envelope_matches_the_reserved_keys(contract: dict) -> None:
    """信封的四个键必须**就是** `RESERVED_KEYS`。

    这两个集合一旦不同，就意味着有一个键是"载荷里能写、但前端以为是信封填的"，
    而 `publish()` 正是按 `RESERVED_KEYS` 拦截的——拦的是一个集合、
    前端信的是另一个，冲突的地方恰好落在那道缝里。
    """
    assert set(contract["envelope"]["required"]) == RESERVED_KEYS


def test_the_contract_names_the_shape_of_every_event(contract: dict) -> None:
    """摊平的五种、嵌套的六种，契约里必须写明是哪一种。

    这一行是给别人看的，也是给上面的 `CARRIED_KEY` 做交叉验证的：
    两边都写了一遍，写岔了这条会红。
    """
    nested = {name for name, entry in contract["events"].items() if entry["carries"]}
    flat = set(contract["events"]) - nested

    assert nested == {name for name, key in CARRIED_KEY.items() if key}
    assert flat == {name for name, key in CARRIED_KEY.items() if not key}
    assert len(nested) == 6 and len(flat) == 5


def test_every_declared_domain_object_has_a_key_list(contract: dict) -> None:
    """每种被携带的领域对象都要在 `domain` 里有一份键清单，条目非空。"""
    for entry in contract["events"].values():
        target = entry["carries"]
        if not target:
            continue
        assert target in contract["domain"], target
        assert contract["domain"][target], target


# ============================================================
# 真实事件 vs 契约
# ============================================================


def test_a_healthy_run_emits_every_declared_type_it_should(
    outcome: Outcome, contract: dict
) -> None:
    """契约里写了的，流水线必须真的发出来。

    这是几个断言里唯一一个**只能靠真跑**才能做的：静态看代码永远看不出
    "这个类型声明了但没人发"。`chart` 与 `message` 都曾经是这样——
    前端为它们写了永远走不到的 `case`，而"图表能不能画""三层分工可不可见"
    没有任何测试碰得到。
    """
    missing = sorted(HEALTHY_RUN_TYPES - set(outcome.types))

    assert missing == [], f"这些事件类型在健康运行里却从未发出：{missing}"
    assert "error" not in outcome.types, "健康运行里不该有 error——那说明有东西在静默失败"


def test_the_failure_only_exemption_holds_exactly_one_type(contract: dict) -> None:
    """豁免名单上只允许有 `error` 一项。

    这一条守的是上面那条断言的**逃逸口**：某种事件发不出来时，最省事的
    "修复"是把它加进豁免名单——而那条路会把一个真的坏掉的埋点变成
    一行注释。想豁免，就得在这里写下来并说明为什么。
    """
    assert set(EVENT_TYPES) - HEALTHY_RUN_TYPES == {"error"}
    assert set(contract["events"]) - HEALTHY_RUN_TYPES == {"error"}


async def test_error_is_covered_by_a_real_failure(run_mock_pipeline, contract: dict) -> None:
    """失败运行。`error` 是唯一一种健康运行里不该出现的事件，所以它只能靠
    **真的把流水线弄挂**来覆盖。

    不覆盖的后果很具体：前端那个 `case 'error'` 分支、契约里那三个字段、
    以及"任务行会变成 failed"这条行为，全都没有东西在守——而失败路径
    恰恰是最不常被手工走到的那一条。用的是 mock 的失败钩子，
    所以这条用例同样不花一分钱、不出网。
    """
    from app.core.pipeline.runner import TERMINAL_STATUSES
    from app.providers import registry

    # `intake` 的第一步（生成调研范围）失败，异常一路冒到编排层的兜底。
    registry.get_llm().fail_purposes = {"scope"}

    outcome = await run_mock_pipeline()
    payload = outcome.payload("error")

    assert set(payload) - set(contract["envelope"]["required"]) == set(
        contract["events"]["error"]["required"]
    )
    assert payload["stage"] == "intake"
    assert payload["kind"] == "Transient", "kind 要能区分基础设施问题与代码 bug"
    assert "被配置为失败" in payload["message"]

    # 挂了就没有终态报告，也不该有 done —— 前端靠"没有 done"判断这次没成。
    assert "done" not in outcome.types
    assert outcome.result.error
    assert outcome.runner.state.status in TERMINAL_STATUSES


def test_emitted_payloads_carry_every_declared_key(outcome: Outcome, contract: dict) -> None:
    """键一个不少。

    用**并集**而不是某一条：`elapsedMs` 只在阶段结束时才有、`detail` 只在
    有产出的阶段才有。只看第一条会把这些可选键判成缺失。
    """
    problems: list[str] = []
    for name, entry in contract["events"].items():
        if name not in set(outcome.types):
            continue
        missing = set(entry["required"]) - outcome.keys_of(name)
        if missing:
            problems.append(f"{name} 缺少 {sorted(missing)}")

    assert problems == []


def test_emitted_payloads_carry_nothing_undeclared(outcome: Outcome, contract: dict) -> None:
    """键一个不多，包括信封。

    未声明的键是**最隐蔽的一类漂移**：它不会让任何东西坏掉，只是前端
    永远看不到它。于是它会一直在那里，而有价值的字段与它一起被忽略时
    也没人分得清。
    """
    envelope = set(contract["envelope"]["required"])
    problems: list[str] = []

    for name, entry in contract["events"].items():
        allowed = envelope | set(entry["required"]) | set(entry.get("optional") or [])
        extra = outcome.keys_of(name) - allowed
        if extra:
            problems.append(f"{name} 多出未声明的键 {sorted(extra)}")

    assert problems == []


# ============================================================
# 被携带的领域对象
# ============================================================


@pytest.mark.parametrize(
    "event_type",
    ["thought", "message", "evidence", "trace", "chart", "image"],
)
def test_carried_domain_objects_match_their_key_list(
    outcome: Outcome, contract: dict, event_type: str
) -> None:
    """六个领域对象的键集合与契约**完全相等**（不是包含）。

    相等而不是包含：领域对象多一个字段，前端那个接口就少一个字段，
    而报告页上会缺一块内容——静默地缺。多出来的那些最容易留下来，
    因为删掉它需要有人想起来"哦那个字段是给人看的"。
    """
    key = CARRIED_KEY[event_type]
    target = contract["domain"][contract["events"][event_type]["carries"]]
    declared = set(target)

    checked = 0
    for payload in outcome.payloads(event_type):
        carried = payload[key]
        assert isinstance(carried, dict), f"{event_type}.{key} 不是对象"
        assert set(carried) == declared, (
            f"{event_type}.{key} 与契约不符："
            f"多 {sorted(set(carried) - declared)}、"
            f"少 {sorted(declared - set(carried))}"
        )
        checked += 1

    assert checked, f"{event_type} 一条都没有，这条用例什么也没验证"


def test_the_envelope_is_never_shadowed_by_a_nested_object(outcome: Outcome) -> None:
    """每个事件的信封都是完好的——嵌套的对象没有把顶层键挤掉。

    载荷是 `{**envelope, **data}` 拼出来的，所以只要有一处埋点把
    `taskId` 摊平到顶层，信封那一份就会被静默覆盖，前端拿到一个
    属于别的任务的事件。`publish()` 会拦住它（见 `test_events.py`），
    这里再确认一遍**真实的**那批事件一条都没踩到。
    """
    for type_, data in outcome.events:
        assert set(data) >= RESERVED_KEYS, f"{type_} 的信封少了键"
        assert data["taskId"] == outcome.task_id
