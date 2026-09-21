"""OpenAI 兼容协议的 LLM 基类。

相当多的模型服务都提供 OpenAI 兼容端点（DeepSeek、智谱、Moonshot、通义……），
它们的差异只在：base_url、模型名、是否支持某些可选参数、以及错误码。
把公共部分收在这里，接入一家新 provider 就只剩下一二十行。

三处刻意的选择
--------------
1. **SDK 自带的重试关掉**（`max_retries=0`），重试全部走 `retry.with_backoff`。
   否则会有两层重试互相叠加，实际等待时间变成两者之积，而日志里只看得到一层。

2. **错误先映射再重试**。SDK 的异常在 `_call` 里就翻译成 `ProviderError`，
   重试策略据此判断 `retryable`——这样"什么算限流"是每个适配器自己的知识，
   而"限流了怎么办"只有一份实现。

3. **成本按返回的真实模型名查表**。不能假设"请求了 core 档就一定用了 core 的模型"——
   有些服务会在负载高时把请求路由到别的模型，那时按请求档位算成本就是错的。
"""
from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import openai
from openai import OpenAI

from app.core.config import Settings
from app.providers import retry as retry_mod
from app.providers.base import (
    ChatMessage,
    LLMCapabilities,
    LLMResponse,
    ModelPricing,
    ProbeResult,
    Tier,
    TokenUsage,
    apply_pricing,
    run_probe,
)
from app.providers.errors import (
    AuthFailed,
    BadRequest,
    NotFound,
    ProviderError,
    ProviderNotConfigured,
    RateLimited,
    Transient,
    classify_status,
    parse_retry_after,
)

#: 探测时尝试档位的顺序：**从便宜到贵**。第一个配了模型的档位就用来探测。
_PROBE_TIERS: tuple[Tier, ...] = ("fast", "aux", "core")


