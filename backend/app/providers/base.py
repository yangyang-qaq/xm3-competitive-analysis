"""Provider 归一化接口。

为什么要有这一层
----------------
LLM 与搜索服务各有各的方言：博查用 `include` + `oneMonth` 表达"限定站点、近一月"，
Tavily 用 `include_domains` + `days`，Serper 用 `site:` + `tbs`；模型档位更是每家一套命名。

如果让这些方言漏进业务代码，会出现两个后果：

1. **换 provider 要改流水线。** 参考实现里，编排层直接读 `settings.zhipu_model_core`、
   直接传 `freshness="oneYear"`——"换个模型"这件事因此变成了一次跨模块重构。
2. **能力差异变成静默失败。** A 家支持站点过滤、B 家不支持。若业务代码假设它总是可用，
   换到 B 家后搜索结果会悄悄变差，而不是明确报错。

所以这里的做法是：**只定义归一化的语义，方言全部下沉到适配器**。

    # 采集阶段只表达意图
    search(SearchQuery(text=q, sites=("douyin.com",), freshness="month"))

它变成哪家的什么参数，是适配器的私事。流水线里不会出现任何厂商名。

配套的两个机制
--------------
**能力协商**：`capabilities` 声明这家 provider 支持什么。`site_filter=False` 时，
调用方退化成"把站点名拼进 query"——这是显式、可测的策略，而不是碰运气。

**档位映射归 provider 管**：`chat(..., tier="core")` 由 provider 的 `resolve_model()`
翻译成自己的模型名。新增一家 provider = 写一个类，业务代码零改动。

适配器不需要继承任何东西
------------------------
下面用的是 `Protocol` 而非 ABC：结构匹配即可，第三方 provider 不必 import 本模块。
注册走 `registry.py` 的装饰器。
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

# ============================================================
# 归一化枚举
# ============================================================

#: 时效性。这是语义，不是任何一家的参数值。
Freshness = Literal["any", "day", "week", "month", "year"]

#: 模型档位。core 用于核心章节与关键分析，aux 用于常规章节，fast 用于杂务。
Tier = Literal["core", "aux", "fast"]

TIERS: tuple[Tier, ...] = ("core", "aux", "fast")


# ============================================================
# 连通性探测
# ============================================================


#: 抓取探测的默认目标。
#:
#: **不是 `example.com`**，虽然它看起来是最自然的选择（RFC 2606 保留给示例用途、
#: 极稳定、无反爬）。实测它取回来只有 117 字正文，低于抽取出正文的下限
#: `_MIN_CHARS = 200`，于是每次探测都被判成"正文抽取失败、已回退到摘要"——
#: 探测恒红，而故障并不存在。
#:
#: 换成 IANA 这个保留域说明页：同一个机构、同样几十年不变，正文 731 字，
#: 稳定通过抽取。以下都是本机实测值（`HttpFetcher.fetch`）：
#:
#:     https://example.com                              117 字  降级
#:     https://www.iana.org/help/example-domains        731 字  通过
#:     https://httpbin.org/html                        3566 字  通过
#:
#: 没选 httpbin：它是测试服务，可被限流；IANA 是标准机构，长期性更好。
#: 适配器可以覆写（比如某些网络环境下需要换一个可达的目标）。
PROBE_URL = "https://www.iana.org/help/example-domains"

#: 搜索探测用的固定查询词。刻意选一个任何语言的索引里都必然有结果的词——
#: 探测关心的是链路通不通，不是结果好不好。
PROBE_QUERY = "test"


@dataclass(frozen=True)
class ProbeResult:
    """一次连通性探测的结果。"""

    ok: bool
    #: 人读的一句话。成功时说这次探测打到了什么，失败时说为什么。
    detail: str = ""
    latency_ms: int = 0
    #: 异常类名，如 `AuthFailed` / `RateLimited`。空表示没抛异常。
    #: 给机器判断用的是 `ok`，这个字段是给排查用的——"连不上"和"密钥错了"
    #: 都表现为 `ok=False`，但要做的事完全不同。
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "latencyMs": self.latency_ms,
            "error": self.error,
        }


def run_probe(call: Callable[[], str]) -> ProbeResult:
    """跑一次探测调用，把结果或异常整理成 `ProbeResult`。

    各适配器的 `probe()` 长的都是同一个样子（试一次、计时、捕获），
    所以这里收一次。真正因厂商而异的部分——"对这家来说什么算一次廉价调用"——
    留在各自的 `probe()` 里。

    **捕获 `Exception` 而不是只捕获 `ProviderError`**：`ProviderError` 的语义是
    "外部服务出问题了"，而探测要回答的是"现在能不能用"。适配器里一个 `KeyError`
    同样意味着不能用，而且那正是最该被看见的一种失败。这里把它降级成
    `ok=False, error="KeyError"` 展示出来——不是掩盖，一个 500 的健康检查
    比一条写着"KeyError"的结果更没用。
    """
    started = time.perf_counter()
    try:
        detail = call()
    except Exception as exc:  # noqa: BLE001 - 见 docstring
        return ProbeResult(
            ok=False,
            detail=str(exc) or type(exc).__name__,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=type(exc).__name__,
        )
    return ProbeResult(
        ok=True,
        detail=detail,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


# ============================================================
# LLM
# ============================================================


@dataclass(frozen=True)
class ChatMessage:
    """一条对话消息。用具名字段而不是裸 dict，免得 role 拼错只能运行时才发现。"""

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class TokenUsage:
    """token 消耗与折算成本。

    `cost_usd` 是归一化后的美元金额：各家按自己的货币计价（DeepSeek 是人民币），
    在适配层按定价表换算，这样跨 provider 的成本才可比。汇率是定价表里可审计的常量。

    `cached_prompt_tokens` 是 `prompt_tokens` 里**命中前缀缓存**的那一部分，
    它是 `prompt_tokens` 的子集而不是另一个维度的增量。之所以必须单独记，
    是因为命中与未命中的单价能差 50 倍（实测 DeepSeek 空闲时段 0.02 vs 1 元/Mtok）。

    **实测：这个流水线的命中率只有 0.8%**（一次 quick 模式运行，13 次调用里
    只有最先两次命中）。原因是几个章节是**并行**扇出写的——每个章节都是自己那份
    前缀的第一个写者，谁也不等谁，于是谁也命中不了。所以按未命中价一刀切，
    在这份负载上带来的偏差不到 1%，而不是价差暗示的 40%。

    那为什么还要算？因为**在测出来之前没人知道是 0.8% 还是 80%**。价差摆在那里
    （50 倍），而命中率取决于提示词怎么拼、调用怎么排——那是一个会随重构变化的
    工程变量。现在它有三个用途：成本数是可复核的；命中率本身是一个指标
    （涨上去说明前缀复用变好了）；提示词结构一改，这个数会立刻告出代价。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cached_prompt_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            cached_prompt_tokens=self.cached_prompt_tokens + other.cached_prompt_tokens,
        )

    @property
    def cache_hit_rate(self) -> float:
        """命中缓存的输入 token 占比。没有输入 token 时是 0 而不是 0/0。"""
        if not self.prompt_tokens:
            return 0.0
        return min(self.cached_prompt_tokens / self.prompt_tokens, 1.0)

    def as_dict(self) -> dict:
        return {
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "totalTokens": self.total_tokens,
            "costUsd": round(self.cost_usd, 6),
            "cachedPromptTokens": self.cached_prompt_tokens,
            "cacheHitRate": round(self.cache_hit_rate, 4),
        }


