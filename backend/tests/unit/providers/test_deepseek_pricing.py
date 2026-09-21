"""DeepSeek 的定价表：分时段、缓存命中价、汇率折算。

这三件事每一件都能让成本表错一倍以上，而错的成本指标比没有成本指标更糟——
它会让人据此做决策（"换更便宜的档位"、"这个任务不值得跑"）。
所以它们必须是被测过的，不能只靠读注释相信。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.core.config import Settings
from app.providers.llm.deepseek import (
    _DEFAULT_FX_CNY_PER_USD,
    _IDLE_PRICES_CNY,
    _PEAK_PRICES_CNY,
    DeepSeekProvider,
    _fx_rate,
    _is_peak,
)

CST = timezone(timedelta(hours=8))


@pytest.fixture
def provider(monkeypatch):
    """一个配好了三档模型的 DeepSeek provider（三档都是 `deepseek-flash`）。

    凭据是 `_env()` 现读 `os.environ` 的，所以 `monkeypatch.setenv` 有效。
    **不出网**：只是构造对象并读定价表。

    注意档位配置是在 `__init__` 里快照进 `_cred` 的，所以**想改档位必须重新构造**，
    构造完再 `setenv` 是无效的——见 `make()`。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key-for-pricing-test")
    monkeypatch.setenv("DEEPSEEK_MODEL_CORE", "deepseek-flash")
    monkeypatch.setenv("DEEPSEEK_MODEL_AUX", "deepseek-flash")
    monkeypatch.setenv("DEEPSEEK_MODEL_FAST", "deepseek-flash")
    return DeepSeekProvider(Settings())


@pytest.fixture
def make(monkeypatch):
    """改了档位之后重新构造一个 provider。"""

    def _make(**models: str) -> DeepSeekProvider:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key-for-pricing-test")
        for tier in ("core", "aux", "fast"):
            monkeypatch.setenv(
                f"DEEPSEEK_MODEL_{tier.upper()}", models.get(tier, "deepseek-flash")
            )
        return DeepSeekProvider(Settings())

    return _make


# ============================================================
# 时段判断
# ============================================================


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (9, 0, True),  # 高峰区间左端，闭
        (10, 0, True),
        (11, 59, True),
        (12, 0, False),  # 右端，开
        (13, 0, False),  # 午休
        (14, 0, True),
        (17, 59, True),
        (18, 0, False),  # 右端，开
        (20, 0, False),
        (3, 0, False),  # 凌晨
    ],
)
def test_weekday_peak_windows(hour, minute, expected):
    """周三的各时刻。区间是 `[9:00, 12:00)` 与 `[14:00, 18:00)`。

    端点归属刻意是"左闭右开"：公布的只有"9:00-12:00"这个说法，而两倍价差
    经不起含糊，所以选一个确定的解释并用测试钉住。
    """
    now = datetime(2026, 9, 16, hour, minute, tzinfo=CST)  # 2026-09-16 是周三

    assert _is_peak(now) is expected


@pytest.mark.parametrize("day", [19, 20])  # 周六、周日
def test_weekends_are_never_peak(day):
    """周末全天按空闲计价。周六上午十点也**不是**高峰——
    这条错了会让周末跑的报告成本翻倍，而周末恰好是个人项目最常跑的时候。
    """
    assert _is_peak(datetime(2026, 9, day, 10, 0, tzinfo=CST)) is False


def test_peak_is_judged_in_beijing_time_not_local_time():
    """按**服务方的本地时间**判断，不是本机时间。

    北京时间 10:00 对应 UTC 02:00。一个把机器时区设成 UTC 的人，
    如果按本机时间算，会在每天最贵的四小时里拿到空闲价——
    而且是系统性的、看不出来的偏低。
    """
    beijing_ten = datetime(2026, 9, 16, 10, 0, tzinfo=CST)
    same_moment_in_utc = beijing_ten.astimezone(UTC)

    assert same_moment_in_utc.hour == 2, "前提：北京时间 10 点就是 UTC 2 点"
    assert _is_peak(beijing_ten) is True
    assert _is_peak(same_moment_in_utc) is True, "同一个时刻，换个时区表示不该改变结论"


# ============================================================
# 价目表
# ============================================================


def test_peak_prices_are_exactly_twice_the_idle_prices():
    """公布口径就是"高峰价为空闲价的 2 倍"。

    这条断言用公布的**关系**而不是逐个数字去核对，于是改价时只要关系不变
    就不用改测试，而关系一变就会红。
    """
    assert tuple(p * 2 for p in _IDLE_PRICES_CNY) == _PEAK_PRICES_CNY


def test_cached_input_is_fifty_times_cheaper_than_a_miss():
    """命中与未命中的输入价差 50 倍。

    所以命中率一旦上去，成本就由这一项主导；反过来，实测这个流水线的命中率
    只有 0.8%（见 `TokenUsage` 的 docstring），眼下这一项的权重还很小。
    价差是**价目表的事实**，命中率是**负载的事实**，两者都要单独钉住。
    """
    cached, miss, _ = _IDLE_PRICES_CNY

    assert miss / cached == 50


def test_only_flash_series_models_get_a_price(provider):
    """只给 flash 系列报价。reasoner 之类的价格完全不同，
    套用 flash 价会让成本偏低——而偏低的成本不会有人去复查。
    """
    assert set(provider.pricing()) == {"deepseek-flash"}


