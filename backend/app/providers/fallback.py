"""LLM 降级：主 provider 不可用时切到备用。

设计取舍
--------
**降级必须是可观测的。** 静默换一家 provider 会污染全部指标：
cost 的单位变了、模型名变了、输出风格变了，而报告上看不出来。
所以每次降级都记一条 `degradations`，`summary()` 供报告页与 trace 面板披露。

**熔断要区分"会不会自愈"。**
  - `AuthFailed` / `QuotaExhausted` / `ProviderNotConfigured` —— 不会自愈，
    第一次撞上就**永久熔断**，后续调用直接走备用，不再为每次调用白付一次失败往返。
  - `RateLimited` / `Transient` / `MalformedResponse` —— 可能自愈，
    **本次降级但下次仍试主 provider**。否则一次网络抖动会让整份报告都跑在备用模型上。

**流式不做中途降级。** 已经 yield 出去的文本收不回来，切 provider 会产出前后拼接的
半截内容。`chat_stream` 直接委托当前生效的一方，不做兜底。
"""
from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence

from app.providers.base import (
    ChatMessage,
    LLMCapabilities,
    LLMResponse,
    ModelPricing,
    ProbeResult,
    Tier,
)
from app.providers.errors import (
    AuthFailed,
    HarnessError,
    ProviderError,
    ProviderNotConfigured,
    QuotaExhausted,
)

log = logging.getLogger(__name__)

#: 撞上这些错误说明主 provider 不会自己好起来，直接熔断。
_FATAL = (AuthFailed, QuotaExhausted, ProviderNotConfigured)


class FallbackLLM:
    """主备 LLM 包装。对外仍是一个 `LLMProvider`。"""

    def __init__(self, primary, secondary) -> None:
        self.primary = primary
        self.secondary = secondary
        self.name = f"{primary.name}>{secondary.name}"
        #: 能力取主 provider 的。备用可能更强或更弱，但流水线要按"正常情况下"的能力写代码；
        #: 备用能力不足这件事由降级记录披露，而不是让整条流水线按较弱的那个来设计。
        self.capabilities: LLMCapabilities = primary.capabilities

        self._latched = False
        self.degradations: list[dict] = []
        self.fallback_calls = 0

    # ------------------------------------------------------------------

    @property
    def latched(self) -> bool:
        """主 provider 是否已被熔断。"""
        return self._latched

    def resolve_model(self, tier: Tier) -> str:
        if self._latched:
            return self.secondary.resolve_model(tier)
        return self.primary.resolve_model(tier)

    def pricing(self) -> Mapping[str, ModelPricing]:
        """合并两家的定价表。

        成本本身在各自的适配器内部按"实际返回的模型名"算好了，这里合并只是为了
        让 `/api/providers` 能把两家的单价一起展示出来。
        同名模型以主 provider 为准——跨 provider 的同名模型极罕见，
        真出现了，备用那家的单价会以 `provider:model` 的形式保留，不会丢。
        """
        merged: dict[str, ModelPricing] = dict(self.primary.pricing())
        for model, price in self.secondary.pricing().items():
            if model in merged:
                merged.setdefault(f"{self.secondary.name}:{model}", price)
            else:
                merged[model] = price
        return merged

    def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        tier: Tier = "aux",
        temperature: float = 0.6,
        max_tokens: int = 2048,
        json_mode: bool = False,
        purpose: str = "",
        evidence_ids: Sequence[str] | None = None,
    ) -> LLMResponse:
        kwargs = {
            "tier": tier,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "json_mode": json_mode,
            "purpose": purpose,
            "evidence_ids": evidence_ids,
        }

        if not self._latched:
            try:
                return self.primary.chat(messages, **kwargs)
            except HarnessError:
                # 夹具的问题不是"主 provider 挂了"，**不能因此换一家**。
                # 回放时主 provider 少一条录制就去打备选，会静默把这次评测
                # 换成另一个模型跑出来的结果——两份数字放一起比较，
                # 而没有任何地方说明它们不是同一个模型产的。
                raise
            except ProviderError as exc:
                self._record(exc, purpose, tier)

        self.fallback_calls += 1
        return self.secondary.chat(messages, **kwargs)

    def chat_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tier: Tier = "aux",
        temperature: float = 0.6,
        max_tokens: int = 2048,
    ) -> Iterator[str]:
        target = self.secondary if self._latched else self.primary
        return target.chat_stream(
            messages, tier=tier, temperature=temperature, max_tokens=max_tokens
        )

    # ------------------------------------------------------------------

    def probe(self) -> ProbeResult:
        """**两家都探**，然后给一句能直接拿去修的说法。

        只探主 provider 是不够的：配了备用的人真正想知道的是"主挂了的时候
        备用顶不顶得上"。只探主的话，主一红就报红，而那时系统其实是可用的——
        这个探测会天天喊狼来了，然后就没人看它了。

        也就是说 `ok` 的判据是"至少一家能用"，不是"主能用"。
        """
        primary = self.primary.probe()
        if primary.ok:
            return ProbeResult(
                ok=True,
                detail=f"主 {self.primary.name}：{primary.detail}",
                latency_ms=primary.latency_ms,
            )

        secondary = self.secondary.probe()
        if secondary.ok:
            return ProbeResult(
                ok=True,
                detail=(
                    f"主 {self.primary.name} 不可用（{primary.error}：{primary.detail}），"
                    f"备用 {self.secondary.name} 可用"
                ),
                latency_ms=primary.latency_ms + secondary.latency_ms,
            )
        return ProbeResult(
            ok=False,
            detail=(
                f"主 {self.primary.name} 与备用 {self.secondary.name} 都不可用；"
                f"备用报 {secondary.error}：{secondary.detail}"
            ),
            latency_ms=primary.latency_ms + secondary.latency_ms,
            error=secondary.error,
        )

    def _record(self, exc: ProviderError, purpose: str, tier: Tier) -> None:
        fatal = isinstance(exc, _FATAL)
        entry = {
            "purpose": purpose,
            "tier": tier,
            "from": self.primary.name,
            "to": self.secondary.name,
            "error": type(exc).__name__,
            "message": str(exc),
            "latched": fatal,
        }
        self.degradations.append(entry)
        if fatal:
            self._latched = True
            log.error(
                "主 LLM provider %s 熔断（%s），后续全部调用走 %s：%s",
                self.primary.name,
                type(exc).__name__,
                self.secondary.name,
                exc,
            )
        else:
            log.warning(
                "主 LLM provider %s 本次失败（%s），本次改走 %s：%s",
                self.primary.name,
                type(exc).__name__,
                self.secondary.name,
                exc,
            )

    def summary(self) -> dict:
        """给报告页与 trace 面板用的降级披露。

        `degraded=False` 时前端不显示横幅——不能因为"配了备用"就总挂个警告。
        """
        return {
            "primary": self.primary.name,
            "secondary": self.secondary.name,
            "latched": self._latched,
            "fallbackCalls": self.fallback_calls,
            "degraded": bool(self.degradations),
            "events": list(self.degradations),
        }
