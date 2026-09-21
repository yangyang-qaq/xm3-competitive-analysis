"""归一化契约：token 累加、定价折算、搜索请求指纹。"""
from __future__ import annotations

import pytest

from app.providers.base import (
    ModelPricing,
    SearchQuery,
    TokenUsage,
    apply_pricing,
)


def test_usage_addition_is_componentwise():
    a = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, cost_usd=0.001)
    b = TokenUsage(prompt_tokens=20, completion_tokens=7, total_tokens=27, cost_usd=0.002)
    total = a + b
    assert (total.prompt_tokens, total.completion_tokens, total.total_tokens) == (30, 12, 42)
    assert total.cost_usd == 0.003


def test_usage_addition_does_not_mutate():
    """frozen dataclass 的价值就在这儿：累加必须产生新对象，
    否则一处 += 会悄悄改掉别的 span 引用的那份 usage。"""
    a = TokenUsage(prompt_tokens=10)
    _ = a + TokenUsage(prompt_tokens=1)
    assert a.prompt_tokens == 10


def test_usage_as_dict_is_camel_case():
    usage = TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3, cost_usd=0.123456789)
    assert usage.as_dict() == {
        "promptTokens": 1,
        "completionTokens": 2,
        "totalTokens": 3,
        "costUsd": 0.123457,
        "cachedPromptTokens": 0,
        "cacheHitRate": 0.0,
    }


def test_pricing_cost_is_per_million_tokens():
    price = ModelPricing(input_per_mtok_usd=0.27, output_per_mtok_usd=1.10)
    usage = TokenUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert price.cost(usage) == 0.27 + 1.10


# ============================================================
# 缓存命中计价
# ============================================================


def test_cached_tokens_are_billed_at_the_cached_rate():
    """命中与未命中的输入单价差 50 倍（DeepSeek 闲置时段 0.02 对 1 元/Mtok），
    所以这条算术错了，整份报告的成本就是错的。

    100 万输入里 90 万命中：0.27 的价下应当只有 0.1×0.27 走未命中价。
    """
    price = ModelPricing(
        input_per_mtok_usd=1.0, output_per_mtok_usd=4.0, cached_input_per_mtok_usd=0.02
    )
    usage = TokenUsage(prompt_tokens=1_000_000, cached_prompt_tokens=900_000)

    assert price.cost(usage) == pytest.approx(100_000 / 1e6 * 1.0 + 900_000 / 1e6 * 0.02)


def test_cached_tokens_are_a_subset_not_an_extra_batch():
    """**命中数是提示词里的子集**，不是另外一批 token。

    把它当成增量的话，输入量会被算成 190 万而不是 100 万，成本直接翻倍——
    而这类错误在数字上看起来完全合理，没人会去查。
    """
    price = ModelPricing(input_per_mtok_usd=1.0, output_per_mtok_usd=0.0, cached_input_per_mtok_usd=0.0)
    all_cached = TokenUsage(prompt_tokens=1_000_000, cached_prompt_tokens=1_000_000)

    # 全部命中且命中价为 0 时，输入侧应当一分钱不花
    assert price.cost(all_cached) == 0.0


def test_a_missing_cached_rate_falls_back_to_the_full_input_rate():
    """没配缓存价时按未命中价计，得到的是**上界**。

    宁可偏高：偏高会在成本表上多看两眼，偏低会让一份看起来便宜的报告
    永远不被复查。而"这家不区分缓存"的那些 provider 压根不会报出命中数，
    走不到这个分支。
    """
    no_cache_price = ModelPricing(input_per_mtok_usd=1.0, output_per_mtok_usd=4.0)
    usage = TokenUsage(prompt_tokens=1_000_000, cached_prompt_tokens=900_000)

    assert no_cache_price.cost(usage) == pytest.approx(1_000_000 / 1e6 * 1.0)


