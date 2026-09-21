"""错误分类。这层决定了"要不要重试"，所以每条都要钉死。"""
from __future__ import annotations

import httpx
import pytest

from app.providers.cassette import CassetteMiss
from app.providers.errors import (
    AuthFailed,
    BadRequest,
    HarnessError,
    NotFound,
    ProviderError,
    QuotaExhausted,
    RateLimited,
    Transient,
    classify_httpx,
    classify_status,
    parse_retry_after,
    should_degrade,
)


@pytest.mark.parametrize(
    ("status", "expected", "retryable"),
    [
        (400, BadRequest, False),
        (401, AuthFailed, False),
        (402, QuotaExhausted, False),
        (403, AuthFailed, False),
        (404, NotFound, False),
        (408, Transient, True),
        (429, RateLimited, True),
        (500, Transient, True),
        (502, Transient, True),
        (503, Transient, True),
    ],
)
def test_status_mapping(status, expected, retryable):
    error = classify_status(status, provider="p")
    assert isinstance(error, expected)
    assert error.retryable is retryable
    assert error.provider == "p"


def test_unknown_status_falls_back_to_base_error():
    """没见过的状态码保守处理：不重试。宁可不重试，也不要对 4xx 反复冲击。"""
    error = classify_status(418)
    assert type(error) is ProviderError
    assert error.retryable is False


def test_429_carries_retry_after():
    error = classify_status(429, retry_after=12.0)
    assert isinstance(error, RateLimited)
    assert error.retry_after == 12.0


def test_rate_limit_is_the_only_4xx_worth_retrying():
    """限流会自愈，认证失败不会。这条区分是重试策略的全部依据。"""
    assert classify_status(429).retryable is True
    assert classify_status(401).retryable is False
    assert classify_status(402).retryable is False


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("超时"),
        httpx.ReadTimeout("读超时"),
        httpx.ConnectError("连不上"),
        httpx.RemoteProtocolError("对端断开"),
    ],
)
def test_transport_errors_are_transient(exc):
    error = classify_httpx(exc, provider="p")
    assert isinstance(error, Transient)
    assert error.retryable is True


def test_other_exceptions_are_not_retryable():
    error = classify_httpx(ValueError("解码失败"), provider="p")
    assert type(error) is ProviderError
    assert error.retryable is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("3", 3.0), ("0", 0.0), (" 12.5 ", 12.5), ("-1", None), ("abc", None), ("", None), (None, None)],
)
def test_parse_retry_after(raw, expected):
    assert parse_retry_after(raw) == expected


def test_error_str_prefixes_provider():
    assert str(AuthFailed("密钥无效", provider="bocha")) == "[bocha] 密钥无效"
    assert str(AuthFailed("密钥无效")) == "密钥无效"


# ============================================================
# should_degrade：唯一决定"该不该降级继续"的地方
# ============================================================
#
# 这条规则是从一次真实的假绿里长出来的，见 `HarnessError` 的 docstring。


def test_可选调用遇到运行时故障时降级():
    """这是降级逻辑存在的理由：一个可选步骤挂了不该毁掉整份报告。"""
    assert should_degrade(RateLimited("限流", provider="p"), required=False) is True
    assert should_degrade(Transient("超时", provider="p"), required=False) is True


def test_必需调用任何情况下都不降级():
    assert should_degrade(Transient("超时", provider="p"), required=True) is False


def test_夹具缺失即使可选也必须抛出():
    """**这条是那次假绿的回归测试。**

    改了调度提示词 → 那条 dispatch 录制失效 → `CassetteMiss`。
    那处调用是 `required=False`，于是异常被吞、拿到空 payload、
    三个角色整类退回默认队伍——报告照常产出，闸门报"12 项契约全绿"。
    **绿着闸门，48 人动态组队整个没生效。**

    所以 `HarnessError` 的语义是"这次运行依赖的东西本身是坏的"，
    它不是"这次运气不好"；对它的正确反应永远是停下来。
    """
    assert should_degrade(CassetteMiss("回放缺失", provider="deepseek"), required=False) is False


def test_夹具错误是_provider_错误的一种_但不是运行时故障():
    """继承关系是有意的：它长在 provider 边界上，要和别的错误一起被捕获。

    区分它们的是 `should_degrade`，不是类型系统——所以这条测试钉的是
    "两者都进同一个 except，但走向不同的分支"。
    """
    assert issubclass(CassetteMiss, HarnessError)
    assert issubclass(HarnessError, ProviderError)


def test_普通错误不会被误判成夹具错误():
    """反向：如果判宽了，真正的运行时故障会变成硬失败，
    生产的健壮性就没了——两个方向都得钉。
    """
    for exc in (RateLimited("x", provider="p"), Transient("x", provider="p")):
        assert not isinstance(exc, HarnessError)