@dataclass(frozen=True)
class LLMResponse:
    """一次补全的结果。

    除文本外还带上 model / provider / usage / latency：这些是 trace 与成本指标的原料，
    如果适配器不返回，上层就只能靠全局计数器去猜，并发下必然错乱。
    """

    text: str
    model: str
    provider: str
    usage: TokenUsage
    latency_ms: int
    finish_reason: str = "stop"
    raw: dict | None = None


@dataclass(frozen=True)
class LLMCapabilities:
    """这家 provider 能做什么。

    声明出来是为了让调用方能**协商**而不是**假设**。
    """

    json_mode: bool = False
    """支持原生 JSON 输出约束（response_format）。不支持时上层靠 prompt + 容错解析。"""

    json_schema_mode: bool = False
    """支持严格 Schema 约束输出。"""

    thinking_toggle: bool = False
    """可以显式关闭思考模式。开着会显著增加 token 消耗与延迟——实测同一句话
    completion 从 46 涨到 149 tokens，所以调研流水线里统一关闭。"""

    streaming: bool = True
    max_context_tokens: int = 32_000
    max_output_tokens: int = 8_192


@dataclass(frozen=True)
class ModelPricing:
    """每百万 token 的价格，归一化为美元。

    `cached_input_per_mtok_usd` 为 `None` 表示"这张表没配缓存价"，此时命中部分
    按未命中价计——成本因此是一个**上界**。宁可偏高也不要偏低：偏高会在
    成本表上多看两眼，偏低则会让一份看起来便宜的报告永远不被复查。
    不区分缓存的那几家（mock、智谱）压根不会报出命中 token，走不到这个分支。

    **哨兵是 `None` 而不是 0**：有些服务的缓存命中就是不计费的，此时 0 是一个
    货真价实的单价。用 0 兼作"未配置"的话，"缓存免费"会被当成"没配"而按
    未命中价计费——把一份本该更便宜的成本算贵，且没有任何迹象表明这是配置问题。

    分时段定价（DeepSeek 的高峰价是空闲价的 2 倍）**不在这里建模**：
    这是一张静态价目表，而"现在是哪个时段"是适配器构造它时才知道的事。
    所以由适配器决定填哪一组数——见 `deepseek.py` 的 `_pricing_table()`。
    """

    input_per_mtok_usd: float
    output_per_mtok_usd: float
    cached_input_per_mtok_usd: float | None = None
    source_currency: str = "USD"
    source_price_per_mtok: tuple[float, float] = (0.0, 0.0)
    note: str = ""

    def cost(self, usage: TokenUsage) -> float:
        """按命中/未命中分别计价。

        命中价缺省时，命中部分**回退到未命中价**——即把成本算成上界。
        这是有意选的偏向：报一个明确说明过的上界，好过报一个因为漏配而偏低的数。

        命中数会先夹到 `[0, prompt_tokens]`：它按定义是提示词的子集，
        报大了（适配器解析错、或跨轮次累加时算错）不该让成本变成负数。
        夹紧而不是抛异常——一个坏掉的 usage 字段不该让整份报告跑不出来。
        """
        cached = min(max(usage.cached_prompt_tokens, 0), usage.prompt_tokens)
        miss = usage.prompt_tokens - cached
        cached_rate = (
            self.input_per_mtok_usd
            if self.cached_input_per_mtok_usd is None
            else self.cached_input_per_mtok_usd
        )
        return (
            miss / 1_000_000 * self.input_per_mtok_usd
            + cached / 1_000_000 * cached_rate
            + usage.completion_tokens / 1_000_000 * self.output_per_mtok_usd
        )


