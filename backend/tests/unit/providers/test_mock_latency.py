"""`MOCK_LATENCY_MS` 这个开关到底接没接上。

为什么值得为一条 sleep 写测试
------------------------------
mock 快到 0.2 秒跑完一整个 quick 任务，于是**一切跟时间有关的行为都测不了**：
浏览器断网 10 秒再重连、SSE 心跳、并发订阅的窗口，全都来不及发生。
把 mock 调慢是让这些变成可复现场景的唯一办法（也是零成本的唯一办法）。

而"加一个开关"恰恰是最容易假绿的一类改动：字段声明在 `Settings` 里、
文档里也写了，而**三个 provider 的调用点一个都没读它**——表现和没加完全一样，
且现有的任何一条测试都不会红。所以下面逐个调用点验，而不是验"配置读得出来"。

断言只取下界
------------
`time.sleep` 保证不早于请求的时长返回（PEP 475 起 EINTR 会被重试），
所以"至少等了这么久"是稳的。反过来断言"默认没等待"要靠墙上时钟的**上界**，
在负载高的机器上会偶发——偶发失败的测试最后一定会被跳过，等于没写。
默认值这一条改用"字段声明的默认就是 0"来验，那是确定性的。
"""
from __future__ import annotations

import time

import pytest

from app.core.config import Settings
from app.providers.base import ChatMessage, SearchQuery
from app.providers.errors import Transient
from app.providers.mock import MockFetcher, MockLLMProvider, MockSearchProvider

#: 每次调用等待的毫秒数。取 50 是因为它远大于计时误差、又小到
#: 七八条断言加起来只有几百毫秒——测试套件不该被这条守卫拖慢。
LATENCY_MS = 50

#: 允许的误差。`sleep` 不会早退，所以这只是为了不依赖计时器的实现细节。
FLOOR = LATENCY_MS / 1000.0 * 0.9

_MESSAGES = [ChatMessage("user", "对比一下这两家的定价")]


def _elapsed(call) -> float:
    start = time.perf_counter()
    call()
    return time.perf_counter() - start


def _elapsed_until_raises(call, exc: type[BaseException]) -> float:
    """计时一次**必定抛错**的调用。

    不能写成 `elapsed = _elapsed(...)` 再断言——那句赋值在 `with` 块里，
    异常抛出时它根本没执行完，拿到的是 `UnboundLocalError`，
    于是"没等待"和"真的抛了错"这两个完全不同的原因会报成同一个错。
    """
    start = time.perf_counter()
    with pytest.raises(exc):
        call()
    return time.perf_counter() - start


@pytest.fixture
def slow() -> Settings:
    return Settings(mock_latency_ms=LATENCY_MS)


# ============================================================
# 三个调用点各验一次
#
# 分成三条而不是一条，是因为它们是**三处独立的代码**，谁漏了都不会被
# 另外两条发现。
# ============================================================


def test_LLM_补全走这个开关(slow: Settings) -> None:
    llm = MockLLMProvider(slow)
    elapsed = _elapsed(lambda: llm.chat(_MESSAGES, purpose="analyze"))
    assert elapsed >= FLOOR, f"chat 只用了 {elapsed * 1000:.1f}ms，开关没接上"


def test_搜索走这个开关(slow: Settings) -> None:
    search = MockSearchProvider(slow)
    elapsed = _elapsed(lambda: search.search(SearchQuery(text="笔记软件 定价")))
    assert elapsed >= FLOOR, f"search 只用了 {elapsed * 1000:.1f}ms，开关没接上"


def test_抓取走这个开关(slow: Settings) -> None:
    fetcher = MockFetcher(slow)
    elapsed = _elapsed(lambda: fetcher.fetch("https://example.com/pricing"))
    assert elapsed >= FLOOR, f"fetch 只用了 {elapsed * 1000:.1f}ms，开关没接上"


# ============================================================
# 失败路径同样等待
#
# 这一条守的是等待与失败判断的**先后**。放到失败判断之后的话，
# mock 一调慢，成功路径慢、失败路径瞬回——而返工、降级这些分支
# 恰恰是被失败路径驱动的，测出来的时序就是假的。
# ============================================================


def test_LLM_失败的那次调用也一样等待(slow: Settings) -> None:
    llm = MockLLMProvider(slow)
    llm.fail_purposes.add("analyze")

    elapsed = _elapsed_until_raises(
        lambda: llm.chat(_MESSAGES, purpose="analyze"), Transient
    )
    assert elapsed >= FLOOR, "失败比成功快一个量级，说明等待被放到了失败判断之后"


def test_搜索失败的那次调用也一样等待(slow: Settings) -> None:
    search = MockSearchProvider(slow)
    search.fail_queries.add("笔记软件 定价")

    elapsed = _elapsed_until_raises(
        lambda: search.search(SearchQuery(text="笔记软件 定价")), Transient
    )
    assert elapsed >= FLOOR, "失败的 search 没有等待"


# ============================================================
# 默认必须是不等待
# ============================================================


def test_默认不等待() -> None:
    """默认值必须是 0，否则**整个测试套件**会凭空慢下来。

    一次 quick 运行的 provider 调用数以百计，默认值只要不是 0，
    单元测试、集成测试、e2e 全都要陪着等，而症状只是"测试变慢了"——
    没人会因此去查 mock。
    """
    field = Settings.model_fields["mock_latency_ms"]
    assert field.default == 0


def test_不传_settings_时也不等待() -> None:
    """不传 settings 的构造方式（大量测试这么用）必须仍然是快的。"""
    llm = MockLLMProvider()
    assert _elapsed(lambda: llm.chat(_MESSAGES, purpose="analyze")) < 1.0
