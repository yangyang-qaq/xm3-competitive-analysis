"""DeepSeek provider。

模型名为什么不在代码里写默认值
------------------------------
因为模型 ID 是**账号相关**的：不同区域、不同开通时间的账号，可用模型列表并不一样
（公开渠道有 deepseek-chat / deepseek-reasoner，但企业账号往往还有别的）。
把某个名字写死成默认值，等于让这份代码对别人是错的，而且错得很隐蔽——
表现为"调用报模型不存在"，排查时很难想到是默认值的问题。

所以这里 `default_models` 留空，由 `.env` 的 `DEEPSEEK_MODEL_{CORE,AUX,FAST}` 指定。
没配时会在 `resolve_model()` 抛出一个明确说明该配哪一项的错误。
自己账号下有哪些模型可以用 `GET {base_url}/models` 查。

定价为什么按"当前时段"给
------------------------
这家是**分时段计价**的：高峰价是空闲价的 2 倍，而两条分界线是按小时划的。
一次调研任务里输入 token 占八成以上，时段判错会让成本整整偏一倍——
比"忽略缓存命中价"造成的偏差还大，所以不能像别家那样填一张固定价目表。
判断放在 `_pricing_table()` 里，价目表本身仍然只是一张静态表。

缓存命中价也必须算：命中与未命中的输入单价差 **50 倍**，而 API 会把命中数
（`prompt_cache_hit_tokens`）报回来。

实测这个流水线的命中率只有 **0.8%**——一次 quick 模式运行 13 次调用里，
只有最先的 `scope` / `plan` 两次命中（48% / 72%），其余全是 0。原因是
各章节**并行**扇出，每个章节都是自己那份前缀的第一个写者。所以在这个负载上，
按未命中价一刀切带来的偏差不到 1%。

之所以仍然分开计，是因为**在测出来之前不知道是 0.8% 还是 80%**：价差是 50 倍，
而命中率由提示词拼法与调用编排决定，是个会随重构变的工程变量。
把命中数取回来，这个数就从猜测变成了一个可观测的指标。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, time, timedelta, timezone
from typing import Any

from app.providers.base import LLMCapabilities, ModelPricing
from app.providers.llm.openai_compat import OpenAICompatProvider
from app.providers.registry import register_llm

log = logging.getLogger(__name__)

#: 北京时间。分时段计价按**服务方的本地时间**算，不是本机时间——
#: 拿本机时间判断的话，一个把笔记本时区设成 UTC 的人会在每天最贵的四小时里
#: 拿到空闲价，成本表系统性偏低。
_CST = timezone(timedelta(hours=8))

#: 高峰时段（北京时间）。公布口径是"周一至周五 9:00-12:00 与 14:00-18:00"，
#: 其余时间含周末与节假日全天为空闲。这里按周几 + 两个区间判断。
#: [9, 12) 而不是 [9, 12]：区间端点归属含糊，而两倍价差经不起含糊。
_PEAK_WINDOWS: tuple[tuple[time, time], ...] = (
    (time(9, 0), time(12, 0)),
    (time(14, 0), time(18, 0)),
)

#: 单价，单位**元/百万 token**，2026-09-10 12:00 起生效。
#: 顺序是 `(缓存命中输入, 缓存未命中输入, 输出)`。
#: 来源：DeepSeek 开放平台 2026-09-09 的调价公告（flash 系列，含 vision-exp）。
_IDLE_PRICES_CNY: tuple[float, float, float] = (0.02, 1.0, 4.0)
_PEAK_PRICES_CNY: tuple[float, float, float] = (0.04, 2.0, 8.0)

#: 人民币兑美元汇率：**买入 1 美元需要多少元**。取 2026-09-15 中国外汇交易中心
#: 公布的中间价 6.7670。这是一个**有日期的**事实，不是估算——所以它写在这里
#: 而注释里带着日期与出处。汇率会漂，介意的话用 `DEEPSEEK_FX_CNY_PER_USD` 覆盖。
#:
#: 为什么不像定价那样干脆进环境变量：定价是**每一家各不相同**的，而汇率对所有
#: 以人民币计价的 provider 是同一个数，它属于"这家 provider 的常识"。
_DEFAULT_FX_CNY_PER_USD = 6.7670


def _fx_rate() -> float:
    """读汇率，非法值直接退回默认值并留一条日志。

    这里不抛异常：一个打错的汇率不该让整条流水线跑不出报告。
    但也不能静默接受——偏差 10% 的成本表和编的没区别。
    """
    raw = os.environ.get("DEEPSEEK_FX_CNY_PER_USD", "").strip()
    if not raw:
        return _DEFAULT_FX_CNY_PER_USD
    try:
        rate = float(raw)
    except ValueError:
        log.warning("DEEPSEEK_FX_CNY_PER_USD 不是数字，改用默认汇率 %s", _DEFAULT_FX_CNY_PER_USD)
        return _DEFAULT_FX_CNY_PER_USD
    if rate <= 0:
        log.warning("DEEPSEEK_FX_CNY_PER_USD 应为正数，改用默认汇率 %s", _DEFAULT_FX_CNY_PER_USD)
        return _DEFAULT_FX_CNY_PER_USD
    return rate


def _is_peak(now: datetime) -> bool:
    """给定时刻是否在高峰计价时段内。`now` 必须是**带时区**的时刻。

    带参数而不是自己取 `datetime.now()`，是为了让"周三上午十点"这种判断
    能被直接测到——分时段的逻辑没法靠调系统时间去验证。
    """
    local = now.astimezone(_CST)
    if local.weekday() >= 5:  # 周六、周日
        return False
    return any(start <= local.time() < end for start, end in _PEAK_WINDOWS)


@register_llm("deepseek")
class DeepSeekProvider(OpenAICompatProvider):
    name = "deepseek"
    display_name = "DeepSeek"

    capabilities = LLMCapabilities(
        json_mode=True,
        json_schema_mode=False,
        thinking_toggle=True,
        streaming=True,
        max_context_tokens=128_000,
        max_output_tokens=8_192,
    )

    default_base_url = "https://api.deepseek.com"
    default_models: dict[str, str] = {}

    #: 这些模型永远在思考、且不接受 thinking 参数，传了会直接报错。
    #: reasoner 类模型要排除，否则每次调用都失败。
    _THINKING_EXEMPT = ("reasoner",)

    def _extra_body(self, model: str) -> dict | None:
        """关闭思考模式。

        实测：不传这个参数时思考是**默认开启**的，同一句简单提问的 completion
        从 46 tokens 涨到 149。调研流水线一次任务要打几十次调用，这个开销乘起来很可观，
        而且思考过程对结构化输出没有帮助。

        反过来，需要深度推理的少数环节（如矛盾检测）可以显式传 `thinking_toggle` 覆盖。
        """
        if any(marker in model.lower() for marker in self._THINKING_EXEMPT):
            return None
        return {"thinking": {"type": "disabled"}}

    def _cached_prompt_tokens(self, raw: Any) -> int:
        """DeepSeek 把命中数报在 `usage.prompt_cache_hit_tokens`（顶层字段）。

        与 OpenAI 方言的 `prompt_tokens_details.cached_tokens` 不是同一个位置，
        所以覆写基类的钩子。**实测过这个字段确实会回来**，并且第二次发同一段
        前缀时它变成非零——不是根据文档猜的。取不到就是 0，成本按未命中算。
        """
        return int(getattr(raw, "prompt_cache_hit_tokens", 0) or 0)

    def _pricing_table(self) -> dict[str, ModelPricing]:
        """按**当前时段**选一组价，折算成美元。

        时段判断放在这里而不是 `ModelPricing` 里：那张表是静态价目表，
        而"现在是高峰还是空闲"是构造它的这一瞬间才知道的事。

        代价要说清楚：`pricing()` 是在响应**返回之后**被调用的，所以用的是
        "调用结束的时刻"。与调用开始的时刻最坏差一次调用的时长（实测 p100 约
        两分钟），而高峰边界是按小时划的，跨边界的调用极少。这个近似在
        成本量级上可以忽略，但不能不写出来。

        哪些模型适用这份价，见 `_priced_models()`：只给 flash 系列。
        其它系列价格完全不同，宁可不给价也不套用。
        """
        fx = _fx_rate()
        peak = _is_peak(datetime.now(_CST))
        hit, miss, out = _PEAK_PRICES_CNY if peak else _IDLE_PRICES_CNY
        tier_note = "高峰" if peak else "空闲"

        table: dict[str, ModelPricing] = {}
        for model in self._priced_models():
            table[model] = ModelPricing(
                input_per_mtok_usd=miss / fx,
                output_per_mtok_usd=out / fx,
                cached_input_per_mtok_usd=hit / fx,
                source_currency="CNY",
                source_price_per_mtok=(miss, out),
                note=(
                    f"{tier_note}时段 {miss}/{out} 元每百万 token（缓存命中 {hit}），"
                    f"按 {fx:.4f} 折算，2026-09-10 起生效"
                ),
            )
        return table

    #: flash 系列的识别标记：模型名里含它就算这个系列。
    #:
    #: **为什么是匹配而不是列名单**：这份价目表是 flash 系列的，而模型 ID 是
    #: 账号相关的（见模块 docstring）——本机账号配的就是 `deepseek-flash`，
    #: 它不在公开渠道的那两个名字里。写死名单的话，唯一真在用的那个模型
    #: 会没有价、成本记 0，等于整张成本表白做。
    #:
    #: **已知代价**：将来若出现名字含 flash 但定价不同的变体（比如 flash-lite），
    #: 它会被按 flash 计费。选这个代价是因为另一侧更糟——漏价是静默的成本 0，
    #: 而 0 在成本表上看起来像"免费"，不会有人去查；而系列名与价目表挂钩
    #: 是这家自己的命名约定，不是我们猜的。
    _FLASH_MARKER = "flash"

    def _priced_models(self) -> tuple[str, ...]:
        """要对哪些模型报价：三档配置里出现的、且属于 flash 系列的那些。

        只给 flash 系列报价。其它系列（reasoner 等）价格完全不同，
        套用 flash 价会让成本**偏低**——而偏低的成本不会有人去复查。
        宁可不给价（成本记 0 并显示"未配置定价"），也不要给一个错的价。
        """
        names = {
            self._cred.model_for(tier, self.default_models.get(tier, ""))
            for tier in ("core", "aux", "fast")
        }
        return tuple(sorted(n for n in names if n and self._FLASH_MARKER in n.lower()))
