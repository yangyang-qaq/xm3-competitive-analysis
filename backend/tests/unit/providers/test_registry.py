"""注册表：选择、构造、以及"没有密钥也能回放"。

`get_settings` 被换成返回固定 Settings 的对象，而不是靠 monkeypatch 环境变量——
`Settings` 是 lru_cache 的，靠环境变量间接影响它会让用例的意图变得难以追踪。
"""
from __future__ import annotations

import json

import pytest

import app.providers.registry as registry
from app.core.config import Settings
from app.providers.cassette import RecordingProvider, ReplayProvider
from app.providers.errors import ProviderNotConfigured
from app.providers.mock import MockLLMProvider


@pytest.fixture
def use_settings(monkeypatch):
    def _apply(**kwargs) -> Settings:
        settings = Settings(**kwargs)
        monkeypatch.setattr(registry, "get_settings", lambda: settings)
        return settings

    return _apply


# ============================================================
# 注册
# ============================================================


def test_builtin_modules_all_register():
    """`_BUILTIN_MODULES` 刻意没有 try/except：模块导入失败必须立刻炸掉，
    而不是让这家 provider 悄悄从可选列表里消失。"""
    assert registry.available_llm() == ["deepseek", "mock", "zhipu"]
    assert registry.available_search() == ["bocha", "mock", "tavily"]
    assert "http" in registry._FETCH
    assert "mock" in registry._FETCH


def test_loading_is_idempotent():
    registry.load_builtin_providers()
    registry.load_builtin_providers()
    assert registry.available_llm() == ["deepseek", "mock", "zhipu"]


# ============================================================
# 选择
# ============================================================


def test_unknown_provider_lists_the_alternatives(use_settings):
    """报错信息要能直接指出下一步。只说"未注册"的话，还得回去翻代码找可用名。"""
    use_settings(llm_provider="nonexistent")
    with pytest.raises(KeyError) as excinfo:
        registry.get_llm()
    message = str(excinfo.value)
    assert "nonexistent" in message
    assert "deepseek" in message


def test_mock_is_selectable_like_any_other_provider(use_settings):
    """mock 不是"测试替身"，是一等实现——CLI 的 --provider mock 走的就是这条路。"""
    use_settings(llm_provider="mock", search_provider="mock", fetch_provider="mock")
    assert isinstance(registry.get_llm(), MockLLMProvider)
    assert registry.get_search().name == "mock"


def test_selection_is_cached(use_settings):
    """provider 持有连接池，每次调用都新建会退化成"每条请求一个连接"。"""
    use_settings(llm_provider="mock")
    assert registry.get_llm() is registry.get_llm()


def test_reset_clears_the_cache(use_settings):
    use_settings(llm_provider="mock")
    first = registry.get_llm()
    registry.reset_providers()
    assert registry.get_llm() is not first


# ============================================================
# 缺密钥时的行为
# ============================================================


