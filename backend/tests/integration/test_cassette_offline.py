"""阶段 1 的验收：录制 → 回放往返，且回放**不需要任何凭据**。

这是"真实流水线可以离线测"这条主张的证据。单测里已经验过 cassette 组件本身，
这里验的是**经过注册表的完整路径**——也就是流水线将来实际走的那个入口。

为什么这条测试值得单独写：如果回放路径要求有密钥，CI 里就跑不起来，
于是"零成本回归"会退化成"本地偶尔跑一下"，而这正是要避免的。
"""
from __future__ import annotations

import pytest

import app.providers.registry as registry
from app.core.config import Settings
from app.providers.base import ChatMessage, SearchQuery
from app.providers.cassette import CassetteMiss
from app.providers.mock import MockFetcher

_API_KEYS = (
    "DEEPSEEK_API_KEY",
    "ZHIPU_API_KEY",
    "BOCHA_API_KEY",
    "TAVILY_API_KEY",
    "SERPER_API_KEY",
)

_MESSAGES = [ChatMessage("user", "对比一下这两个产品的定价差异")]
_QUERY = SearchQuery(text="定价对比", sites=("douyin.com",), freshness="month", limit=4)


@pytest.fixture
def use_settings(monkeypatch):
    def _apply(**kwargs) -> Settings:
        settings = Settings(**kwargs)
        monkeypatch.setattr(registry, "get_settings", lambda: settings)
        return settings

    return _apply


@pytest.fixture
def no_credentials(monkeypatch):
    """把所有密钥从环境里清掉。

    `.env` 里是配了真密钥的，不清掉的话"回放不需要凭据"这条断言等于没测。
    """
    for key in _API_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_full_record_then_replay_cycle_without_any_credentials(tmp_path, use_settings, no_credentials):
    # ---- 第一遍：录制 ----
    use_settings(
        cassette_mode="record",
        cassette_dir=str(tmp_path),
        llm_provider="mock",
        search_provider="mock",
        fetch_provider="mock",
    )
    recorder = registry.get_llm()
    recorded_text = recorder.chat(_MESSAGES, purpose="analyze_claims").text
    recorded_urls = [h.url for h in registry.get_search().search(_QUERY)]
    fetched = registry.get_fetcher().fetch("https://example.com/keep")

    assert (tmp_path / "llm.mock.jsonl").exists()
    assert (tmp_path / "search.mock.jsonl").exists()
    assert (tmp_path / "fetch.mock.jsonl").exists()

    # ---- 第二遍：清掉缓存与凭据，只回放 ----
    registry.reset_providers()
    use_settings(
        cassette_mode="replay",
        cassette_dir=str(tmp_path),
        llm_provider="mock",
        search_provider="mock",
        fetch_provider="mock",
    )

    assert registry.get_llm().chat(_MESSAGES, purpose="analyze_claims").text == recorded_text
    assert [h.url for h in registry.get_search().search(_QUERY)] == recorded_urls
    assert registry.get_fetcher().fetch("https://example.com/keep").text == fetched.text


def test_replay_never_reaches_the_network(tmp_path, use_settings, no_credentials, monkeypatch):
    """回放时若真的发了请求，说明有某条路径绕过了 cassette。

    这里把 httpx 的所有发送方法都换成会抛异常的桩：只要有任何一次真实出网，
    测试立刻失败。比起"看日志里没有报错"，这是唯一能确证的写法。

    注意本用例目前用 mock provider，它们本来就不出网，所以现在它守的是"路径没被绕过"。
    等阶段 3 录下真实 cassette 后，同一条用例才真正开始检查适配器。
    """
    use_settings(
        cassette_mode="record",
        cassette_dir=str(tmp_path),
        llm_provider="mock",
        search_provider="mock",
    )
    registry.get_llm().chat(_MESSAGES)
    registry.get_search().search(_QUERY)
    registry.reset_providers()

    import httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("回放模式下发起了真实网络请求")

    for method in ("send", "request", "get", "post"):
        monkeypatch.setattr(httpx.Client, method, forbidden, raising=False)

    use_settings(
        cassette_mode="replay",
        cassette_dir=str(tmp_path),
        llm_provider="mock",
        search_provider="mock",
    )
    assert registry.get_llm().chat(_MESSAGES).text
    assert registry.get_search().search(_QUERY)


def test_replay_misses_loudly_instead_of_calling_out(tmp_path, use_settings, no_credentials):
    """缺录制必须抛 `CassetteMiss`，绝不静默联网。

    静默联网的危害是**间歇性花钱**——CI 大部分时候是绿的，
    只是账单上多了一笔，没人会把两者联系起来。
    """
    use_settings(
        cassette_mode="replay",
        cassette_dir=str(tmp_path),
        llm_provider="mock",
        search_provider="mock",
    )
    with pytest.raises(CassetteMiss):
        registry.get_llm().chat([ChatMessage("user", "这条从来没录过")])


def test_recordings_are_keyed_by_provider_so_modes_do_not_collide(
    tmp_path, use_settings, no_credentials
):
    """mock 与真实 provider 的录制分文件存放。

    混在一个文件里的话，用 mock 录的假响应会在 CI 里冒充真实响应，
    而"回放通过"就变得毫无意义。
    """
    use_settings(
        cassette_mode="record",
        cassette_dir=str(tmp_path),
        llm_provider="mock",
        search_provider="mock",
    )
    registry.get_llm().chat(_MESSAGES)

    assert [p.name for p in tmp_path.glob("llm.*.jsonl")] == ["llm.mock.jsonl"]


def test_degraded_fetches_survive_the_round_trip(tmp_path, use_settings, no_credentials):
    """降级路径必须能被录下来、回放出来。

    否则"降级率"这类指标在离线测试里永远是 0，而一个恒为 0 的指标等于没有指标——
    更糟的是它会让人以为"我们的抓取从来没失败过"。
    """
    url = next(
        u for u in (f"https://example.com/{i}" for i in range(200)) if MockFetcher.degrades(u)
    )

    use_settings(cassette_mode="record", cassette_dir=str(tmp_path), fetch_provider="mock")
    recorded = registry.get_fetcher().fetch(url, fallback_snippet="搜索摘要")
    assert recorded.degraded is True
    registry.reset_providers()

    use_settings(cassette_mode="replay", cassette_dir=str(tmp_path), fetch_provider="mock")
    replayed = registry.get_fetcher().fetch(url, fallback_snippet="搜索摘要")

    assert replayed.degraded is True
    assert replayed.text == recorded.text
    assert replayed.error == recorded.error
