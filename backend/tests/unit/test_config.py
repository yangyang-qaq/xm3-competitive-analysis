"""配置层测试。

重点验证「凭据按命名约定动态查」这条设计——它是 provider 可插拔的地基。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings, get_settings


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Settings:
    """不读真实 .env 的 Settings，避免测试结果依赖开发者本机配置。"""
    monkeypatch.setenv("XM3_TEST_ISOLATED", "1")
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_defaults_are_provider_neutral(settings: Settings) -> None:
    assert settings.llm_provider == "deepseek"
    assert settings.search_provider == "bocha"
    assert settings.cassette_mode == "off"
    assert settings.app_port == 8020


def test_credentials_read_by_naming_convention(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """给任意 provider 名配上环境变量，就能取到——无需在 config.py 里加字段。"""
    monkeypatch.setenv("ACME_API_KEY", "k-123")
    monkeypatch.setenv("ACME_BASE_URL", "https://api.acme.test/v1")
    monkeypatch.setenv("ACME_MODEL_CORE", "acme-pro")

    cred = settings.credentials("acme")
    assert cred.configured is True
    assert cred.api_key == "k-123"
    assert cred.base_url == "https://api.acme.test/v1"
    assert cred.model_for("core") == "acme-pro"


def test_unconfigured_provider_is_not_configured(settings: Settings) -> None:
    cred = settings.credentials("nobody")
    assert cred.configured is False
    assert cred.api_key == ""


def test_model_falls_back_to_adapter_default(settings: Settings) -> None:
    """档位未配置时必须回落到适配器给的默认值，而不是空字符串。"""
    cred = settings.credentials("nobody")
    assert cred.model_for("core", "adapter-default") == "adapter-default"


def test_describe_never_leaks_keys(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """脱敏自述不能带出 key 本身——这个接口是无鉴权暴露的。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "super-secret-value")
    described = settings.describe()
    assert "super-secret-value" not in str(described)
    assert described["llm_configured"] is True


def test_cors_origins_defaults_to_frontend_origin(settings: Settings) -> None:
    assert settings.cors_origins == ["http://localhost:3500"]


def test_cors_origins_never_wildcard(settings: Settings) -> None:
    """收紧到具体来源。Verda 用的是 allow_origins=["*"]。"""
    assert "*" not in settings.cors_origins


def test_repo_settings_load_without_error() -> None:
    """仓库里的 .env（若存在）必须能被解析——防的是改坏配置文件却无人察觉。"""
    get_settings.cache_clear()
    assert get_settings().app_port > 0
    get_settings.cache_clear()


# ============================================================
# 压测护栏
#
# 这几条守的是 `loadtest/locustfile.py` 的前提条件。它单独看只是几行赋值，
# 但它是**唯一**挡在"20 个并发用户打到真实 provider"前面的东西，
# 而那个事故花的是真钱。所以它值得有自己的测试。
# ============================================================


def test_force_mock_overrides_every_provider(settings: Settings) -> None:
    forced = Settings(  # type: ignore[call-arg]
        _env_file=None,
        force_mock_provider=True,
        llm_provider="deepseek",
        search_provider="bocha",
        fetch_provider="http",
    )
    assert (forced.llm_provider, forced.search_provider, forced.fetch_provider) == (
        "mock",
        "mock",
        "mock",
    )


def test_force_mock_clears_the_fallback() -> None:
    """备用 LLM 必须一起清掉。

    只换主 provider 的话，备用还挂着 zhipu，而它**是会真的被调用的**
    （主 provider 出错时降级）——护栏自己留了个洞。
    """
    forced = Settings(  # type: ignore[call-arg]
        _env_file=None, force_mock_provider=True, llm_fallback="zhipu"
    )
    assert forced.llm_fallback == ""


def test_force_mock_is_off_by_default(settings: Settings) -> None:
    """默认必须是关的。默认打开的话，这个开关就变成"谁都跑不了真 provider"了。"""
    assert settings.force_mock_provider is False
    assert settings.llm_provider == "deepseek"


def test_health_discloses_all_three_providers(settings: Settings) -> None:
    """/health 三家都要报。

    压测拿这份自述当准入条件，缺一家就检查不全；而抓取恰是这套系统
    最主要的降级来源，漏掉它等于漏掉最该防的那个。
    """
    described = settings.describe()
    for key in ("llm_provider", "search_provider", "fetch_provider", "force_mock_provider"):
        assert key in described, f"/health 没有报 {key}"