class OpenAICompatProvider:
    """实现 LLMProvider 协议的基类。子类至少要覆写 `name` / `default_base_url` / `default_models`。"""

    name: str = ""
    display_name: str = ""
    capabilities: LLMCapabilities = LLMCapabilities()
    default_base_url: str = ""
    #: 档位兜底模型名。`.env` 里没配 <PROVIDER>_MODEL_<TIER> 时用这里的值。
    default_models: Mapping[str, str] = {}
    #: 重试策略。慢而稳是调研流水线的取向。
    retry_policy: retry_mod.RetryPolicy = retry_mod.DEFAULT

    def __init__(self, settings: Settings, *, http_client: Any = None) -> None:
        self._settings = settings
        self._cred = settings.credentials(self.name)
        if not self._cred.configured:
            raise ProviderNotConfigured(
                f"未配置 {self.name.upper()}_API_KEY", provider=self.name
            )

        self._client = OpenAI(
            api_key=self._cred.api_key,
            base_url=self._cred.base_url or self.default_base_url,
            timeout=settings.llm_timeout,
            # 重试由 retry.with_backoff 统一负责，见模块 docstring
            max_retries=0,
            http_client=http_client,
        )

    # ------------------------------------------------------------------
    # 交给子类定制的部分
    # ------------------------------------------------------------------

    def _extra_body(self, model: str) -> dict | None:
        """额外请求体参数。默认无。

        典型用途是关闭思考模式——它在调研流水线里既慢又费 token。
        """
        return None

    def _pricing_table(self) -> Mapping[str, ModelPricing]:
        """模型名 → 定价。默认空，此时成本记为 0 而不是估算值。"""
        return {}

    # ------------------------------------------------------------------
    # LLMProvider
    # ------------------------------------------------------------------

    def resolve_model(self, tier: Tier) -> str:
        default = self.default_models.get(tier, "")
        model = self._cred.model_for(tier, default)
        if not model:
            raise ProviderError(
                f"{self.name} 的 {tier} 档没有可用模型：请在 .env 配置 "
                f"{self.name.upper()}_MODEL_{tier.upper()}",
                provider=self.name,
            )
        return model

    def pricing(self) -> Mapping[str, ModelPricing]:
        """定价表。环境变量里的配置覆盖代码内置的默认值——
        定价会变，允许不改代码就更新。

        **整条覆盖，不做字段级合并**：只覆盖输入价而留着内置的缓存价，
        会得到一张从未在任何地方公布过的价目表。要么整行用你的，要么整行用内置的。
        """
        merged = dict(self._pricing_table())
        for model, (inp, out, cached) in self._settings.pricing_for(self.name).items():
            merged[model] = ModelPricing(
                input_per_mtok_usd=inp,
                output_per_mtok_usd=out,
                cached_input_per_mtok_usd=cached,
                note=f"来自 {self.name.upper()}_PRICING",
            )
        return merged

    def probe(self) -> ProbeResult:
        """发一句最短的话、只要 1 个 token。

        走 `fast` 档而不是 `core`：探测会按定价计费，而它想验证的是
        "密钥有效、端点可达、模型名认得"，这些与档位无关。用最便宜的那个档。

        `fast` 没配就依次退到 `aux` / `core`。一个只配了 core 的账号是能用的，
        不该因为没配杂务档就被探成红的。三个档全空才是真的不可用。
        """
        for tier in _PROBE_TIERS:
            model = self._cred.model_for(tier, self.default_models.get(tier, ""))
            if model:
                break
        else:
            return ProbeResult(
                ok=False,
                detail=f"没有任何档位配置了模型（试过 {'/'.join(_PROBE_TIERS)}）",
                error="ProviderNotConfigured",
            )

        def call() -> str:
            resp = self.chat(
                [ChatMessage(role="user", content="ping")],
                tier=tier,
                temperature=0.0,
                max_tokens=1,
                purpose="probe",
            )
            return f"{resp.model} 应答正常（{resp.latency_ms} ms）"

        return run_probe(call)

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
        _ = (purpose, evidence_ids)  # trace 层通过 contextvars 采集，这里无需处理
        model = self.resolve_model(tier)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode and self.capabilities.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        extra = self._extra_body(model)
        if extra:
            kwargs["extra_body"] = extra

        started = time.perf_counter()
        completion = retry_mod.with_backoff(
            lambda: self._call(kwargs),
            policy=self.retry_policy,
            provider=self.name,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)

        text = self._extract_text(completion)
        usage = self._extract_usage(completion, model)
        # finish_reason 只用于展示，响应形状异常时留空即可，不值得为它让整次调用失败
        finish_reason = ""
        with contextlib.suppress(AttributeError, IndexError):
            finish_reason = completion.choices[0].finish_reason or ""

        return LLMResponse(
            text=text,
            model=model,
            provider=self.name,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
        )

    def chat_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tier: Tier = "aux",
        temperature: float = 0.6,
        max_tokens: int = 2048,
    ) -> Iterator[str]:
        model = self.resolve_model(tier)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        extra = self._extra_body(model)
        if extra:
            kwargs["extra_body"] = extra

        try:
            stream = self._client.chat.completions.create(**kwargs)
            for chunk in stream:
                delta = getattr(chunk.choices[0].delta, "content", None)
                if delta:
                    yield delta
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _call(self, kwargs: dict) -> Any:
        """发一次请求，把 SDK 异常翻译成类型化错误。

        翻译必须发生在这里而不是外层：`with_backoff` 要靠 `retryable` 决定是否重试。
        """
        try:
            return self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc

    def _map_error(self, exc: Exception) -> ProviderError:
        """OpenAI SDK 异常 → ProviderError。

        子类可覆写以处理自家特有的错误码——尤其是那些 HTTP 200 但业务码报错的情况。
        """
        provider = self.name

        if isinstance(exc, openai.RateLimitError):
            retry_after = None
            response = getattr(exc, "response", None)
            if response is not None:
                retry_after = parse_retry_after(response.headers.get("retry-after"))
            return RateLimited(
                "触发限流", provider=provider, detail=str(exc), retry_after=retry_after
            )
        if isinstance(exc, openai.AuthenticationError):
            return AuthFailed("密钥无效", provider=provider, detail=str(exc))
        if isinstance(exc, openai.PermissionDeniedError):
            return AuthFailed("无访问权限", provider=provider, detail=str(exc))
        if isinstance(exc, openai.NotFoundError):
            return NotFound("模型或端点不存在", provider=provider, detail=str(exc))
        if isinstance(exc, openai.BadRequestError):
            return BadRequest("请求参数不合法", provider=provider, detail=str(exc))
        if isinstance(exc, openai.APITimeoutError):
            return Transient("请求超时", provider=provider, detail=str(exc))
        if isinstance(exc, openai.APIConnectionError):
            return Transient("连接失败", provider=provider, detail=str(exc))
        if isinstance(exc, openai.APIStatusError):
            status = getattr(exc, "status_code", 0)
            return classify_status(status, provider=provider, detail=str(exc))
        if isinstance(exc, openai.OpenAIError):
            return ProviderError("调用失败", provider=provider, detail=str(exc))
        return ProviderError(f"未预期的异常：{exc!r}", provider=provider)

    def _extract_text(self, completion: Any) -> str:
        try:
            content = completion.choices[0].message.content or ""
        except (AttributeError, IndexError) as exc:
            raise ProviderError("响应缺少 choices", provider=self.name) from exc
        return self._strip_think(content)

    @staticmethod
    def _strip_think(text: str) -> str:
        """剥离内联输出的 <think> 块。

        有些模型即使关掉了思考开关，也会把推理过程以标签形式写进正文；
        不剥离的话它会污染 JSON 解析和报告正文。
        """
        import re

        if not text or "<think>" not in text:
            return text
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
        if "<think>" in cleaned:  # 被截断的未闭合块
            cleaned = cleaned.split("<think>")[0]
        return cleaned.strip()

    def _cached_prompt_tokens(self, raw: Any) -> int:
        """从响应里取"命中前缀缓存的输入 token 数"。

        默认读 OpenAI 方言的 `usage.prompt_tokens_details.cached_tokens`。
        **各家报这个数的地方不一样**，所以这是个可覆写的钩子而不是写死在这里——
        把每家自己的字段名留在基类，正是这一层要防的事（见模块 docstring）。
        读不到时返回 0：未知的命中数按未命中计价，成本偏高而不是偏低。
        """
        details = getattr(raw, "prompt_tokens_details", None)
        if details is None:
            return 0
        return int(getattr(details, "cached_tokens", 0) or 0)

    def _extract_usage(self, completion: Any, model: str) -> TokenUsage:
        raw = getattr(completion, "usage", None)
        if raw is None:
            return TokenUsage()
        usage = TokenUsage(
            prompt_tokens=int(getattr(raw, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(raw, "completion_tokens", 0) or 0),
            total_tokens=int(getattr(raw, "total_tokens", 0) or 0),
            cached_prompt_tokens=self._cached_prompt_tokens(raw),
        )
        return apply_pricing(usage, self.pricing().get(model))
