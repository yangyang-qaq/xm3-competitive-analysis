"""抓取编排：决定抓哪些、抓几条、并发多少。

为什么不按"搜索结果的顺序"抓
---------------------------
搜索返回的前 N 条里，常常有 3 条来自同一个站点。按顺序抓的话，
预算会被一个站点吃掉，最后报告里的"独立信源数"只有 2——而这一步
本来就该为交叉验证服务。

所以抓取是**按域名轮转**的：每个域名先各抓一条，再回头看第二轮。
这样在任意预算下，域名覆盖数都先于单域名的深度增长。

预算是硬上限
------------
超预算不是"多花一点"，而是任务跑不完。所以宁可少抓几条并把
"未抓取 N 条"如实记进 `skipped_budget`，也不要跑到一半被掐。
"""
from __future__ import annotations

import contextvars
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from app.core.evidence.sourcetypes import independent_domain, source_base_score
from app.core.evidence.textquality import assess_text
from app.core.models import Evidence
from app.core.observability.trace import span
from app.providers.errors import should_degrade

#: 单个域名最多抓几条。超过之后，多抓的边际信息量很低——
#: 同一站点的多篇文章通常共享同一个信息源与立场。
DEFAULT_MAX_PER_DOMAIN = 3

#: 并发抓取数。默认 6：再高对目标站点不礼貌，而且绝大多数时间
#: 花在等待上，提高并发对总耗时的改善会迅速变平。
DEFAULT_CONCURRENCY = 6


@dataclass
class FetchOutcome:
    """一轮抓取的结果。所有数字都直接进报告，不加工。"""

    considered: int = 0
    attempted: int = 0
    ok: int = 0
    degraded: int = 0
    #: 因为预算或单域名上限而没抓的条数。**必须显式记录**：
    #: 不记的话，"这份报告只用了 8 条证据"看起来像是只有 8 条可用。
    skipped_budget: int = 0

    #: evidence_id → 模板噪声占比。交给可信度评分做正文分折扣。
    boilerplate: dict[str, float] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def degraded_rate(self) -> float:
        if self.attempted <= 0:
            return 0.0
        return round(self.degraded / self.attempted, 4)

    def to_dict(self) -> dict:
        return {
            "considered": self.considered,
            "attempted": self.attempted,
            "ok": self.ok,
            "degraded": self.degraded,
            "skippedBudget": self.skipped_budget,
            "degradedRate": self.degraded_rate,
            "failures": list(self.failures[:20]),
        }


def _priority(ev: Evidence) -> float:
    """抓取优先级。

    只看**不需要抓取就能知道**的信号：来源类型基准分、有没有发布时间、
    有没有摘要。用不上正文本身——那正是我们要去取的东西。
    相关度如果已经算过（`relevance_score` 在 detail 里）也算进来。
    """
    score = source_base_score(ev.source_type)
    if ev.published_at:
        score += 4.0
    if len(ev.snippet) >= 80:
        score += 3.0
    if not ev.title:
        score -= 5.0
    return score


def select_targets(
    evidences: Sequence[Evidence],
    *,
    budget: int,
    max_per_domain: int = DEFAULT_MAX_PER_DOMAIN,
) -> tuple[list[Evidence], int]:
    """按域名轮转挑出要抓的证据，返回 (选中的, 因预算跳过的条数)。

    单独成函数是为了让它能被单测直接覆盖：选哪几条是个有明确对错的
    决策，不该藏在并发编排的中间。
    """
    if budget <= 0:
        return [], len(evidences)

    ordered = sorted(evidences, key=_priority, reverse=True)

    # 域名 → 已选条数。空域名的归到一组（多半是畸形 URL，
    # 但也要有限流，不能因为归不了组就无限选）。
    buckets: dict[str, list[Evidence]] = {}
    for ev in ordered:
        buckets.setdefault(independent_domain(ev.url), []).append(ev)

    # 轮转：每一轮从每个域名取一条，直到取满预算或全部取完。
    chosen: list[Evidence] = []
    for round_index in range(max_per_domain):
        for domain in sorted(buckets, key=lambda d: _priority(buckets[d][0]), reverse=True):
            bucket = buckets[domain]
            if round_index >= len(bucket):
                continue
            if len(chosen) >= budget:
                return chosen, len(evidences) - len(chosen)
            chosen.append(bucket[round_index])

    return chosen, max(0, len(evidences) - len(chosen))


