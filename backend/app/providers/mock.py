"""离线 Mock provider。

存在的理由
----------
调研流水线一次完整运行要打几十次 LLM + 上百次搜索，跑一遍真金白银。
如果只有"接上真 API 才能测"这一条路，会直接导致两个后果：
没有人会在改完一行代码后跑一遍完整流程，而"完整流程能跑通"恰恰是最该被守护的东西。

所以 mock 不是"测试用的假数据"，而是**让流水线可以零成本端到端运行的一等实现**。
`run_pipeline_cli --provider mock` 走的是与生产完全相同的代码路径，只有 provider 实现不同。

它也能模拟失败：`MockLLMProvider.fail_purposes` 里的 purpose 会抛错，
用来驱动质检返工、降级等分支——那些路径靠真 API 很难稳定复现。
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator, Sequence
from typing import Any

from app.core.config import Settings
from app.providers.base import (
    PROBE_URL,
    ChatMessage,
    FetchedPage,
    ImageRef,
    LLMCapabilities,
    LLMResponse,
    ModelPricing,
    ProbeResult,
    SearchCapabilities,
    SearchHit,
    SearchQuery,
    Tier,
    TokenUsage,
    probe_search,
    run_probe,
)
from app.providers.errors import Transient
from app.providers.registry import register_fetcher, register_llm, register_search

# 证据池：MockSearch 每产出一条结果就登记一个 id，MockLLM 生成论点时从这里取用。
# 这让"论点引用了真实存在的证据"在 mock 下也成立——否则引用校验会把它们全丢掉，
# 端到端跑出来的报告会是一份空壳，测不出任何东西。
#
# 它是**进程级**的，所以它同时是一个隔离陷阱：这个池子只增不减，
# 而 MockLLM 并不知道自己在为哪次任务服务。取头几条的话，第二个任务
# 的论点会引用**第一个任务**的证据 id——那些 id 在本任务的证据集合里
# 不存在，于是全部被判成幻觉引用、从正文里删掉。
# 表现是"单独跑绿、一起跑红"，而且报告看起来只是"引用质量差"。
EVIDENCE_POOL: list[str] = []


def reset_mock_state() -> None:
    """清空证据池。测试之间必须调用，否则用例会互相污染。"""
    EVIDENCE_POOL.clear()


def _sleep_like_a_real_provider(settings: Settings | None) -> None:
    """按 `MOCK_LATENCY_MS` 人为等待，默认不等。

    mock 默认快得不像话——一整个 quick 任务 0.2 秒。那不是优点，
    那是一个**测不到东西的盲区**：所有跟时间有关的行为（浏览器断线重连、
    SSE 心跳、并发订阅的窗口）都来不及发生，于是"能跑通"和"跑得快"
    在测试里看起来是同一件事。

    三个 provider 的每条路径都过这里，所以 `MOCK_LATENCY_MS=100`
    意味着"每次外部调用慢 100 毫秒"，一次运行的总耗时约等于调用次数乘它。
    设成 0（默认）时这个函数只做一次比较，不影响任何测试的时长。
    """
    delay_ms = settings.mock_latency_ms if settings is not None else 0
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)


def _pick_evidence(count: int) -> list[str]:
    """取**最近**登记的几条，而不是最早的几条。

    在一次真实运行里，被检索到的东西就是"刚刚看到的材料"，所以模型
    引用的是池子尾部——这既更接近真实模型的行为，也让跨任务的污染
    在结构上不发生（当前任务的检索结果一定比别人先前留下的更新）。
    """
    return EVIDENCE_POOL[-count:]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ============================================================
# 搜索
# ============================================================

#: 合成语料。覆盖不同类型与权威度的站点，好让可信度评分能拉开差距——
#: 如果所有结果都是 60 分，评分逻辑就等于没测。
_CORPUS: tuple[tuple[str, str, str], ...] = (
    ("official", "https://www.{slug}.com/pricing", "官方定价页"),
    ("news", "https://www.36kr.com/p/{slug}-funding", "36氪报道"),
    ("news", "https://www.tmtpost.com/{slug}-review", "钛媒体评测"),
    ("zhihu", "https://www.zhihu.com/question/{slug}", "知乎讨论"),
    ("bilibili", "https://www.bilibili.com/video/{slug}", "B站评测视频"),
    ("xiaohongshu", "https://www.xiaohongshu.com/explore/{slug}", "小红书笔记"),
    ("douyin", "https://www.douyin.com/video/{slug}", "抖音短视频"),
    ("review", "https://sspai.com/post/{slug}", "少数派体验报告"),
    ("web", "https://blog.example.com/{slug}-notes", "个人博客"),
    ("unknown", "https://aggregator.test/{slug}", "聚合站转载"),
)

#: 各站点的发布时间，跨越不同时效档位。
_DATES: tuple[str, ...] = (
    "2026-08-14T00:00:00+08:00",
    "2026-05-02T00:00:00+08:00",
    "2025-11-20T00:00:00+08:00",
    "2025-03-08T00:00:00+08:00",
    "2024-06-15T00:00:00+08:00",
)


@register_search("mock")
class MockSearchProvider:
    """确定性搜索。同样的 query 永远得到同样的结果。"""

    name = "mock"
    capabilities = SearchCapabilities(
        site_filter=True,
        freshness_filter=True,
        long_snippet=True,
        max_results_per_call=50,
    )

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self.calls: list[SearchQuery] = []
        self.fail_queries: set[str] = set()

    def probe(self) -> ProbeResult:
        """mock 的探测走的是真的搜索路径，只是它的"网络"在进程内，
        所以它报的是实情：这条链路确实跑得通。"""
        return probe_search(self)

    def search(self, query: SearchQuery) -> Sequence[SearchHit]:
        self.calls.append(query)
        # 等待**放在失败判断之前**：真实 provider 的一轮失败（429、超时）
        # 同样花掉一个往返。反过来放的话，`MOCK_LATENCY_MS` 一设，
        # 失败路径会比成功路径快一个量级，测出来的时序是假的。
        _sleep_like_a_real_provider(self._settings)
        if query.text in self.fail_queries:
            raise Transient("mock：这条 query 被配置为失败", provider=self.name)

        hits: list[SearchHit] = []
        # 扫描的候选数**不能**用 limit 截断：真实 provider 会返回"符合站点过滤的结果"，
        # 而不是"先取 limit 条再过滤"。截断的话，限定到语料里位置靠后的站点
        # （如 douyin.com）永远返回空，平台覆盖数会变成一个由 mock 造成的假 0。
        for index in range(len(_CORPUS) * 2):
            source_type, url_template, site_name = _CORPUS[index % len(_CORPUS)]
            slug = _digest(f"{query.text}:{index}")
            url = url_template.format(slug=slug)

            # 限定站点时只保留命中的那些——模拟真实 provider 的过滤行为，
            # 否则调用方的降级分支（provider 不支持站点过滤）永远不会被触发。
            if query.sites and not any(site in url for site in query.sites):
                continue

            evidence_id = f"EV-{_digest(url)}"
            if evidence_id not in EVIDENCE_POOL:
                EVIDENCE_POOL.append(evidence_id)

            hits.append(
                SearchHit(
                    title=f"{query.text} · {site_name}",
                    url=url,
                    snippet=(
                        f"关于「{query.text}」的{site_name}内容。"
                        f"该来源类型为 {source_type}，用于验证可信度评分的分档是否生效。"
                    ),
                    site_name=site_name,
                    published_at=_DATES[index % len(_DATES)],
                    provider=self.name,
                    rank=index,
                )
            )
            if len(hits) >= query.limit:
                break
        return hits


# ============================================================
# 抓取
# ============================================================


@register_fetcher("mock")
class MockFetcher:
    """返回合成的正文。长度与结构刻意做出差异，好让正文质量相关的评分有区分度。"""

    name = "mock"

    #: 探测目标。用 base 的默认值即可，mock 不看域名、只看 URL 的哈希。
    probe_url: str = PROBE_URL

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self.calls: list[str] = []

    def probe(self) -> ProbeResult:
        """取一次探测目标，同样要求真的抽出了正文。

        这条在 mock 下**有实际意义**：`degrades()` 按 URL 哈希决定是否模拟失败，
        而探测目标是固定的，所以它要么永远绿、要么永远红，不存在"每次跑结果不同"
        的探测。真变红了，说明 mock 的降级规则被改了——那正是想被看见的事。
        """
        url = self.probe_url

        def call() -> str:
            page = self.fetch(url)
            if page.degraded or not page.text.strip():
                raise Transient(f"取回了页面但没能抽出正文：{page.error or '正文为空'}")
            return f"抽出正文 {len(page.text)} 字（HTTP {page.status}）"

        return run_probe(call)

    @staticmethod
    def degrades(url: str) -> bool:
        """这条 URL 会不会被模拟成抓取失败。

        规则暴露出来是为了让测试能**确定性地**取到一条降级记录：
        让测试自己扫 URL 直到撞上失败的话，用例会以约 1/7 的概率随机通过或失败，
        而偶发失败的测试最后一定会被跳过。
        """
        return int(_digest(url)[:2], 16) % 7 == 0

    def fetch(self, url: str, *, fallback_snippet: str = "") -> FetchedPage:
        self.calls.append(url)
        _sleep_like_a_real_provider(self._settings)
        digest = _digest(url)

        # 每 7 条模拟一次抓取失败：降级分支必须有路径可以走到
        if self.degrades(url):
            return FetchedPage(
                url=url,
                final_url=url,
                ok=False,
                text=fallback_snippet,
                status=403,
                degraded=True,
                error="mock：模拟的抓取失败（403）",
            )

        body = (
            f"这是 {url} 的正文。\n\n"
            f"该页面讨论了相关产品的功能、定价与用户反馈。"
            f"正文长度被刻意拉长以通过正文质量检查。\n"
        ) * 12
        return FetchedPage(
            url=url,
            final_url=url,
            ok=True,
            text=body,
            title=f"页面标题 {digest[:6]}",
            published_at="2026-06-01T00:00:00+08:00",
            status=200,
            images=[ImageRef(url=f"https://img.example.com/{digest}.png", alt="示意图")],
            og_image=f"https://img.example.com/{digest}-og.png",
            degraded=False,
        )


# ============================================================
# LLM
# ============================================================


def _claims_payload() -> dict:
    pool = _pick_evidence(4)
    return {
        "claims": [
            {
                "text": "该产品在核心功能上覆盖完整，但高级能力需要付费档位",
                "confidence": "high",
                "evidenceIds": pool[:2],
                "brand": "示例品牌",
            },
            {
                "text": "定价处于同赛道中位偏上，免费额度够试用但不够生产使用",
                "confidence": "medium",
                "evidenceIds": pool[1:3],
                "brand": "示例品牌",
            },
            {
                "text": "用户评价集中在易用性上，抱怨点主要是导出与协作",
                "confidence": "low",
                "evidenceIds": pool[2:4],
                "brand": "示例品牌",
            },
        ]
    }


def _comparison_payload() -> dict:
    pool = _pick_evidence(3)
    return {
        "matrix": {
            "dimensions": ["核心功能", "协作能力", "生态集成", "价格"],
            "brands": ["示例品牌", "对照品牌"],
            "scores": [[4, 3, 4, 2], [3, 4, 3, 4]],
            "evidenceIds": pool,
        },
        "marketShare": [
            {"brand": "示例品牌", "share": 23.5, "basis": "按调研样本推算", "evidenceIds": pool[:1]}
        ],
        "fiveForces": [
            {
                "force": "现有竞争者",
                "intensity": 4,
                "analysis": "同赛道玩家密集，功能差异正在收窄。",
                "evidenceIds": pool[:1],
            }
        ],
        "trends": [
            {
                "name": "搜索热度",
                "unit": "指数",
                "points": [{"period": "2026-01", "value": 62}, {"period": "2026-06", "value": 81}],
                "evidenceIds": pool[:1],
            }
        ],
    }


def _structured_payload() -> dict:
    pool = _pick_evidence(2)
    return {
        "featureTree": {
            "brand": _PLACEHOLDER_BRAND,
            "categories": [
                {
                    "category": "内容管理",
                    "features": [
                        {"name": "多视图", "support": "full", "note": "表格/看板/日历", "evidenceIds": pool}
                    ],
                }
            ],
        },
        "pricingModel": {
            "brand": _PLACEHOLDER_BRAND,
            "currency": "CNY",
            "modelType": "订阅制",
            "freeTier": "个人免费版，容量受限",
            "tiers": [
                {
                    "name": "专业版",
                    "price": "68",
                    "period": "月",
                    "unit": "每席位",
                    "targetUser": "小团队",
                    "includes": ["无限页面", "版本历史"],
                    "evidenceIds": pool,
                }
            ],
        },
        "userPersona": {
            "brand": _PLACEHOLDER_BRAND,
            "personas": [
                {
                    "name": "效率型知识工作者",
                    "segment": "中小团队负责人",
                    "needs": ["快速检索", "多人协作"],
                    "scenarios": ["周会材料整理"],
                    "painPoints": ["跨端同步慢"],
                    "decisionFactors": ["迁移成本", "团队已有习惯"],
                    "migrationCost": "中等",
                    "evidenceIds": pool,
                }
            ],
        },
    }


#: 结构化夹具里那个品牌名是**占位符**，不是某个品牌的名字。
#: 真实的品牌名由 `_rebrand` 按"这次问的是谁"填进去。
_PLACEHOLDER_BRAND = "示例品牌"


def _rebrand(payload: dict, brand: str) -> dict:
    """把结构化夹具里那个 `示例品牌` 占位符换成**真正被问到的那个**品牌。

    为什么必须换：这份夹具早先对每个品牌都回同一份写着 `示例品牌` 的载荷，
    于是 mock 下**每个品牌的功能树都叫同一个名字**——`_analyze_structured`
    存进 ctx 的三棵树品牌全同，而"报告里一个品牌出现两次"这件事
    在 mock 里是**常态**，没人会觉得它可疑。真报告里同一件事是缺陷
    （见 `问题记录.md` 问题 54），而它正是被这个夹具盖住的。

    **只换占位符，不换别的名字。** 测试要能拿这个夹具伪造"问 A 答 B"
    （`monkeypatch.setitem(mock._FIXTURES, "analyze_structured", ...)`
    塞一份品牌写别的载荷）——那正是要验的那条路径，不能被这里改回
    被问的那个名字，否则用例会绿在一个和它声称的相反的原因上。
    """
    out: dict = {}
    for key, value in payload.items():
        if isinstance(value, dict) and value.get("brand") == _PLACEHOLDER_BRAND:
            value = {**value, "brand": brand}
        out[key] = value
    return out


def _scope_payload() -> dict:
    return {
        "subject": "示例调研对象",
        "domain": "效率工具",
        "category": "知识管理",
        "candidateBrands": ["示例品牌", "对照品牌", "第三品牌"],
        "rationale": "mock：按品类关键词扩展候选竞品",
    }


def _clarify_payload() -> dict:
    return {
        "questions": [
            {
                "id": "q1",
                "question": "本次调研希望聚焦哪个层面？",
                "kind": "single",
                "options": ["产品功能", "商业模式", "市场格局"],
                "recommended": "产品功能",
            },
            {
                "id": "q2",
                "question": "目标读者是谁？",
                "kind": "single",
                "options": ["产品团队", "投资决策", "个人学习"],
                "recommended": "产品团队",
            },
            {
                "id": "q3",
                "question": "有没有必须覆盖的维度？",
                "kind": "text",
                "options": [],
                "recommended": "",
            },
        ]
    }


def _plan_payload() -> dict:
    return {
        "subject": "示例调研对象",
        "brands": ["示例品牌", "对照品牌", "第三品牌"],
        "dimensions": ["功能覆盖", "定价策略", "用户口碑", "生态集成"],
        "searchAngles": ["官方功能页", "定价页", "第三方评测", "用户社区讨论"],
        "rationale": "mock：按维度与竞品构造搜索角度",
    }


def _dispatch_payload() -> dict:
    return {
        "lead": "L3-001",
        "strategists": ["L2-001", "L2-002"],
        "executors": ["L1-001", "L1-002", "L1-025"],
        "rationale": "mock：按调研主题挑选战略与执行专家",
    }


def _review_payload() -> dict:
    return {
        "dimensions": [
            {"dimension": "功能覆盖", "score": 4, "comment": "证据较充分"},
            {"dimension": "定价策略", "score": 3, "comment": "仅覆盖主要档位"},
        ],
        "summary": "整体可用，定价维度的证据密度仍可提升。",
    }


def _sentiment_payload() -> dict:
    return {
        "labels": [
            {"index": 0, "sentiment": "positive", "reason": "认可易用性"},
            {"index": 1, "sentiment": "negative", "reason": "抱怨价格"},
            {"index": 2, "sentiment": "neutral", "reason": "陈述性描述"},
        ]
    }


#: purpose → 载荷。查找时先精确匹配，再退回冒号前的前缀
#: （`write:feature` 会命中 `write`），这样章节写作可以只维护一条模板。
_FIXTURES: dict[str, Any] = {
    "scope": _scope_payload,
    "clarify": _clarify_payload,
    "plan": _plan_payload,
    "dispatch": _dispatch_payload,
    "analyze_claims": _claims_payload,
    "analyze_comparison": _comparison_payload,
    "analyze_structured": _structured_payload,
    "audit_review": _review_payload,
    "sentiment": _sentiment_payload,
}


def _section_text() -> str:
    """章节正文模板。返回的是纯文本而非 JSON。

    **正文里带真实的 `[证据: EV-xxxx]` 角标**，而不是一段干净的文字。
    这不只是为了好看：`write.resolve_citations` 要从正文里解析引用的，
    给一段不带角标的模板就等于让这条路径在 mock 下**永远不被执行**——
    于是"引用解析写错了"这件事会一直等到接上真 API 才暴露，
    而那时候你面对的是几十次真实调用换来的一个格式错误。
    """
    pool = _pick_evidence(4)
    cited = [f"[证据: {eid}]" for eid in pool]
    while len(cited) < 4:
        cited.append("")

    return (
        "本节基于已采集的证据梳理相关结论。\n\n"
        f"第一，功能覆盖面上，官方页面与第三方评测给出了较为一致的描述，"
        f"核心能力已经完备，差异主要体现在高级档位{cited[0]}。\n\n"
        f"第二，定价策略上，免费额度足以完成试用，但生产环境使用需要付费档位，"
        f"这在用户社区讨论中被反复提及{cited[1]}。\n\n"
        f"第三，从用户反馈看，正面评价集中在易用性，负面评价集中在导出与协作能力，"
        f"这一分歧在不同平台上表现一致{cited[2]}{cited[3]}。\n"
    )


# 章节写作的 purpose 是 `write:<section>` / `refine:<section>`，
# 靠前缀回退命中这两条，所以章节模板只需要维护一份。
_FIXTURES["write"] = _section_text
_FIXTURES["refine"] = _section_text


@register_llm("mock")
class MockLLMProvider:
    """确定性 LLM。

    - 按 `purpose` 返回预置载荷；返回值是 JSON 字符串，与真实 provider 一致
    - `fail_purposes` 里的 purpose 直接抛错，用于驱动返工/降级分支
    - `calls` 记录全部调用，测试据此断言"到底打了几次、打给了谁"
    """

    name = "mock"
    capabilities = LLMCapabilities(
        json_mode=True,
        json_schema_mode=True,
        thinking_toggle=True,
        streaming=True,
        max_context_tokens=128_000,
        max_output_tokens=8_192,
    )

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self.calls: list[dict] = []
        self.fail_purposes: set[str] = set()
        self.overrides: dict[str, Any] = {}

    # ---- 内部 ----

    def _resolve_payload(self, purpose: str) -> Any:
        if purpose in self.overrides:
            return self.overrides[purpose]
        if purpose in _FIXTURES:
            return _FIXTURES[purpose]()
        prefix, _, suffix = purpose.partition(":")
        if prefix in self.overrides:
            return self.overrides[prefix]
        if prefix in _FIXTURES:
            payload = _FIXTURES[prefix]()
            if prefix == "analyze_structured" and suffix:
                payload = _rebrand(payload, suffix)
            return payload
        return None

    # ---- LLMProvider ----

    def resolve_model(self, tier: Tier) -> str:
        return f"mock-{tier}"

    def probe(self) -> ProbeResult:
        """真的打一次 mock 补全。

        `purpose="probe"` 不在 `_FIXTURES` 里，于是回落到 `None`——mock 对未知
        purpose 本来就返回空载荷，这正是想要的：探测关心链路通不通，不关心内容。

        耗时用 `run_probe` 自己量的，不读 `resp.latency_ms`：mock 的那个字段是
        写死的 1，报出去是个假数字。
        """

        def call() -> str:
            resp = self.chat(
                [ChatMessage(role="user", content="ping")],
                tier="fast",
                temperature=0.0,
                max_tokens=1,
                purpose="probe",
            )
            return f"{resp.model} 应答正常"

        return run_probe(call)

    def pricing(self) -> dict[str, ModelPricing]:
        # 定价为零：mock 不产生费用，报一个假的单价会让成本指标失去意义。
        return {
            f"mock-{tier}": ModelPricing(0.0, 0.0, note="mock 不产生费用") for tier in ("core", "aux", "fast")
        }

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
        _sleep_like_a_real_provider(self._settings)
        model = self.resolve_model(tier)
        prompt_chars = sum(len(m.content) for m in messages)
        self.calls.append(
            {
                "purpose": purpose,
                "tier": tier,
                "model": model,
                "json_mode": json_mode,
                "evidence_ids": list(evidence_ids or []),
                "prompt_chars": prompt_chars,
            }
        )

        if purpose in self.fail_purposes:
            raise Transient(f"mock：purpose={purpose!r} 被配置为失败", provider=self.name)

        payload = self._resolve_payload(purpose)
        if payload is None:
            # 没配模板的 purpose 一律当章节正文处理。这是刻意宽松的兜底：
            # 提示词一改，purpose 字符串可能就对不上模板了，而对不上的后果
            # 如果是抛错，流水线会因为一处命名改动整体跑不起来。
            text = _section_text()
        elif isinstance(payload, str):
            text = payload
        else:
            text = json.dumps(payload, ensure_ascii=False)

        # token 数按字符数粗略折算，让进度与成本面板有非零的值可显示。
        # 真实 provider 返回的是 API 给的精确值。
        completion_tokens = max(1, len(text) // 2)
        prompt_tokens = max(1, prompt_chars // 3)
        return LLMResponse(
            text=text,
            model=model,
            provider=self.name,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                cost_usd=0.0,
            ),
            latency_ms=1,
            finish_reason="stop",
        )

    def chat_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tier: Tier = "aux",
        temperature: float = 0.6,
        max_tokens: int = 2048,
    ) -> Iterator[str]:
        _ = (messages, tier, temperature, max_tokens)
        yield from ("mock ", "流式 ", "输出")
