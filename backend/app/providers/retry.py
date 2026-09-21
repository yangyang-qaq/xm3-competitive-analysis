"""退避重试。

**只有一处实现，所有适配器共用。**

与"在客户端里写个 for 循环"的区别在于：重试策略是可注入、可测试、可观测的。

`sleep` 参数是关键——测试里传一个记账用的假函数，就能断言"重试了 3 次、间隔是
4/8/15 秒"而不用真的等 27 秒。真实等待的测试最后总会被人加上 `@pytest.mark.slow` 然后不再运行。
"""
from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from app.providers.errors import ProviderError

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """退避策略。

    默认值偏保守（4/8/15/25 秒）：调研流水线一次任务会打几十上百次请求，
    与其在限流上快速失败，不如让单次调用慢一点——总耗时反而更短。
    """

    backoffs: tuple[float, ...] = (4.0, 8.0, 15.0, 25.0)
    """每次重试前的等待秒数。长度为 N 表示最多重试 N 次（总尝试 N+1 次）。"""

    respect_retry_after: bool = True
    """provider 明确给了 Retry-After 时，用它覆盖策略里的等待时间。"""

    @property
    def max_attempts(self) -> int:
        return len(self.backoffs) + 1


#: 不重试：一次调用只有一次机会。用于对延迟敏感、失败可接受的场合。
NO_RETRY = RetryPolicy(backoffs=())

#: 默认策略。
DEFAULT = RetryPolicy()

#: 抓取网页用。页面挂了重试 4 次没意义，而且会拖慢整个采集阶段。
FETCH = RetryPolicy(backoffs=(1.0, 3.0))


def with_backoff(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy = DEFAULT,
    provider: str = "",
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, float, ProviderError], None] | None = None,
) -> T:
    """执行 `fn`，遇到可重试的 ProviderError 就退避重试。

    只有 `ProviderError.retryable` 为 True 的错误会被重试；其余立即抛出。
    非 ProviderError 的异常同样立即抛出——那是代码缺陷，重试只会掩盖它。

    `on_retry(第几次重试, 本次等待秒数, 错误)` 用于让调用方记录重试行为。
    """
    last_error: ProviderError | None = None
    # 展开成可变列表按索引推进：provider 给的 Retry-After 需要在运行中抬高"下一次"的等待，
    # 而 for 循环遍历的元组在进入循环时就固定了，改不动。
    delays: list[float] = [0.0, *policy.backoffs]
    attempt = 0

    while attempt < len(delays):
        delay = delays[attempt]
        if delay > 0:
            sleep(delay)
        try:
            return fn()
        except ProviderError as exc:
            if not exc.retryable:
                raise
            last_error = exc
            attempt += 1
            remaining = len(delays) - attempt
            if remaining <= 0:
                break
            log.warning(
                "%s 第 %d 次尝试失败（还可重试 %d 次）：%s",
                provider or exc.provider or "provider",
                attempt,
                remaining,
                exc,
            )
            if on_retry is not None:
                on_retry(attempt - 1, delay, exc)

            hint = getattr(exc, "retry_after", None) if policy.respect_retry_after else None
            if hint:
                # provider 给了个非数值的 Retry-After 就按策略里的等待来
                with contextlib.suppress(TypeError, ValueError):
                    delays[attempt] = max(delays[attempt], float(hint))

    assert last_error is not None
    raise last_error