def test_missing_key_raises_without_a_fallback(use_settings, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    use_settings(llm_provider="deepseek")
    with pytest.raises(ProviderNotConfigured):
        registry.get_llm()


def test_missing_primary_key_falls_back_to_the_secondary(use_settings, monkeypatch):
    """配了备用却因为主密钥缺失而全线不可用，很荒唐。

    这是"熔断"最该覆盖的场景——它甚至不需要发生任何一次失败的调用。
    """
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    use_settings(llm_provider="deepseek", llm_fallback="mock")
    assert registry.get_llm().name == "mock"


def test_unconfigured_secondary_is_simply_unavailable(use_settings, monkeypatch):
    """备用没配密钥只该丢下降级能力，不该让服务起不来。"""
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    use_settings(llm_provider="mock", llm_fallback="zhipu")
    assert registry.get_llm().name == "mock"


def test_both_configured_builds_a_fallback(use_settings, monkeypatch):
    """两边都配好时，降级链真的被组起来。

    `setenv` 那一行是后补的，补的理由值得记（问题 55）：这条用例早先
    靠的是**这台机器上有 `backend/.env`**。于是它在开发机上一直绿，
    在克隆出来的目录里红，而 CI（没有 `.env`）从第一次跑就会红。
    用例要问的是"配了两个 provider 会不会组链"，与"这台机器的环境里
    恰好有没有钥匙"无关——所以钥匙必须由用例自己给。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-a-real-one")
    use_settings(llm_provider="mock", llm_fallback="deepseek")
    provider = registry.get_llm()
    assert provider.name == "mock>deepseek"


# ============================================================
# 与 cassette 的组合
# ============================================================


def test_replay_needs_no_api_key(use_settings, monkeypatch, tmp_path):
    """CI 里没有密钥是常态。回放必须先构造真实适配器的话，
    等于要求"离线测试"得先有线上凭据。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    use_settings(cassette_mode="replay", cassette_dir=str(tmp_path), llm_provider="deepseek")

    provider = registry.get_llm()
    assert isinstance(provider, ReplayProvider)
    assert provider.name == "deepseek"


def test_replay_skips_the_fallback_wrapper(use_settings, monkeypatch, tmp_path):
    """两条路都只会抛 CassetteMiss，让第二个盖掉第一个只会更难看出缺的是哪条录制。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    use_settings(
        cassette_mode="replay",
        cassette_dir=str(tmp_path),
        llm_provider="deepseek",
        llm_fallback="mock",
    )
    assert isinstance(registry.get_llm(), ReplayProvider)


def test_record_mode_wraps_every_provider_kind(use_settings, tmp_path):
    use_settings(
        cassette_mode="record",
        cassette_dir=str(tmp_path),
        llm_provider="mock",
        search_provider="mock",
        fetch_provider="mock",
    )
    assert isinstance(registry.get_llm(), RecordingProvider)
    assert isinstance(registry.get_search(), RecordingProvider)
    assert isinstance(registry.get_fetcher(), RecordingProvider)


def test_cassette_filenames_use_machine_kinds_not_labels(use_settings, tmp_path):
    """`kind` 会进文件名。

    早先它同时承担了"给人看的标签"和"机器用的键"两个角色，
    于是文件名变成 `搜索.bocha.jsonl`——跨平台、跨编码都难受。
    """
    from app.providers.base import ChatMessage

    use_settings(cassette_mode="record", cassette_dir=str(tmp_path), llm_provider="mock")
    registry.get_llm().chat([ChatMessage("user", "hi")])

    assert [p.name for p in tmp_path.glob("*.jsonl")] == ["llm.mock.jsonl"]


# ============================================================
# /api/providers 的数据源
# ============================================================


def test_describe_all_lists_every_registered_provider(use_settings, monkeypatch):
    """这张表的价值在于"我有哪几家可选、各自支持什么、哪家还没配密钥"，
    所以它必须列出全部，而不只是当前生效的那个。"""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    use_settings(llm_provider="deepseek", search_provider="bocha")

    rows = registry.describe_all()
    index = {(row["kind"], row["name"]): row for row in rows}

    assert ("llm", "deepseek") in index
    assert ("llm", "zhipu") in index
    assert ("llm", "mock") in index
    assert ("search", "bocha") in index
    assert ("search", "tavily") in index

    assert index[("llm", "deepseek")]["active"] is True
    assert index[("llm", "zhipu")]["active"] is False
    assert index[("llm", "zhipu")]["configured"] is False
    assert index[("search", "tavily")]["configured"] is False


def test_describe_all_reports_capabilities_and_models(use_settings, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_MODEL_CORE", "my-core-model")
    # 仓库 .env 里配了 aux/fast，这里显式清掉才能测"未配置"的分支
    monkeypatch.delenv("DEEPSEEK_MODEL_AUX", raising=False)
    use_settings(llm_provider="deepseek")

    row = next(r for r in registry.describe_all() if r["name"] == "deepseek")
    assert row["capabilities"]["json_mode"] is True
    assert row["models"]["core"] == "my-core-model"
    # 没配的档位是空字符串，而不是编一个默认模型名出来
    assert row["models"]["aux"] == ""


def test_describe_all_never_leaks_the_api_key(use_settings, monkeypatch):
    """这是能直接印在页面上的数据。带着密钥就等于把它贴到浏览器里了。"""
    secret = "sk-super-secret-value"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    use_settings(llm_provider="deepseek")

    assert secret not in json.dumps(registry.describe_all(), ensure_ascii=False)


def test_describe_all_survives_a_broken_provider(use_settings, monkeypatch, tmp_path):
    """某家 provider 构造失败不该让整张能力表打不开——
    恰恰是它坏掉的时候，你才最想看这张表。"""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    use_settings(llm_provider="deepseek", search_provider="bocha")

    rows = registry.describe_all()
    tavily = next(r for r in rows if r["name"] == "tavily")
    assert tavily["configured"] is False
    assert tavily["capabilities"] == {}  # 构造不起来就没有能力可报，但仍在列表里
