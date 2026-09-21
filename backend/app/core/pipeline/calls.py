"""带埋点的 provider 调用收口。

**流水线里的每一次 LLM / 搜索 / 抓取调用都必须走这里**，没有例外。

这不是风格要求，是铁律四（全程可观测）的实现方式。如果允许阶段代码
直接调 `ctx.llm.chat(...)`，那么"每次调用都有埋点"就变成了一个
靠自觉维持的约定——而自觉在赶进度的时候第一个失效。收口之后，
漏埋点在结构上不可能：那段代码只能通过这里碰到 provider。

顺带解决第二个问题：LLM 调用的失败处理。参考实现里，一次调用失败
要么整个任务崩，要么被 `except Exception: return {}` 静默吞掉，
后者会让报告带着空块渲染出来。这里的策略是显式的 `required` 参数——
必需调用失败就抛，可选调用失败就记进 `CoercionReport` 并返回空值，
**降级路径一定留痕**。

能力协商在这里
--------------
`adapt_query` 是"provider 不支持站点过滤时怎么办"的唯一实现处。
参考实现把类似的兜底硬编码在舆情循环里，于是换成别的搜索源时
那段代码还在，但它兜的东西已经不存在了。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.core.observability.trace import record_llm_usage, span
from app.core.pipeline.context import PipelineContext
from app.core.schemas.base import CoercionReport, parse_json_object
from app.providers.base import SearchHit, SearchQuery
from app.providers.errors import ProviderError, should_degrade

#: 搜索并发。默认 5：搜索接口通常对并发更敏感，而这里的瓶颈
#: 本来就是目标方的响应时间，再高的并发只增加被限流的概率。
DEFAULT_SEARCH_CONCURRENCY = 5


@dataclass
class CallStats:
    """调用统计。全部来自实际发生的调用，不做估算。"""

    llm_calls: int = 0
    llm_errors: int = 0
    llm_optional_failures: int = 0
    search_calls: int = 0
    search_errors: int = 0
    #: 其中属于**返工轮**的那部分。是 `search_calls` 的**子集**，不是并列项：
    #: 报告里"返工花了多少钱"要靠它算，所以两个数必须能对上——
    #: 子集关系是能被测试守住的那种关系（`rework <= total`）。
    rework_search_calls: int = 0
    raw_hits: int = 0
    site_filter_folded: int = 0
    by_purpose: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "llmCalls": self.llm_calls,
            "llmErrors": self.llm_errors,
            "llmOptionalFailures": self.llm_optional_failures,
            "searchCalls": self.search_calls,
            "searchErrors": self.search_errors,
            "reworkSearchCalls": self.rework_search_calls,
            "rawHits": self.raw_hits,
            "siteFilterFolded": self.site_filter_folded,
            "byPurpose": dict(self.by_purpose),
        }


def stats_of(ctx: PipelineContext) -> CallStats:
    """取（或惰性创建）本次运行的调用统计。

    挂在 `ctx.__dict__` 上而不是声明成 dataclass 字段：它是**派生的**
    运行计数，不参与阶段间传递，也不是任何阶段的产物。
    声明成字段会让"谁负责填它"这个问题有一个假答案。
    """
    stats = ctx.__dict__.get("_call_stats")
    if stats is None:
        stats = CallStats()
        ctx.__dict__["_call_stats"] = stats
    return stats


_stats = stats_of  # 内部简写


# ============================================================
# LLM
# ============================================================


def chat_text(
    ctx: PipelineContext,
    messages: list[dict],
    *,
    tier: str = "aux",
    purpose: str,
    max_tokens: int = 2048,
    temperature: float = 0.6,
    required: bool = True,
    report: CoercionReport | None = None,
) -> str:
    """一次纯文本 LLM 调用。"""
    from app.providers.base import ChatMessage

    stats = _stats(ctx)
    stats.llm_calls += 1
    stats.by_purpose[purpose] = stats.by_purpose.get(purpose, 0) + 1

    with span("llm", purpose, purpose=purpose, detail={"tier": tier}) as sp:
        try:
            response = ctx.llm.chat(
                [ChatMessage(role=m["role"], content=m["content"]) for m in messages],
                tier=tier,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=max_tokens,
                purpose=purpose,
            )
        except ProviderError as exc:
            stats.llm_errors += 1
            if not should_degrade(exc, required=required):
                raise
            # 可选调用失败：记进报告，返回空串。调用方据此降级。
            stats.llm_optional_failures += 1
            if report is not None:
                report.note(f"LLM 调用 `{purpose}` 失败：{type(exc).__name__}: {exc}")
            return ""
        record_llm_usage(sp, response, ctx.llm.name)
        if sp is not None:
            sp.purpose = purpose
    return response.text


def chat_json(
    ctx: PipelineContext,
    messages: list[dict],
    *,
    tier: str = "aux",
    purpose: str,
    max_tokens: int = 3072,
    temperature: float = 0.3,
    required: bool = True,
    report: CoercionReport | None = None,
) -> tuple[dict, CoercionReport]:
    """一次要求 JSON 输出的 LLM 调用，返回 `(解析结果, 容错记录)`。

    `temperature` 默认 0.3，比文本生成低得多：结构化提取需要的是
    稳定复现同一个判断，而不是文采。用 0.7 去抽一张定价表，
    同样的输入两次可能得到不同的档位数量。
    """
    report = report or CoercionReport()
    text = chat_text(
        ctx, messages, tier=tier, purpose=purpose, max_tokens=max_tokens,
        temperature=temperature, required=required, report=report,
    )
    if not text:
        return {}, report
    payload, report = parse_json_object(text, report=report)
    return payload, report


def llm_status(ctx: PipelineContext) -> dict:
    """LLM 的降级状态。

    只有 `FallbackLLM` 有 `summary()`。普通 provider 返回空 dict——
    报告里"是否发生了降级"这一栏在没有备选 provider 时应该完全不出现，
    而不是显示一个恒为 false 的字段。
    """
    summary = getattr(ctx.llm, "summary", None)
    return summary() if callable(summary) else {}


# ============================================================
# 搜索
# ============================================================


def adapt_query(ctx: PipelineContext, query: SearchQuery) -> SearchQuery:
    """能力协商：provider 不支持站点过滤时，把站点当关键词拼进去。

    这是**降级**，不是等价替代：真正的站点过滤是"只返回这个域名的结果"，
    而拼关键词只是"倾向于返回提到这个域名的结果"，后者会混进大量
    提到但并非来自该站点的页面。所以每折叠一次都记一次数，
    报告里如实披露——不披露的话，"平台覆盖数"会看起来比实际可靠。

    站点名去掉了 `www.` 与协议：搜索引擎对 `site:douyin.com` 这类
    写法更友好，但这里刻意**不生成 `site:` 前缀**——那是某个厂商的
    查询方言。拼裸域名在任何搜索引擎上都能工作，虽然精度低一些。
    """
    capabilities = getattr(ctx.search, "capabilities", None)
    if not query.sites or (capabilities and capabilities.site_filter):
        return query

    _stats(ctx).site_filter_folded += 1
    keywords = " ".join(site.replace("www.", "") for site in query.sites)
    return SearchQuery(
        text=f"{query.text} {keywords}".strip(),
        limit=query.limit,
        sites=(),  # 已经折进文本，再传一遍会让适配器尝试过滤并返回空
        freshness=query.freshness,
        locale=query.locale,
    )


def search_sync(
    ctx: PipelineContext, query: SearchQuery, *, rework: bool = False
) -> list[SearchHit]:
    """一次搜索。失败返回空列表并计数，不抛——单条 query 失败
    不该中断整个采集阶段（预算已经花了，剩下的结果仍然有用）。

    `rework=True` 时这次调用记在**返工池**上。计数放在这里而不是
    调用方：这是所有搜索的唯一入口，放这儿才不可能漏记或重复记
    （返工池是小的那个，漏记几次就会让它看起来还有余额）。
    """
    stats = _stats(ctx)
    prepared = adapt_query(ctx, query)
    stats.search_calls += 1
    ctx.search_calls_made += 1
    if rework:
        stats.rework_search_calls += 1
        ctx.rework_search_calls_made += 1

    with span("search", prepared.text[:120], detail={"sites": list(query.sites)}) as sp:
        try:
            hits = list(ctx.search.search(prepared))
        except ProviderError as exc:
            stats.search_errors += 1
            if sp is not None:
                sp.status = "error"
                sp.error = f"{type(exc).__name__}: {exc}"[:200]
            # 搜索**总是**降级：单条 query 失败不该中断整个采集阶段。
            # 所以这里传 `required=False`——过滤掉的是运行时故障之外的
            # 那一类（`HarnessError`），它必须穿透，理由见 `should_degrade`。
            if not should_degrade(exc, required=False):
                raise
            return []
        stats.raw_hits += len(hits)
        ctx.raw_hits += len(hits)
        if sp is not None:
            sp.detail["hits"] = len(hits)
            sp.provider = ctx.search.name
    return hits


async def gather_search(
    ctx: PipelineContext,
    queries: list[SearchQuery],
    *,
    concurrency: int = DEFAULT_SEARCH_CONCURRENCY,
    rework: bool = False,
) -> list[tuple[SearchQuery, list[SearchHit]]]:
    """并发执行一批搜索。

    用 `asyncio.to_thread` 而不是自己开线程池：`to_thread` **会复制
    当前上下文**，所以线程里的 `span()` 能看见 tracer，每次搜索都会
    留下埋点。用裸 `ThreadPoolExecutor` 的话这些埋点会静默消失。
    """
    if not queries:
        return []
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run(query: SearchQuery) -> tuple[SearchQuery, list[SearchHit]]:
        async with semaphore:
            # `to_thread` 直接支持关键字参数，不用 `partial` 包一层。
            return query, await asyncio.to_thread(search_sync, ctx, query, rework=rework)

    return list(await asyncio.gather(*(run(query) for query in queries)))


# ============================================================
# 抓取
# ============================================================


async def gather_fetch(ctx: PipelineContext, evidences: list[Any], *, budget: int) -> Any:
    """抓正文。

    整体丢进一个线程里跑（`fetch_many` 自己内部再开线程池），
    这样 `asyncio.to_thread` 负责复制上下文，而 `fetch_many` 里的
    每个子任务各自 `copy_context()` —— 两层都要做，缺一层埋点就没了。
    """
    from app.core.evidence.fetch import fetch_many

    return await asyncio.to_thread(
        fetch_many,
        ctx.fetcher,
        evidences,
        budget=budget,
    )
