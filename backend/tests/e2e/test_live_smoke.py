"""真实外部调用的冒烟测试。**默认不跑**（见 `pyproject.toml` 的 `addopts`）。

    .venv/Scripts/python.exe -m pytest -m live -v

它们测的是 mock **测不到**的那一类东西：厂商的线上行为。
mock 是一个我们自己写的、永远符合我们预期的世界——它能证明"代码逻辑自洽"，
不能证明"对面那个服务今天还是我们以为的样子"。这个文件是给后者留的位置。

成本：整份文件约 4 次外部调用，实测花钱量级是**千分之一美分量级**。
完整流水线那一条（十几块钱量级的真实报告）另外用环境变量开启，见文件末尾。

为什么不做成"没配密钥就跳过"
--------------------------
跳过会让 `pytest -m live` 在一台没有任何凭据的机器上输出一个**全是绿的空**。
那正是这个项目一直在防的那种绿：它看起来在说"联网这条链路是通的"，
而其实一个请求都没发出去。显式要求跑联网测试而凭据缺失，就该红。
"""
from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.providers.base import ChatMessage, SearchQuery
from app.providers.registry import load_builtin_providers

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def settings():
    load_builtin_providers()
    return get_settings()


@pytest.fixture
def llm(settings):
    """当前配置的 LLM。没配就红——见模块 docstring。"""
    from app.providers.registry import get_llm

    provider = get_llm()
    if provider.name == "mock":
        pytest.fail(
            "LLM 是 mock。`-m live` 的用例必须打真实服务；"
            "要么在 .env 里配好 LLM_PROVIDER，要么别请求 live。"
        )
    return provider


# ============================================================
# 三家各自的链路
# ============================================================


def test_the_configured_llm_answers(llm):
    """一次最短的真实补全。验证密钥、端点、模型名三件事。"""
    resp = llm.chat(
        [ChatMessage(role="user", content="只回复两个字：收到")],
        tier="fast",
        temperature=0.0,
        max_tokens=8,
    )

    assert resp.text.strip(), "返回了空文本——可能是被安全策略拦了，也可能是模型名对不上"
    assert resp.model, "响应里没有模型名，成本就查不到表"
    assert resp.usage.prompt_tokens > 0, "usage 为空的话成本与 token 指标全是 0"
    assert resp.latency_ms > 0


def test_the_configured_search_returns_hits(settings):
    """一次真实搜索。

    **允许 0 条结果**：一次合法的搜索本来就可能空手而归，把它判成失败会让
    这条测试间歇性变红。真正要拦的是"密钥不对/配额用尽"——那两类会被适配器
    映射成异常，在这里就抛出来了。
    """
    from app.providers.registry import get_search

    provider = get_search()
    if provider.name == "mock":
        pytest.fail("搜索是 mock，不能算作一次联网冒烟。")

    hits = provider.search(SearchQuery(text="竞品分析", limit=3))

    for hit in hits:
        assert hit.url.startswith("http"), f"返回了一条不像链接的结果：{hit.url!r}"


def test_the_configured_fetcher_extracts_real_text(settings):
    """抓一个真实页面。

    断言的是**抽出了正文**而不是 HTTP 200：抓取器最常见的故障是页面取回来了
    但抽取规则对不上，于是每条证据都静默退化成只有摘要。那种故障下状态码是 200。
    """
    from app.providers.base import PROBE_URL
    from app.providers.registry import get_fetcher

    provider = get_fetcher()
    if provider.name == "mock":
        pytest.fail("抓取是 mock，不能算作一次联网冒烟。")

    page = provider.fetch(PROBE_URL)

    assert page.status == 200, f"HTTP {page.status}：{page.error}"
    assert not page.degraded, f"取回来了但没抽出正文：{page.error or '正文为空'}"
    assert len(page.text.strip()) > 200


# ============================================================
# 只可能被线上行为打破的约定
# ============================================================


def test_the_cache_hit_field_is_still_where_we_read_it(llm):
    """**这是这份文件里最该存在的一条。**

    缓存命中数与未命中数的单价差 50 倍，而命中数是从响应里一个**厂商私有字段**
    读出来的。厂商改了字段名、或 SDK 升级后不再透传未知字段，读取就会**静默**
    返回 0——成本随之偏高，而界面上一切正常，没有任何迹象。

    做法是发两次完全相同、且前缀足够长的请求：第二次必然命中一部分前缀。
    比对着文档断言字段存在要硬，因为它验的是"线上真的会命中并且我们读得到"。
    """
    from app.providers.llm.deepseek import DeepSeekProvider

    if not isinstance(llm, DeepSeekProvider):
        pytest.skip(f"这条盯的是 DeepSeek 的私有字段，当前 LLM 是 {llm.name}")

    prefix = "以下是一段足够长的固定系统说明，用于触发前缀缓存：" + ("规则若干。" * 200)
    messages = [ChatMessage(role="system", content=prefix)]

    first = llm.chat(messages, tier="fast", temperature=0.0, max_tokens=4)
    second = llm.chat(messages, tier="fast", temperature=0.0, max_tokens=4)

    assert second.usage.prompt_tokens > 0
    assert second.usage.cached_prompt_tokens > 0, (
        "第二次同样的请求没有报出任何缓存命中。要么厂商挪了字段，"
        "要么 SDK 不再透传未知字段——两种情况都会让成本静默偏高。"
        f"（第一次 prompt={first.usage.prompt_tokens} "
        f"cached={first.usage.cached_prompt_tokens}）"
    )
    assert second.usage.cached_prompt_tokens <= second.usage.prompt_tokens


def test_a_configured_llm_model_actually_has_a_price(llm):
    """配了模型名却查不到价，成本会静默记 0——而 0 在成本表上看起来像"免费"。

    mock 之外就没有单价为零的正当理由，所以这条要红。
    """
    table = llm.pricing()
    model = llm.resolve_model("fast")

    assert model in table, (
        f"{llm.name} 的 {model} 没有定价。定价表里只有 {sorted(table)}——"
        "模型 ID 是账号相关的，报价规则要跟着配置走（见 deepseek._priced_models）。"
    )
    assert table[model].input_per_mtok_usd > 0


def test_usage_is_actually_priced(llm):
    """最后一道：走一趟完整的调用，确认成本**不是 0**。

    前一条查的是表里有没有价，这一条查的是价有没有真的被应用到 usage 上——
    `apply_pricing` 漏调、或模型名对不上查表落空，都会在这里现形。
    """
    resp = llm.chat(
        [ChatMessage(role="user", content="只回复：好")],
        tier="fast",
        temperature=0.0,
        max_tokens=4,
    )

    assert resp.usage.cost_usd > 0, (
        f"这次调用记成了 0 成本（prompt={resp.usage.prompt_tokens}）。"
        "定价表漏配或 apply_pricing 没接上。"
    )
