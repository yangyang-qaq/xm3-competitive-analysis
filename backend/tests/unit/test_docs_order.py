"""文档与代码一致性：`docs/ARCHITECTURE.md` 的流水线顺序必须等于 `STAGE_ORDER`。

参考实现的架构文档里，流水线写的是"分析 → 撰写 → 质检"，而代码里实际是
"分析 → 质检 → 撰写"。文档与代码脱节到这种程度时，读文档的人会得出
**错误的结论**，而且没有任何机制会提醒他们——一条测试就能永久解决它。

为什么这条测试值得存在
----------------------
大多数"文档一致性测试"最后会退化成一句 `assert "collect" in text`——
那种断言在文档里写"我们不采集"时也成立。所以这里的判据要具体到
**节点顺序**，而且解析规则必须写死在一个地方（`_mermaid_nodes`），
免得测试自己变成第二个真相源。

解析约定
--------
只看 `docs/ARCHITECTURE.md` 里**第一个** ```mermaid 块，取出其中所有
`id[标签]` 的**出现**（按文本顺序，同一个 id 只记第一次），这就是节点顺序。

为什么不按"每行开头的节点定义"取：mermaid 的常见写法是链式
（`intake["…"] --> orchestrator["…"]`），一行里有两个节点，
按行首取只能拿到第一个——第一版就是这么写的，实测只解析出了 `intake`。
链式写法下的节点顺序恰好就是 `id[...]` 的文本出现顺序，
所以"取所有出现"既简单又正确。

边上的标签写成 `-->|"…"|`（竖线），不在方括号里，所以不会混进来。
**第二个 mermaid 块不用这条规则**：它里面有 `subgraph api["…"]`，
`api` / `pipeline` 这些子图名会被当成节点。所以只解析第一块。

`TERMINAL_STAGE`（`done`）也算一个节点：图里画了它，
而它不是 `PIPELINE_STAGES` 的成员（它没有耗时和进度）。
把它单独拼在后面，而不是塞进 `STAGE_ORDER`——那样 `progress_at`
会多算一个占权重的阶段。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.pipeline.stages import (
    PIPELINE_STAGES,
    STAGE_ORDER,
    TERMINAL_STAGE,
    describe_stages,
    next_stage,
    progress_at,
)

ARCHITECTURE = Path(__file__).resolve().parents[3] / "docs" / "ARCHITECTURE.md"

#: `id[标签]`，标签可带引号。**不加 `^` 锚点**：链式写法下一行里
#: 会出现多个节点，锚行首就只能拿到第一个。
_NODE_DEF = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*\"?[^\]]*?\"?\s*\]")


def _mermaid_nodes(text: str) -> list[str]:
    """取第一个 mermaid 块里的节点 id，按首次出现顺序。

    没有 mermaid 块时返回空列表而不是抛异常：那说明**文档被改坏了**，
    而"断言空列表等于 STAGE_ORDER"失败的报错信息比一个 KeyError 有用得多。
    """
    blocks = re.findall(r"```mermaid\n(.*?)```", text, re.DOTALL)
    if not blocks:
        return []
    out: list[str] = []
    for node_id in _NODE_DEF.findall(blocks[0]):
        # 去重保留首次出现：一个节点被声明两次时，"顺序"就没有意义了，
        # 靠去重让顺序仍然是确定的，而不是让重复把它挤歪。
        if node_id not in out:
            out.append(node_id)
    return out


@pytest.fixture(scope="module")
def architecture() -> str:
    if not ARCHITECTURE.exists():
        pytest.fail(f"找不到 {ARCHITECTURE}")
    return ARCHITECTURE.read_text("utf-8")


def test_架构文档里的流水线顺序等于代码(architecture: str) -> None:
    """这一条就是全部目的。"""
    documented = _mermaid_nodes(architecture)
    assert documented, (
        f"{ARCHITECTURE.name} 的第一个 mermaid 块里一个节点都没解析出来。"
        "要么块没了，要么节点写成了 `id[label]` 之外的形状。"
    )

    expected = [*STAGE_ORDER, TERMINAL_STAGE]
    assert documented == expected, (
        "架构文档的流水线顺序与 `stages.py` 不一致。\n"
        f"  文档：{documented}\n"
        f"  代码：{expected}\n"
        "改了顺序就两边一起改——这正是这条测试的用途。"
    )


def test_解析器抓得住一个顺序写反的文档() -> None:
    """给上面的测试做反证：**证明它真的会红**。

    没有这一条的话，`_mermaid_nodes` 写错（比如正则永远匹配不到东西）
    会让上面那条同样绿——因为"空列表"和"顺序一样"都可能走到同一个 assert。

    这里不拿真文档做字符串替换（那样只能测出"改了一个字"），
    而是直接喂两个最小的 mermaid 块：一个对的、一个把 `audit` 与 `write`
    对调的。解析器必须给出两个**不同**的序列，且都等于字面写的那个。
    """
    good = (
        "```mermaid\n"
        'flowchart LR\n    intake["需求理解"] --> orchestrator["专家调度"]\n'
        '    orchestrator --> collect["联网采集"]\n'
        '    collect --> analyze["分析研判"]\n'
        '    analyze --> audit["质量审计"]\n'
        '    audit --> write["报告撰写"]\n'
        "```\n"
    )
    bad = good.replace('audit["质量审计"]', 'TMP["质量审计"]').replace(
        'write["报告撰写"]', 'audit["报告撰写"]'
    ).replace('TMP["质量审计"]', 'write["质量审计"]')

    assert _mermaid_nodes(good) == [
        "intake", "orchestrator", "collect", "analyze", "audit", "write",
    ]
    # 对调之后两条都变了：说明解析器真的在读内容与顺序，不是在返回常量。
    assert _mermaid_nodes(bad) == [
        "intake", "orchestrator", "collect", "analyze", "write", "audit",
    ]


def test_解析器看得见真文档里的每一个阶段(architecture: str) -> None:
    """节点顺序对，但少画了一个阶段，同样会让读文档的人得出错的结论。

    上面那条 `==` 已经把这一点覆盖了，这里单独再钉一次是为了让报错
    直接说出**缺的是哪个**，而不是只说"两个列表不相等"。
    """
    documented = set(_mermaid_nodes(architecture))
    missing = [key for key in (*STAGE_ORDER, TERMINAL_STAGE) if key not in documented]
    assert not missing, f"架构文档的流水线图里没有画：{missing}"


def test_顺序常量与阶段表同步() -> None:
    """`STAGE_ORDER` 是从 `PIPELINE_STAGES` 推出来的，不该各自维护。"""
    assert tuple(stage.key for stage in PIPELINE_STAGES) == STAGE_ORDER
    assert len(set(STAGE_ORDER)) == len(STAGE_ORDER), "阶段 key 有重复"


def test_终态不在阶段表里() -> None:
    """`done` 不是阶段——它没有耗时、没有进度，不该有权重。"""
    assert TERMINAL_STAGE not in STAGE_ORDER
    assert progress_at(TERMINAL_STAGE) == 1.0


def test_每个阶段的权重为正且进度单调不减() -> None:
    """进度条的"不倒退"是用户能直接看到的一条性质。

    权重出现 0 或负数时，进度会在某两个阶段之间持平或回退，
    表现是"进度条自己往回走了"——那是看起来像坏了的那种 bug。
    """
    for stage in PIPELINE_STAGES:
        assert stage.weight > 0, f"{stage.key} 的权重是 {stage.weight}"

    starts = [progress_at(key) for key in STAGE_ORDER]
    assert starts == sorted(starts), f"进度起点不单调：{starts}"
    assert all(0.0 <= value < 1.0 for value in starts)


def test_最后一个阶段之后是终态() -> None:
    assert next_stage(STAGE_ORDER[-1]) == TERMINAL_STAGE
    assert next_stage(STAGE_ORDER[0]) == STAGE_ORDER[1]


def test_前端拿到的DAG顺序与常量一致() -> None:
    """`GET /api/pipeline/stages` 的 payload 是前端画图的输入，
    它的 `order` 字段必须就是列表下标——前端按它排序，
    如果这里错了，前端会画出另一个顺序，而两边都"看起来是对的"。
    """
    described = describe_stages()
    assert [item["key"] for item in described] == list(STAGE_ORDER)
    assert [item["order"] for item in described] == list(range(len(STAGE_ORDER)))
    assert all("startProgress" in item for item in described)
