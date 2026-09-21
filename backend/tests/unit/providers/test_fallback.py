"""降级与熔断。

核心断言是**什么时候不再试主 provider**——这是"熔断"与"每次重试一遍"的全部区别。
"""
from __future__ import annotations

import pytest

from app.providers.base import (
    ChatMessage,
    LLMCapabilities,
    LLMResponse,
    ModelPricing,
    TokenUsage,
)
from app.providers.errors import AuthFailed, ProviderError, QuotaExhausted, Transient
from app.providers.fallback import FallbackLLM

_MESSAGES = [ChatMessage("user", "hi")]


class _FakeLLM:
    """可控成功/失败的假 provider。"""

    capabilities = LLMCapabilities(max_context_tokens=64_000)

    def __init__(self, name: str, error: Exception | None = None) -> None:
        self.name = name
        self._error = error
        self.calls = 0
        self.stream_calls = 0

    def resolve_model(self, tier: str) -> str:
        return f"{self.name}-{tier}"

    def pricing(self) -> dict[str, ModelPricing]:
        return {f"{self.name}-core": ModelPricing(1.0, 2.0)}

    def chat(self, messages, **kwargs) -> LLMResponse:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return LLMResponse(
            text=f"{self.name} 的回复",
            model=f"{self.name}-aux",
            provider=self.name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1,
        )

    def chat_stream(self, messages, **kwargs):
        self.stream_calls += 1
        yield self.name


def _pair(primary_error: Exception | None = None) -> tuple[FallbackLLM, _FakeLLM, _FakeLLM]:
    primary = _FakeLLM("primary", primary_error)
    secondary = _FakeLLM("secondary")
    return FallbackLLM(primary=primary, secondary=secondary), primary, secondary


# ============================================================
# 正常路径
# ============================================================


def test_healthy_primary_never_touches_the_secondary():
    fallback, primary, secondary = _pair()
    response = fallback.chat(_MESSAGES)

    assert response.provider == "primary"
    assert primary.calls == 1
    assert secondary.calls == 0
    assert fallback.degradations == []
    assert fallback.summary()["degraded"] is False


# ============================================================
# 熔断：不会自愈的错误
# ============================================================


@pytest.mark.parametrize("error", [AuthFailed("密钥无效"), QuotaExhausted("额度用尽")])
def test_fatal_errors_latch_permanently(error):
    """认证失败和额度用尽不会自己好起来。

    不熔断的话，流水线里几十次调用每一次都要先撞一次失败再降级——
    白花一倍的失败往返，还让日志被同样的错误刷屏。
    """
    fallback, primary, secondary = _pair(error)

    assert fallback.chat(_MESSAGES).provider == "secondary"
    assert primary.calls == 1  # 只撞了一次
    assert fallback.latched is True

    assert fallback.chat(_MESSAGES).provider == "secondary"
    assert fallback.chat(_MESSAGES).provider == "secondary"
    assert primary.calls == 1, "熔断后不该再试主 provider"
    assert secondary.calls == 3


def test_successful_fallback_is_disclosed_not_hidden():
    """降级必须可观测。静默换 provider 会污染成本、模型名与输出风格，
    而报告上看不出来——这类污染比直接报错更糟。"""
    fallback, _, _ = _pair(AuthFailed("密钥无效"))
    fallback.chat(_MESSAGES, purpose="analyze_claims", tier="core")

    summary = fallback.summary()
    assert summary["degraded"] is True
    assert summary["latched"] is True
    assert summary["primary"] == "primary"
    assert summary["secondary"] == "secondary"
    assert summary["fallbackCalls"] == 1

    event = summary["events"][0]
    assert event["error"] == "AuthFailed"
    assert event["purpose"] == "analyze_claims"
    assert event["tier"] == "core"


# ============================================================
# 不熔断：可能自愈的错误
# ============================================================


@pytest.mark.parametrize("error", [Transient("网络抖动"), ProviderError("未知")])
def test_transient_errors_do_not_latch(error):
    """一次网络抖动不该让整份报告都跑在备用模型上。下次仍旧先试主 provider。"""
    fallback, primary, secondary = _pair(error)

    assert fallback.chat(_MESSAGES).provider == "secondary"
    assert fallback.latched is False

    assert fallback.chat(_MESSAGES).provider == "secondary"
    assert primary.calls == 2, "未熔断时应继续尝试主 provider"
    assert secondary.calls == 2


def test_recovery_after_a_transient_failure():
    """主 provider 恢复后应当自动回到主 provider，而不是永久停在备用上。"""
    primary = _FakeLLM("primary", Transient("抖动"))
    secondary = _FakeLLM("secondary")
    fallback = FallbackLLM(primary=primary, secondary=secondary)

    assert fallback.chat(_MESSAGES).provider == "secondary"

    primary._error = None  # 恢复了
    assert fallback.chat(_MESSAGES).provider == "primary"
    assert fallback.latched is False


# ============================================================
# 边界
# ============================================================


def test_secondary_failure_propagates():
    """两家都挂了就如实抛出去。吞掉它只会让任务无声无息地卡住。"""
    primary = _FakeLLM("primary", AuthFailed("主挂了"))
    secondary = _FakeLLM("secondary", AuthFailed("备也挂了"))
    fallback = FallbackLLM(primary=primary, secondary=secondary)

    with pytest.raises(AuthFailed):
        fallback.chat(_MESSAGES)


def test_streaming_never_switches_midway():
    """已经 yield 出去的文本收不回来，中途换 provider 会产出前后拼接的半截内容。"""
    fallback, primary, secondary = _pair(AuthFailed("密钥无效"))

    assert list(fallback.chat_stream(_MESSAGES)) == ["primary"]
    fallback.chat(_MESSAGES)  # 触发熔断
    assert list(fallback.chat_stream(_MESSAGES)) == ["secondary"]


def test_capabilities_follow_the_primary():
    """流水线按"正常情况下"的能力写代码。

    备用能力更弱这件事由降级记录披露，而不是让整条流水线按较弱的那个来设计——
    那等于为了罕见的降级场景牺牲了正常路径。
    """
    fallback, _, _ = _pair()
    assert fallback.capabilities.max_context_tokens == 64_000


def test_pricing_merges_both_tables():
    fallback, _, _ = _pair()
    assert set(fallback.pricing()) == {"primary-core", "secondary-core"}


def test_pricing_keeps_same_named_models_from_both_sides():
    """同名模型极罕见，但不能因为合并就把备用那家的单价丢掉。"""

    class _SameName(_FakeLLM):
        def pricing(self):
            return {"shared-model": ModelPricing(9.0, 9.0)}

    fallback = FallbackLLM(primary=_SameName("p"), secondary=_SameName("s"))
    table = fallback.pricing()
    assert table["shared-model"].input_per_mtok_usd == 9.0
    assert "s:shared-model" in table


def test_name_shows_both_providers():
    fallback, _, _ = _pair()
    assert fallback.name == "primary>secondary"


def test_resolve_model_follows_the_active_provider():
    fallback, _, _ = _pair(AuthFailed("密钥无效"))
    assert fallback.resolve_model("core") == "primary-core"
    fallback.chat(_MESSAGES)  # 熔断
    assert fallback.resolve_model("core") == "secondary-core"
