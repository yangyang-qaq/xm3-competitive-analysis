"""连通性探测。

`/api/providers/health` 是这套系统里唯一"会花钱的只读端点"，所以它自己
必须是被测过的。这里测四件事：

1. `run_probe` 的整理规则——尤其是**非 `ProviderError` 的异常也要变成结果**，
   而不是从探测里冒出去
2. 每个注册的 provider 都真的实现了 `probe()`（协议满足 + 探测结果是绿的）
3. 探测目标本身是有效的——`PROBE_URL` 若换成一个正文太短或恰好命中 mock
   降级规则的页面，探测会**恒红**，而故障并不存在
4. 包装层（降级、录制、回放）各自的探测语义
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.providers import registry
from app.providers.base import PROBE_URL, LLMCapabilities, ProbeResult, run_probe
from app.providers.cassette import record, replay
from app.providers.errors import ProviderError, Transient
from app.providers.fallback import FallbackLLM
from app.providers.mock import MockFetcher, MockLLMProvider, MockSearchProvider

# ============================================================
# run_probe
# ============================================================


def test_success_carries_the_detail_and_a_duration():
    result = run_probe(lambda: "一切正常")

    assert result.ok is True
    assert result.detail == "一切正常"
    assert result.error == ""
    assert result.latency_ms >= 0


def test_provider_error_becomes_a_result_not_an_exception():
    def boom() -> str:
        raise Transient("连接失败", provider="x")

    result = run_probe(boom)

    assert result.ok is False
    assert result.error == "Transient"
    assert "连接失败" in result.detail


def test_a_plain_exception_is_reported_rather_than_propagated():
    """适配器里的一个 `KeyError` 同样是"不能用"，而且那是最该被看见的一种失败。

    这里刻意断言它**不**冒出去：健康检查端点抛 500 的话，调用方拿不到任何
    关于这家 provider 的信息，比一条写着 `KeyError` 的结果差得多。
    """

    def boom() -> str:
        raise KeyError("choices")

    result = run_probe(boom)

    assert result.ok is False
    assert result.error == "KeyError"
    assert result.detail  # 非空：至少让人知道抛的是什么


def test_an_exception_with_no_message_still_has_a_detail():
    """有些异常 `str()` 是空串（比如裸 `TimeoutError()`）。

    `detail` 为空的话前端那里就是一行空白——一个红点配一句空话。
    """
    result = run_probe(lambda: (_ for _ in ()).throw(TimeoutError()))

    assert result.ok is False
    assert result.detail == "TimeoutError"


def test_as_dict_is_camel_case():
    """API 边界一律 camelCase，探测结果也不例外。"""
    assert set(ProbeResult(ok=True).as_dict()) == {"ok", "detail", "latencyMs", "error"}


# ============================================================
# 每个 provider 都能探
# ============================================================


@pytest.fixture
def use_settings(monkeypatch):
    def _apply(**kwargs) -> Settings:
        settings = Settings(**kwargs)
        monkeypatch.setattr(registry, "get_settings", lambda: settings)
        return settings

    return _apply


@pytest.mark.parametrize(
    ("kind", "cls"),
    [
        ("llm", MockLLMProvider),
        ("search", MockSearchProvider),
        ("fetch", MockFetcher),
    ],
)
def test_mock_providers_probe_green(kind, cls):
    """三种 mock 都不联网，但探测走的是**真实调用路径**，
    所以这条绿的确实说明了"这条链路跑得通"。"""
    result = cls().probe()

    assert result.ok is True, result.detail
    assert result.detail


def test_every_registered_provider_satisfies_its_protocol_including_probe(monkeypatch):
    """结构匹配是这套适配层的卖点，所以要有测试守着它。

    `isinstance` 对 `runtime_checkable` 的 Protocol 会**逐方法检查**，
    因此这条断言等价于"`probe()` 没被漏在哪个适配器外面"。

    给每家塞一个假密钥，好让它们都能构造出来——凭据是 `_env()` 现读
    `os.environ` 的，所以 `monkeypatch.setenv` 有效。**这条测试不出网**：
    只是构造对象，一次调用都不发。
    """
    from app.providers.base import Fetcher, LLMProvider, SearchProvider

    registry.load_builtin_providers()
    for kind in registry._REGISTRIES:
        for name in registry._REGISTRIES[kind]:
            monkeypatch.setenv(f"{name.upper()}_API_KEY", "fake-key-for-construction")
    settings = Settings()

    protocols = {"llm": LLMProvider, "search": SearchProvider, "fetch": Fetcher}
    checked = 0
    for kind, protocol in protocols.items():
        for name, cls in registry._REGISTRIES[kind].items():
            assert isinstance(cls(settings), protocol), f"{kind}.{name} 不满足协议"
            checked += 1

    # 防止这个循环在注册表被清空时变成一个空转的绿
    assert checked >= 6


# ============================================================
# 探测目标本身
# ============================================================


def test_the_probe_url_is_not_a_url_the_mock_simulates_as_broken():
    """`MockFetcher.degrades()` 按 URL 哈希决定是否模拟抓取失败，约 1/7 命中。

    `PROBE_URL` 是固定的，所以一旦它落在那 1/7 里，mock 的抓取探测就**恒红**——
    而且是个假红。这条断言把"改 `PROBE_URL` 时要顺手验一下"变成自动的。

    真实故障长这样：把 `PROBE_URL` 从 `example.com` 换成 IANA 那页时，
    哈希变了，探测结果就可能整个翻过来。
    """
    assert MockFetcher.degrades(PROBE_URL) is False


def test_the_probe_url_is_not_example_com():
    """`example.com` 看着最自然，但它的正文只有 117 字，低于抽取出正文的
    下限（`_MIN_CHARS = 200`），于是每次探测都被判成"正文抽取失败"。

    也就是说：用它会得到一个**恒红且诊断错误**的探测——报的是"抽取器坏了"，
    而实际是"这个页面本来就没多少字"。这条断言把它钉住，免得有人觉得
    换回 example.com 更"标准"。
    """
    assert "example.com" not in PROBE_URL


# ============================================================
# 包装层
# ============================================================


class _Stub:
    """按给定结果应答的假 provider，用来驱动降级分支。

    带 `capabilities` 是因为 `FallbackLLM.__init__` 要读主 provider 的能力
    （它按"正常情况下"的那家来声明能力）。
    """

    def __init__(self, name: str, result: ProbeResult) -> None:
        self.name = name
        self._result = result
        self.capabilities = LLMCapabilities()

    def probe(self) -> ProbeResult:
        return self._result


_OK_A = ProbeResult(ok=True, detail="A 正常", latency_ms=10)
_OK_B = ProbeResult(ok=True, detail="B 正常", latency_ms=20)
_BAD = ProbeResult(ok=False, detail="A 挂了", error="AuthFailed", latency_ms=5)
_BAD_B = ProbeResult(ok=False, detail="B 也挂了", error="Transient", latency_ms=7)


def test_fallback_primary_ok_does_not_probe_the_secondary():
    """主能用就不探备用：探测会花钱，而备用能不能用此刻不需要知道。"""
    primary = _Stub("A", _OK_A)
    secondary = _Stub("B", ProbeResult(ok=True, detail="不该被调到"))

    result = FallbackLLM(primary=primary, secondary=secondary).probe()

    assert result.ok is True
    assert "A 正常" in result.detail
    assert result.latency_ms == 10


def test_fallback_ok_when_only_the_secondary_works():
    """**判据是"至少一家能用"，不是"主能用"。**

    只探主的话，主一红就报红——而那时系统其实可用。这种探测会天天喊狼来了，
    然后就没人看它了。
    """
    result = FallbackLLM(primary=_Stub("A", _BAD), secondary=_Stub("B", _OK_B)).probe()

    assert result.ok is True
    assert "A 不可用" in result.detail
    assert "B 可用" in result.detail


def test_fallback_red_only_when_both_are_down():
    result = FallbackLLM(primary=_Stub("A", _BAD), secondary=_Stub("B", _BAD_B)).probe()

    assert result.ok is False
    assert "都不可用" in result.detail
    assert result.error == "Transient"


def test_recording_wrapper_passes_the_probe_through_without_recording(tmp_path):
    """探测**绕开录制层**。

    cassette 应该只含流水线真正用到的那几次调用。混进一条探测记录，
    "这份录制对应哪次报告"就说不清了，而且回放时那条探测还会被当成一次
    真实调用被重放。
    """
    recorder = record(MockLLMProvider(), "llm", Settings(cassette_mode="record", cassette_dir=str(tmp_path)))

    result = recorder.probe()

    assert result.ok is True
    assert recorder.written == 0


def test_replay_probe_reports_unavailable_with_a_reason(tmp_path):
    """回放时不与任何真实服务通信，所以**不能**报绿。

    `ok=true` 会是在说"这家 provider 现在能用"，而这一轮里根本没问过它。
    宁可返回一个带解释的红，也不要一个无意义的绿——后者会让人在真的连不上时
    以为连接是好的。
    """
    player = replay(MockLLMProvider, "llm", Settings(cassette_mode="replay", cassette_dir=str(tmp_path)))

    result = player.probe()

    assert result.ok is False
    assert result.error == "CassetteReplay"
    assert "回放模式" in result.detail


# ============================================================
# try_build：唯一的"能用"判据
# ============================================================


def test_try_build_reports_missing_key_as_data_not_as_an_exception():
    outcome = registry.try_build("llm", "zhipu", Settings(ZHIPU_API_KEY=""))

    assert outcome.ok is False
    assert "ProviderNotConfigured" in outcome.error


def test_try_build_succeeds_for_a_provider_that_needs_no_key():
    """mock 没有也不需要密钥。

    这条是踩过的坑：之前"能用"还有第二套判据——`credentials().configured`，
    也就是"有没有密钥"。两套判据在 mock 上打架，于是它在
    `/api/providers` 上挂着"未配置"的红标签、在 `/api/providers/health` 上
    被直接跳过，而它其实跑得好好的。
    """
    outcome = registry.try_build("llm", "mock", Settings())

    assert outcome.ok is True
    assert outcome.error == ""
    assert MockLLMProvider is type(outcome.instance)


@pytest.mark.parametrize(
    ("kind", "name"),
    [("llm", "不存在的provider"), ("不存在的类别", "mock")],
)
def test_try_build_rejects_unknown_kinds_and_names(kind, name):
    outcome = registry.try_build(kind, name, Settings())

    assert outcome.ok is False
    assert outcome.error


def test_provider_error_is_a_plain_exception_subclass():
    """`run_probe` 只 `except Exception` 是**故意**的（见它的 docstring）。

    如果哪天 `ProviderError` 改成了继承 `BaseException`，那条
    `except Exception` 就接不住它了，而症状是探测端点直接 500。
    """
    assert issubclass(ProviderError, Exception)
