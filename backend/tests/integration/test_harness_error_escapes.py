"""夹具缺失必须穿透降级逻辑，一路抛到调用方。

这条测试守的是**一次真实的假绿**，所以它测的不是一个想法，是一个事故：

    改了专家调度的提示词
      → 那条 dispatch 录制因为 key 变化而失效（key 由请求内容决定）
      → 回放层抛 `CassetteMiss`
      → 那处调用是 `required=False`，异常被降级逻辑吞掉
      → `chat_json` 返回 `{}`
      → `validate_team({})` 把三个角色整类退回默认队伍
      → 报告照常产出，降级横幅只有一句"整类退回默认队伍"
      → **闸门报"12 项契约全绿"**

`should_degrade` 的单元测试守的是规则本身；这里守的是**接线**：
规则写对了但某个 `except` 分支没调用它，上面那串连锁反应会原样发生。
所以这条测试走的是真实的 `chat_json` → provider → 异常 这条路径，
而不是直接调 `should_degrade`。
"""
from __future__ import annotations

import pytest

from app.core.modes import get_mode
from app.core.observability.events import EventJournal
from app.core.observability.trace import Tracer
from app.core.pipeline.calls import chat_json
from app.core.pipeline.context import PipelineContext
from app.providers.base import ModelPricing, ProbeResult
from app.providers.cassette import CassetteMiss
from app.providers.errors import RateLimited


class _AlwaysMisses:
    """一个只会抛 `CassetteMiss` 的 provider。

    不用真的 `ReplayProvider` + 空 cassette：那会把这测试变成
    "回放层能不能抛对异常"的测试，而回放层已经有自己的测试了。
    """

    name = "missing"

    def resolve_model(self, tier: str) -> str:  # pragma: no cover - 接口完整性
        return "missing-model"

    def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        raise CassetteMiss("回放缺失：chat", provider=self.name)

    def pricing(self) -> dict[str, ModelPricing]:  # pragma: no cover
        return {}

    def probe(self) -> ProbeResult:  # pragma: no cover
        raise CassetteMiss("回放缺失：probe", provider=self.name)


class _AlwaysRateLimited(_AlwaysMisses):
    """同样会失败，但失败是**运行时的**：必须有别的行为。"""

    name = "limited"

    def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        raise RateLimited("限流", provider=self.name)


def _ctx(llm) -> PipelineContext:  # noqa: ANN001
    return PipelineContext(
        task_id="TK-harness",
        query="对比 Notion 与 Obsidian",
        mode=get_mode("quick"),
        tracer=Tracer("TK-harness"),
        journal=EventJournal("TK-harness"),
        llm=llm,
        search=None,
        fetcher=None,
    )


def test_可选调用遇到夹具缺失时抛出而不是返回空() -> None:
    """**核心断言。** `required=False` 不能把夹具问题变成"这次没成功"。"""
    with pytest.raises(CassetteMiss):
        chat_json(
            _ctx(_AlwaysMisses()),
            [{"role": "user", "content": "挑一个团队"}],
            purpose="dispatch",
            required=False,
        )


def test_可选调用遇到限流时照常降级() -> None:
    """对侧：这条**必须**继续降级。

    如果这里也抛，生产里一个可选步骤的偶发限流就会毁掉整份报告——
    那是这套 `required` 设计要避免的另一半。两个方向都要钉，
    否则"让夹具错误穿透"很容易被实现成"什么错都穿透"。
    """
    payload, report = chat_json(
        _ctx(_AlwaysRateLimited()),
        [{"role": "user", "content": "挑一个团队"}],
        purpose="dispatch",
        required=False,
    )

    assert payload == {}
    assert any("dispatch" in note for note in report.repairs), (
        "降级必须留痕——报告里那句话是这块内容没产出时唯一的解释"
    )


def test_必需调用遇到夹具缺失同样抛出() -> None:
    """`required=True` 本来就抛，这里钉住它没被上面的改动带坏。"""
    with pytest.raises(CassetteMiss):
        chat_json(
            _ctx(_AlwaysMisses()),
            [{"role": "user", "content": "x"}],
            purpose="plan",
            required=True,
        )


def test_降级时用到的_token_统计不会被夹具错误污染() -> None:
    """降级路径要留痕，而且不能把这次失败算成一次成功的调用。

    这条是顺手守的：`record_llm_usage` 在 `try` 之后，异常路径不该走到它。
    """
    from app.core.pipeline.calls import stats_of

    ctx = _ctx(_AlwaysRateLimited())
    chat_json(ctx, [{"role": "user", "content": "x"}], purpose="dispatch", required=False)

    stats = stats_of(ctx)
    assert stats.llm_calls == 1
    assert stats.llm_optional_failures == 1
    assert stats.llm_errors == 1
