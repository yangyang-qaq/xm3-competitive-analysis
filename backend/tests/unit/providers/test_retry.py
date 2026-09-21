"""退避重试的行为。

全部注入假 `sleep`——真实等待的测试最后一定会被标成 slow 然后没人跑。
所以这里断言的是**重试的次数与间隔序列**，而不是"等了一会儿之后成功了"。
"""
from __future__ import annotations

import pytest

from app.providers.errors import BadRequest, ProviderError, RateLimited, Transient
from app.providers.retry import NO_RETRY, RetryPolicy, with_backoff


class _Recorder:
    """记录 sleep 调用，不真的睡。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def test_retries_until_success():
    sleeps = _Recorder()
    calls = 0

    def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise Transient("网络抖动")
        return "ok"

    assert with_backoff(fn, policy=RetryPolicy((1.0, 2.0, 5.0)), sleep=sleeps) == "ok"
    assert calls == 3
    # 第一次尝试不等；重试前分别等 1s、2s
    assert sleeps.delays == [1.0, 2.0]


def test_non_retryable_raises_without_sleeping():
    """400 重试一次还是 400。重试它只会浪费一次往返。"""
    sleeps = _Recorder()

    def fn():
        raise BadRequest("参数不合法")

    with pytest.raises(BadRequest):
        with_backoff(fn, policy=RetryPolicy((1.0, 2.0)), sleep=sleeps)
    assert sleeps.delays == []


def test_exhausting_retries_reraises_last_error():
    sleeps = _Recorder()
    calls = 0

    def fn():
        nonlocal calls
        calls += 1
        raise RateLimited("一直被限流")

    with pytest.raises(RateLimited):
        with_backoff(fn, policy=RetryPolicy((1.0,)), sleep=sleeps)
    # 1 次初始 + 1 次重试
    assert calls == 2
    assert sleeps.delays == [1.0]


def test_no_retry_policy_makes_exactly_one_call():
    calls = 0

    def fn():
        nonlocal calls
        calls += 1
        raise Transient("瞬时故障")

    with pytest.raises(Transient):
        with_backoff(fn, policy=NO_RETRY, sleep=_Recorder())
    assert calls == 1


def test_retry_after_extends_the_next_wait():
    """provider 明确说了"30 秒后再来"，就不该按策略里的 1 秒去撞。

    这是 `with_backoff` 用可变列表而不是展开元组的原因：
    元组在进入循环时就固定了，改不动。
    """
    sleeps = _Recorder()
    calls = 0

    def fn():
        nonlocal calls
        calls += 1
        raise RateLimited("慢一点", retry_after=30.0)

    with pytest.raises(RateLimited):
        with_backoff(fn, policy=RetryPolicy((1.0,)), sleep=sleeps)
    assert calls == 2
    assert sleeps.delays == [30.0]


def test_retry_after_can_be_ignored():
    sleeps = _Recorder()

    def fn():
        raise RateLimited("慢一点", retry_after=30.0)

    with pytest.raises(RateLimited):
        with_backoff(
            fn,
            policy=RetryPolicy((1.0,), respect_retry_after=False),
            sleep=sleeps,
        )
    assert sleeps.delays == [1.0]


def test_non_provider_error_propagates_immediately():
    """KeyError 是代码缺陷，不是"provider 抽风"。重试只会让 bug 晚几分钟暴露。"""
    sleeps = _Recorder()

    def fn():
        raise KeyError("拼错的字段名")

    with pytest.raises(KeyError):
        with_backoff(fn, policy=RetryPolicy((1.0, 2.0)), sleep=sleeps)
    assert sleeps.delays == []


def test_on_retry_reports_each_attempt():
    seen: list[tuple[int, float, str]] = []

    def fn():
        raise Transient("boom")

    with pytest.raises(Transient):
        with_backoff(
            fn,
            policy=RetryPolicy((4.0, 8.0)),
            sleep=_Recorder(),
            on_retry=lambda index, delay, exc: seen.append((index, delay, type(exc).__name__)),
        )
    assert seen == [(0, 0.0, "Transient"), (1, 4.0, "Transient")]


def test_policy_attempt_count():
    assert RetryPolicy(()).max_attempts == 1
    assert RetryPolicy((1.0, 2.0)).max_attempts == 3
    # 任何 ProviderError 都是 Exception 的子类，别在异常体系上搞出岔路
    assert issubclass(Transient, ProviderError)