@runtime_checkable
class LLMProvider(Protocol):
    """文本补全 provider。"""

    name: str
    capabilities: LLMCapabilities

    def resolve_model(self, tier: Tier) -> str:
        """把归一化档位翻译成本家的模型名。"""
        ...

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
        """同步补全。

        刻意是同步的：流水线用 `asyncio.to_thread` 扇出，好处是同步代码更容易写测试、
        也更容易在适配器内部做阻塞式重试。详见 `core/pipeline/runner.py`。

        `purpose` / `evidence_ids` 是给 trace 用的语义标注（这次调用在做什么决策、
        依据了哪些证据），适配器只需把它们透传给它自己的 span 记录。
        """
        ...

    def chat_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tier: Tier = "aux",
        temperature: float = 0.6,
        max_tokens: int = 2048,
    ) -> Iterator[str]:
        """流式补全，逐块产出文本增量。仅用于需要逐字呈现的场合。"""
        ...

    def pricing(self) -> Mapping[str, ModelPricing]:
        """模型名 → 定价。没有定价信息时返回空映射，此时成本记为 0 而非编造。"""
        ...

    def probe(self) -> ProbeResult:
        """打一次真实但廉价的调用，回答"这家现在能不能用"。

        **为什么探测是 provider 的职责**：只有适配器知道对自家来说什么算"廉价的一次调用"
        ——LLM 是发一句 `max_tokens=1` 的话，搜索是查一个词取一条，
        抓取是取一个固定的小页面。把这些写进一个集中的探测模块，
        就等于把厂商差异重新请回了流水线上层，而这正是这一层要防的事。

        **为什么是必填而不是可选**：可选意味着要有一个 `probeable: false` 的分支，
        前端于是要为它写一段永远不执行的显示逻辑。而"每一次实现都真的能探"
        本来就是事实——差别只在探得贵不贵。
        """
        ...


# ============================================================
# 搜索
# ============================================================


@dataclass(frozen=True)
class SearchQuery:
    """一次搜索请求。字段全是语义，不含任何厂商参数名。"""

    text: str
    limit: int = 10
    sites: tuple[str, ...] = ()
    """限定域名。provider 不支持时由调用方降级处理（见 SearchCapabilities.site_filter）。"""

    freshness: Freshness = "any"
    locale: str = "zh-CN"

    def cache_key(self) -> dict:
        """规范化后的请求指纹，供 cassette 生成稳定的 key。

        必须包含所有会影响结果的字段——漏掉一个就会让回放命中错误的记录。
        """
        return {
            "text": self.text,
            "limit": self.limit,
            "sites": list(self.sites),
            "freshness": self.freshness,
            "locale": self.locale,
        }


