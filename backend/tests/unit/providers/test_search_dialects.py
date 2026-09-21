"""方言翻译 —— 适配层存在的全部理由，所以这里测得最细。

用 `httpx.MockTransport` 拦截请求：不发网络、不用密钥、但走的是真实的
请求构造与响应解析代码。这是唯一能在零成本下测"请求发对了没"的办法。

另：所有重试策略都被换成 NO_RETRY。否则一个 500 的用例会真的睡 4+8+15+25 秒，
测试套件会从"没人跑"变成"没人敢跑"。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.providers import retry as retry_mod
from app.providers.base import SearchQuery
from app.providers.errors import (
    AuthFailed,
    BadRequest,
    ProviderError,
    ProviderNotConfigured,
    RateLimited,
    Transient,
)
from app.providers.search.bocha import BochaSearchProvider
from app.providers.search.tavily import TavilySearchProvider


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    for cls in (BochaSearchProvider, TavilySearchProvider):
        monkeypatch.setattr(cls, "retry_policy", retry_mod.NO_RETRY)


# ============================================================
# 博查
# ============================================================


def _bocha_body() -> dict:
    return {
        "code": 200,
        "data": {
            "webPages": {
                "value": [
                    {
                        "name": "标题A",
                        "url": "https://www.douyin.com/video/1",
                        "snippet": "短摘要",
                        "summary": "长摘要内容",
                        "siteName": "抖音",
                        "datePublished": "2026-08-14T00:00:00+08:00",
                    },
                    {
                        "name": "标题B",
                        "url": "https://36kr.com/p/2",
                        "snippet": "只有摘要",
                        "siteName": "36氪",
                    },
                    {"name": "没有 URL 的条目", "url": "", "snippet": "应被丢弃"},
                ]
            }
        },
    }


def _bocha(handler, monkeypatch) -> tuple[BochaSearchProvider, list[httpx.Request]]:
    monkeypatch.setenv("BOCHA_API_KEY", "test-key")
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return BochaSearchProvider(Settings(), http_client=_client(wrapped)), seen


def test_bocha_translates_the_whole_dialect(monkeypatch):
    """一条 query 同时验证 5 项翻译：query / include / freshness / count / 鉴权头。"""
    provider, seen = _bocha(lambda r: httpx.Response(200, json=_bocha_body()), monkeypatch)

    provider.search(
        SearchQuery(text="抖音 知识管理", sites=("douyin.com", "bilibili.com"), freshness="month", limit=15)
    )

    request = seen[0]
    payload = json.loads(request.content)
    assert request.url.path.endswith("/web-search")
    assert payload["query"] == "抖音 知识管理"
    assert payload["include"] == "douyin.com|bilibili.com"  # 列表 → `|` 连接
    assert payload["freshness"] == "oneMonth"               # month → oneMonth
    assert payload["count"] == 15
    assert payload["summary"] is True
    assert request.headers["authorization"] == "Bearer test-key"


@pytest.mark.parametrize(
    ("freshness", "expected"),
    [("any", "noLimit"), ("day", "oneDay"), ("week", "oneWeek"), ("month", "oneMonth"), ("year", "oneYear")],
)
def test_bocha_freshness_table(freshness, expected, monkeypatch):
    provider, seen = _bocha(lambda r: httpx.Response(200, json=_bocha_body()), monkeypatch)
    provider.search(SearchQuery(text="x", freshness=freshness))
    assert json.loads(seen[0].content)["freshness"] == expected


def test_bocha_omits_include_when_no_sites(monkeypatch):
    """不限定站点时不该出现空的 include——有些后端会把它当成"限定到空集合"。"""
    provider, seen = _bocha(lambda r: httpx.Response(200, json=_bocha_body()), monkeypatch)
    provider.search(SearchQuery(text="x"))
    assert "include" not in json.loads(seen[0].content)


def test_bocha_clamps_limit_to_provider_max(monkeypatch):
    provider, seen = _bocha(lambda r: httpx.Response(200, json=_bocha_body()), monkeypatch)
    provider.search(SearchQuery(text="x", limit=999))
    assert json.loads(seen[0].content)["count"] == 50


def test_bocha_parses_hits_and_prefers_long_summary(monkeypatch):
    provider, _ = _bocha(lambda r: httpx.Response(200, json=_bocha_body()), monkeypatch)
    hits = provider.search(SearchQuery(text="x"))

    assert len(hits) == 2  # 空 URL 的那条被丢掉
    assert hits[0].snippet == "长摘要内容"
    assert hits[1].snippet == "只有摘要"  # 没有 summary 时退回 snippet
    assert hits[0].site_name == "抖音"
    assert hits[0].published_at == "2026-08-14T00:00:00+08:00"
    assert hits[0].provider == "bocha"
    assert [h.rank for h in hits] == [0, 1]


def test_bocha_business_error_inside_http_200(monkeypatch):
    """博查把业务错误码放在 HTTP 200 的响应体里。

    只看状态码的话会当成"搜索没结果"，而不是"密钥不对"——
    这是最难查的一类静默失败，所以这条用例是必须的。
    """
    provider, _ = _bocha(lambda r: httpx.Response(200, json={"code": 403, "msg": "无效的密钥"}), monkeypatch)
    with pytest.raises(AuthFailed):
        provider.search(SearchQuery(text="x"))


def test_bocha_business_rate_limit_is_retryable(monkeypatch):
    provider, _ = _bocha(lambda r: httpx.Response(200, json={"code": 429, "msg": "请求过于频繁"}), monkeypatch)
    with pytest.raises(RateLimited) as excinfo:
        provider.search(SearchQuery(text="x"))
    assert excinfo.value.retryable is True


def test_bocha_unmapped_business_code_is_not_retryable(monkeypatch):
    provider, _ = _bocha(lambda r: httpx.Response(200, json={"code": 2001, "msg": "账户异常"}), monkeypatch)
    with pytest.raises(ProviderError) as excinfo:
        provider.search(SearchQuery(text="x"))
    assert type(excinfo.value) is ProviderError
    assert excinfo.value.retryable is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, AuthFailed), (400, BadRequest), (429, RateLimited), (500, Transient), (503, Transient)],
)
def test_bocha_http_status_mapping(status, expected, monkeypatch):
    provider, _ = _bocha(lambda r: httpx.Response(status, text="err"), monkeypatch)
    with pytest.raises(expected):
        provider.search(SearchQuery(text="x"))


def test_bocha_non_json_body_is_transient(monkeypatch):
    provider, _ = _bocha(
        lambda r: httpx.Response(200, text="<html>502 Bad Gateway</html>", headers={"content-type": "text/html"}),
        monkeypatch,
    )
    with pytest.raises(Transient):
        provider.search(SearchQuery(text="x"))


def test_bocha_missing_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("BOCHA_API_KEY", raising=False)
    with pytest.raises(ProviderNotConfigured):
        BochaSearchProvider(Settings())


def test_bocha_empty_result_set_is_not_an_error(monkeypatch):
    """搜到 0 条是合法结果（新品牌、冷门话题），不是故障。"""
    provider, _ = _bocha(lambda r: httpx.Response(200, json={"code": 200, "data": {}}), monkeypatch)
    assert provider.search(SearchQuery(text="x")) == []


# ============================================================
# Tavily
# ============================================================


def _tavily_body() -> dict:
    return {
        "results": [
            {
                "title": "Some post",
                "url": "https://sspai.com/post/9",
                "content": "正文片段",
                "published_date": "2026-07-01",
                "score": 0.9,
            }
        ]
    }


def _tavily(handler, monkeypatch) -> tuple[TavilySearchProvider, list[httpx.Request]]:
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return TavilySearchProvider(Settings(), http_client=_client(wrapped)), seen


def test_tavily_translates_the_whole_dialect(monkeypatch):
    provider, seen = _tavily(lambda r: httpx.Response(200, json=_tavily_body()), monkeypatch)
    provider.search(SearchQuery(text="定价", sites=("sspai.com",), freshness="month", limit=7))

    payload = json.loads(seen[0].content)
    assert payload["query"] == "定价"
    assert payload["include_domains"] == ["sspai.com"]  # 列表而非 `|` 串
    assert payload["days"] == 30                        # 枚举 → 天数
    assert payload["max_results"] == 7
    assert payload["api_key"] == "tavily-key"           # 密钥在 body 里，不在头上


@pytest.mark.parametrize(
    ("freshness", "days"), [("day", 1), ("week", 7), ("month", 30), ("year", 365)]
)
def test_tavily_freshness_is_expressed_in_days(freshness, days, monkeypatch):
    provider, seen = _tavily(lambda r: httpx.Response(200, json=_tavily_body()), monkeypatch)
    provider.search(SearchQuery(text="x", freshness=freshness))
    assert json.loads(seen[0].content)["days"] == days


def test_tavily_omits_days_for_any(monkeypatch):
    provider, seen = _tavily(lambda r: httpx.Response(200, json=_tavily_body()), monkeypatch)
    provider.search(SearchQuery(text="x", freshness="any"))
    assert "days" not in json.loads(seen[0].content)


def test_tavily_synthesizes_site_name_from_url(monkeypatch):
    """Tavily 不返回站点名，但可信度评分需要它。

    适配器的职责就是让上游拿到的字段完整——而不是让上游到处判空。
    """
    provider, _ = _tavily(lambda r: httpx.Response(200, json=_tavily_body()), monkeypatch)
    hits = provider.search(SearchQuery(text="x"))
    assert hits[0].site_name == "sspai.com"


def test_tavily_limit_clamped_to_capability(monkeypatch):
    provider, seen = _tavily(lambda r: httpx.Response(200, json=_tavily_body()), monkeypatch)
    provider.search(SearchQuery(text="x", limit=100))
    assert json.loads(seen[0].content)["max_results"] == 20


def test_tavily_missing_results_field_is_an_error(monkeypatch):
    """HTTP 200 但没有 results，说明响应结构和预期不符——必须报出来，
    而不是返回空列表让上游以为"这个话题没有资料"。"""
    provider, _ = _tavily(lambda r: httpx.Response(200, json={"answer": "..."}), monkeypatch)
    with pytest.raises(ProviderError):
        provider.search(SearchQuery(text="x"))


def test_tavily_400_is_not_retryable(monkeypatch):
    provider, _ = _tavily(lambda r: httpx.Response(400, text="bad"), monkeypatch)
    with pytest.raises(BadRequest):
        provider.search(SearchQuery(text="x"))


# ============================================================
# 两家的一致性：同一份归一化输入，双方都产出合法的 SearchHit
# ============================================================


def test_both_providers_satisfy_the_same_contract(monkeypatch):
    """这是适配层要保证的核心不变量：上游写一次，换家 provider 结果结构不变。"""
    bocha, _ = _bocha(lambda r: httpx.Response(200, json=_bocha_body()), monkeypatch)
    tavily, _ = _tavily(lambda r: httpx.Response(200, json=_tavily_body()), monkeypatch)

    query = SearchQuery(text="定价", limit=5)
    for provider in (bocha, tavily):
        hits = provider.search(query)
        assert hits, f"{provider.name} 没有返回结果"
        for hit in hits:
            assert hit.url.startswith("https://")
            assert hit.provider == provider.name
            assert isinstance(hit.rank, int)