def fetch_many(
    fetcher,
    evidences: Sequence[Evidence],
    *,
    budget: int,
    max_per_domain: int = DEFAULT_MAX_PER_DOMAIN,
    concurrency: int = DEFAULT_CONCURRENCY,
    on_fetched: Callable[[Evidence], None] | None = None,
) -> FetchOutcome:
    """抓正文并写回证据对象。

    单条失败不打断整批：抓取失败是**常态**（403、超时、验证码页），
    不是异常。整批因为一条失败而中止，等于把常态当成了故障。
    """
    outcome = FetchOutcome(considered=len(evidences))
    targets, skipped = select_targets(
        evidences, budget=budget, max_per_domain=max_per_domain
    )
    outcome.skipped_budget = skipped
    outcome.attempted = len(targets)
    if not targets:
        return outcome

    def fetch_one(ev: Evidence) -> None:
        with span("fetch", ev.url[:120], detail={"domain": independent_domain(ev.url)}) as sp:
            try:
                page = fetcher.fetch(ev.url, fallback_snippet=ev.snippet)
            except Exception as exc:
                # 抓取器的异常已经过重试层，到这里就是最终失败。
                #
                # `HarnessError` 是例外，它必须穿透：回放时少一条抓取录制，
                # 在这里会变成 `ev.degraded = True`，也就是"这条证据只有摘要"。
                # 那是一条**真实且合理**的降级说明——只是原因错了，
                # 它不是抓取失败，是我们的夹具缺了一条。两者的区别在下游
                # 完全看不出来，所以只能在源头分开。
                if not should_degrade(exc, required=False):
                    raise
                ev.degraded = True
                outcome.failures.append(f"{ev.url}: {type(exc).__name__}: {exc}"[:200])
                if sp is not None:
                    sp.status = "degraded"
                    sp.error = f"{type(exc).__name__}: {exc}"[:200]
                return

            if page.title and not ev.title:
                ev.title = page.title
            if page.published_at and not ev.published_at:
                ev.published_at = page.published_at
            if page.images:
                ev.images = [
                    {"url": image.url, "alt": image.alt}
                    for image in page.images[:6]
                    if image.url
                ]

            text = page.text or ""
            ev.full_text = text
            ev.degraded = bool(page.degraded) or not page.ok

            quality = assess_text(text)
            outcome.boilerplate[ev.evidence_id] = quality.boilerplate
            if sp is not None:
                sp.detail.update(
                    {"chars": len(text), "boilerplate": quality.boilerplate,
                     "degraded": ev.degraded}
                )
                if ev.degraded:
                    sp.status = "degraded"

    # 并发抓取。**每一批都要自带一份上下文副本**：`ThreadPoolExecutor.submit`
    # 与 `asyncio.to_thread` 不同，它**不会**复制 contextvars。
    # 不手工复制的话，线程里的 `span()` 拿不到 tracer，所有抓取埋点
    # 会静默变成空操作——成本表少掉一整类调用，而且不报错。
    # 每个任务单独 `copy_context()`：同一个 Context 对象不能被并发进入。
    workers = max(1, min(concurrency, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for ev in targets:
            ctx = contextvars.copy_context()
            futures.append(pool.submit(ctx.run, fetch_one, ev))
        for future in futures:
            future.result()  # fetch_one 内部已吞掉所有异常，这里只等它结束

    for ev in targets:
        if ev.degraded:
            outcome.degraded += 1
        else:
            outcome.ok += 1
        if on_fetched is not None:
            on_fetched(ev)

    return outcome