@dataclass(frozen=True)
class SearchHit:
    """一条搜索结果。

    `published_at` 与 `site_name` 是可信度评分的输入，所以必须由适配器尽量填上——
    如果适配器不返回，上层就只能拿 URL 去猜发布时间，那是评分失真的常见来源。
    """

    title: str
    url: str
    snippet: str
    site_name: str = ""
    published_at: str = ""
    provider: str = ""
    rank: int = 0


@dataclass(frozen=True)
class SearchCapabilities:
    site_filter: bool = False
    """支持按域名限定。False 时调用方需把站点名并入 query 文本。"""

    freshness_filter: bool = False
    """支持按时间范围过滤。False 时只能靠 query 里的年份词。"""

    long_snippet: bool = False
    """返回长摘要而非短片段。长摘要能减少抓取次数，但对相关性判断的干扰更大。"""

    max_results_per_call: int = 20


def probe_search(provider: SearchProvider) -> ProbeResult:
    """搜索探测的通用实现：查一个固定的词、只取一条。

    **结果条数为 0 也算通过**：一次合法的搜索本来就可能是空结果，把它判成失败
    会让探测在正常账号上间歇性变红。真正要拦的是"密钥不对"与"配额用尽"——
    这两类在各家往往表现为 HTTP 200 + 业务错误码，已由适配器映射成异常，
    所以探测确实捕获得到。

    这条策略对每一家都一样，所以收在这里；各适配器的 `probe()` 只剩一行委托。
    """

    def call() -> str:
        hits = provider.search(SearchQuery(text=PROBE_QUERY, limit=1))
        if not hits:
            return "调用正常，但该查询词返回 0 条结果"
        return f"返回 {len(hits)} 条，首条来自 {hits[0].site_name or hits[0].url}"

    return run_probe(call)


@runtime_checkable
class SearchProvider(Protocol):
    """网页搜索 provider。"""

    name: str
    capabilities: SearchCapabilities

    def search(self, query: SearchQuery) -> Sequence[SearchHit]:
        """执行搜索。

        只负责取回结果，**不做相关性过滤**——那是 `core/evidence/relevance.py` 的职责。
        过滤逻辑放这里的话，换一家搜索源它会跟着消失，而这恰恰是适配层要防的事。
        """
        ...

    def probe(self) -> ProbeResult:
        """查一个固定的词、只取一条，验证密钥与配额是否可用。"""
        ...


# ============================================================
# 抓取
# ============================================================


@dataclass(frozen=True)
class ImageRef:
    url: str
    alt: str = ""
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class FetchedPage:
    """抓取结果。

    `title` 与 `published_at` 显式建模：可信度评分要用它们，
    而把它们挂在对象上隐式传递（参考实现的做法）会在序列化时被丢掉。
    """

    url: str
    final_url: str
    ok: bool
    text: str
    title: str = ""
    published_at: str = ""
    status: int = 0
    images: list[ImageRef] = field(default_factory=list)
    og_image: str = ""
    captured_at: str = ""
    degraded: bool = True
    """true 表示正文抽取失败、只有摘要。降级必须显式，因为它直接影响证据可信度。"""

    error: str = ""


@runtime_checkable
class Fetcher(Protocol):
    """网页正文抓取。"""

    name: str

    def fetch(self, url: str, *, fallback_snippet: str = "") -> FetchedPage:
        """抓取并抽取正文。

        `fallback_snippet` 是搜索结果的摘要：抓取失败时用它兜底，
        此时返回的结果 `degraded=True`，可信度会被相应扣分。
        """
        ...

    def probe(self) -> ProbeResult:
        """取一次 `PROBE_URL`，验证能否连通并抽出正文。

        探测检查的是**抽出的正文非空**而不只是 HTTP 200：抓取器最常见的故障
        不是连不上，而是页面取回来了但正文抽取规则对不上，于是每条证据都
        静默退化成只有摘要。只看状态码的话，这种故障在探测里是绿的。
        """
        ...


# ============================================================
# 工具
# ============================================================


def apply_pricing(usage: TokenUsage, pricing: ModelPricing | None) -> TokenUsage:
    """按定价表算出成本，返回填好 cost_usd 的新 TokenUsage。

    没有定价信息时成本保持 0——**不估算、不填默认值**。
    成本指标宁可缺失也不能是编的，否则整张成本表都不可信。
    """
    if pricing is None:
        return usage
    return TokenUsage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens or usage.prompt_tokens + usage.completion_tokens,
        cost_usd=pricing.cost(usage),
        # 必须原样带过去。漏了的话缓存计数在定价这一步就消失了，
        # 而它的后果不是"少一个展示字段"——`as_dict()` 报出的命中率会恒为 0，
        # 看起来像"一次都没命中过"，于是没人再去查为什么成本降不下来。
        cached_prompt_tokens=usage.cached_prompt_tokens,
    )