def test_a_reasoner_model_is_left_unpriced(make):
    """把某档换成 reasoner，它**不该**出现在定价表里。

    没价 → 成本记 0 并显示"未配置定价"，这是一个看得见的缺失；
    给个错的价 → 成本看起来正常但其实是错的。前者可发现，后者不可。
    """
    table = make(core="deepseek-reasoner", aux="deepseek-flash", fast="deepseek-flash").pricing()

    assert "deepseek-reasoner" not in table
    assert set(table) == {"deepseek-flash"}


def test_the_public_flash_names_are_recognised(make):
    """公开渠道的 flash 模型名也要有价，包括带后缀的变体。"""
    table = make(core="deepseek-v4-flash", aux="deepseek-v4-flash-vision-exp").pricing()

    assert "deepseek-v4-flash" in table
    assert "deepseek-v4-flash-vision-exp" in table


def test_every_configured_tier_shares_one_price(make):
    """flash 系列各变体执行同一份价目表，所以三档同价。

    三档配成同一个名字时只该出现一条——不是三条一样的记录。
    """
    table = make(core="deepseek-flash", aux="deepseek-flash", fast="deepseek-flash").pricing()

    assert set(table) == {"deepseek-flash"}


def test_the_effective_rate_matches_the_current_window(provider):
    """价目表里填的是**当前时段**那一组。

    不直接断言"现在是高峰"，因为测试跑在几点是不确定的——
    断言的是它和当前时刻的判断一致。
    """
    peak_now = _is_peak(datetime.now(CST))
    expected_miss = (_PEAK_PRICES_CNY if peak_now else _IDLE_PRICES_CNY)[1]
    price = provider.pricing()["deepseek-flash"]

    assert price.input_per_mtok_usd == pytest.approx(expected_miss / _DEFAULT_FX_CNY_PER_USD)


def test_prices_are_converted_from_cny_and_the_source_is_recorded(provider):
    """归一化成美元是为了跨 provider 可比，但**原始货币与原始价必须留痕**——
    否则没人能核对这个美元数是怎么来的。
    """
    cached, miss, out = (
        _PEAK_PRICES_CNY if _is_peak(datetime.now(CST)) else _IDLE_PRICES_CNY
    )
    price = provider.pricing()["deepseek-flash"]

    assert price.source_currency == "CNY"
    assert price.source_price_per_mtok == (miss, out)
    assert price.input_per_mtok_usd == pytest.approx(miss / _DEFAULT_FX_CNY_PER_USD)
    assert price.cached_input_per_mtok_usd == pytest.approx(cached / _DEFAULT_FX_CNY_PER_USD)
    assert "2026-09-10" in price.note, "note 要带上生效日期，否则没人知道这个价何时有效"


# ============================================================
# 汇率
# ============================================================


def test_the_default_fx_rate_is_the_recorded_one():
    """汇率是一个**有日期的**事实，不是估算。这个数写错了，所有人民币
    计价 provider 的成本会一起错，而且错得很均匀（因此很难被发现）。
    """
    assert pytest.approx(6.7670) == _DEFAULT_FX_CNY_PER_USD


def test_fx_can_be_overridden(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_FX_CNY_PER_USD", "7.0")

    assert _fx_rate() == pytest.approx(7.0)


@pytest.mark.parametrize("bad", ["abc", "0", "-1", ""])
def test_a_bad_fx_rate_falls_back_instead_of_raising(monkeypatch, bad):
    """一个打错的汇率不该让整条流水线跑不出报告，但也不能静默接受。

    退回默认值 + 一条日志：报告照出，数字仍然是一个有出处的数。
    """
    monkeypatch.setenv("DEEPSEEK_FX_CNY_PER_USD", bad)

    assert _fx_rate() == pytest.approx(_DEFAULT_FX_CNY_PER_USD)


def test_the_env_override_wins_over_the_builtin_table(monkeypatch, provider):
    """`<PROVIDER>_PRICING` 覆盖内置价——定价会变，不该为了改价改代码。"""
    monkeypatch.setenv(
        "DEEPSEEK_PRICING", '{"deepseek-flash": [0.1, 0.5, 0.002]}'
    )

    price = provider.pricing()["deepseek-flash"]

    assert price.input_per_mtok_usd == pytest.approx(0.1)
    assert price.output_per_mtok_usd == pytest.approx(0.5)
    assert price.cached_input_per_mtok_usd == pytest.approx(0.002)
    assert "DEEPSEEK_PRICING" in price.note


def test_a_two_number_env_entry_means_no_cached_rate(monkeypatch, provider):
    """只给两个数 = 没配缓存价，读出来是 `None`（按未命中价计，上界）。

    **不是 0**：0 是一个真实的单价（缓存可能不计费），两者差 50 倍。
    """
    monkeypatch.setenv("DEEPSEEK_PRICING", '{"deepseek-flash": [0.1, 0.5]}')

    assert provider.pricing()["deepseek-flash"].cached_input_per_mtok_usd is None


def test_cached_hit_tokens_are_read_from_the_vendor_field():
    """命中数在这家报在 `usage.prompt_cache_hit_tokens`，不是 OpenAI 的
    `prompt_tokens_details.cached_tokens`。读错位置的话恒为 0，
    成本静默偏高四成，而界面上一片正常。
    """

    class _Usage:
        prompt_tokens = 1245
        prompt_cache_hit_tokens = 1024

    assert DeepSeekProvider._cached_prompt_tokens(None, _Usage()) == 1024


def test_a_response_without_the_cache_field_reports_zero():
    """别家形状的响应（或这家没开缓存）读不到就给 0。

    0 会让这一条按未命中价计——成本偏高，但不是编的。
    """

    class _Usage:
        prompt_tokens = 100

    assert DeepSeekProvider._cached_prompt_tokens(None, _Usage()) == 0