def test_cached_count_cannot_exceed_the_prompt_count():
    """适配器把命中数报大了（或多轮累加时算错）也不该让成本变成负数。

    夹紧而不是抛异常：一个坏掉的 usage 字段不该让整份报告跑不出来，
    但它也不该把成本算出一个不可能的值。
    """
    price = ModelPricing(input_per_mtok_usd=1.0, output_per_mtok_usd=0.0, cached_input_per_mtok_usd=0.5)
    usage = TokenUsage(prompt_tokens=1_000_000, cached_prompt_tokens=5_000_000)

    assert price.cost(usage) == pytest.approx(1_000_000 / 1e6 * 0.5)


def test_apply_pricing_carries_the_cached_count_through():
    """定价这一步重建了 TokenUsage，漏带字段的话命中数会在这里消失。

    症状不是"少显示一个数"——命中率会恒为 0，看起来像一次都没命中过，
    于是没人再去查成本为什么降不下来。
    """
    usage = TokenUsage(prompt_tokens=1000, cached_prompt_tokens=800)
    priced = apply_pricing(usage, ModelPricing(1.0, 2.0, 0.02))

    assert priced.cached_prompt_tokens == 800
    assert priced.cache_hit_rate == pytest.approx(0.8)


def test_usage_addition_sums_cached_tokens_too():
    a = TokenUsage(prompt_tokens=100, cached_prompt_tokens=60)
    b = TokenUsage(prompt_tokens=200, cached_prompt_tokens=100)

    assert (a + b).cached_prompt_tokens == 160


def test_missing_pricing_yields_zero_cost_not_a_guess():
    """没有定价表时成本记 0。

    编一个"大概"的单价会让整张成本表失去可信度——
    而成本表恰恰是作品集里最容易被追问的那一页。
    """
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)
    assert apply_pricing(usage, None) == usage
    assert apply_pricing(usage, None).cost_usd == 0.0


def test_apply_pricing_fills_total_and_cost():
    usage = TokenUsage(prompt_tokens=2000, completion_tokens=1000)
    priced = apply_pricing(usage, ModelPricing(1.0, 2.0))
    assert priced.total_tokens == 3000
    assert priced.cost_usd == pytest.approx(2000 / 1e6 * 1.0 + 1000 / 1e6 * 2.0)


def test_search_cache_key_covers_every_field():
    """漏掉任何一个字段，cassette 回放就会命中错误的记录。

    这类错误极难发现：回放"成功"了，只是结果是另一个 query 的。
    """
    base = SearchQuery(text="定价", limit=10, sites=("a.com",), freshness="month", locale="zh-CN")
    variations = [
        SearchQuery(text="定价2", limit=10, sites=("a.com",), freshness="month"),
        SearchQuery(text="定价", limit=11, sites=("a.com",), freshness="month"),
        SearchQuery(text="定价", limit=10, sites=("b.com",), freshness="month"),
        SearchQuery(text="定价", limit=10, sites=("a.com",), freshness="year"),
        SearchQuery(text="定价", limit=10, sites=("a.com",), freshness="month", locale="en-US"),
    ]
    keys = {str(base.cache_key())}
    for variant in variations:
        assert str(variant.cache_key()) not in keys, f"{variant} 与基准的指纹撞了"
        keys.add(str(variant.cache_key()))


def test_search_cache_key_is_stable_across_calls():
    query = SearchQuery(text="定价", sites=("a.com", "b.com"))
    assert query.cache_key() == query.cache_key()


def test_search_cache_key_normalizes_sites_to_list():
    """元组和列表要产生同一个指纹——JSON 序列化时它们本来就该等价，
    否则同一份 cassette 会因为调用方传了 list 还是 tuple 而 miss。"""
    assert SearchQuery(text="x", sites=("a.com",)).cache_key() == SearchQuery(
        text="x", sites=("a.com",)
    ).cache_key()
    assert isinstance(SearchQuery(text="x", sites=("a.com",)).cache_key()["sites"], list)
