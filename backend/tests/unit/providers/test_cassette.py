"""录制 / 回放往返。

这是"真实流水线可以离线测"这条主张的证据。测的是三件事：
往返无损、缺录制必须响亮、key 由请求内容决定。
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.providers.base import ChatMessage, SearchQuery
from app.providers.cassette import CassetteMiss, ReplayProvider, record, replay
from app.providers.errors import ProviderError, ProviderNotConfigured
from app.providers.llm.deepseek import DeepSeekProvider
from app.providers.mock import MockFetcher, MockLLMProvider, MockSearchProvider

_MESSAGES = [ChatMessage("user", "分析一下这个产品的定价策略")]


def _settings(tmp_path, mode: str) -> Settings:
    return Settings(cassette_mode=mode, cassette_dir=str(tmp_path))


# ============================================================
# 往返
# ============================================================


def test_llm_record_then_replay_is_lossless(tmp_path):
    inner = MockLLMProvider()
    recorder = record(inner, "llm", _settings(tmp_path, "record"))
    original = recorder.chat(_MESSAGES, purpose="analyze_claims", tier="core")

    assert (tmp_path / "llm.mock.jsonl").exists()

    player = replay(MockLLMProvider, "llm", _settings(tmp_path, "replay"))
    replayed = player.chat(_MESSAGES, purpose="analyze_claims", tier="core")

    assert replayed.text == original.text
    assert replayed.model == original.model
    assert replayed.usage == original.usage


def test_the_recording_is_human_readable_jsonl(tmp_path):
    """一行一条、字段可读，是为了能直接 git diff 看出"这次改了什么"。

    二进制或压缩格式会省点空间，但录制文件的价值恰恰在于它是可审阅的。
    """
    recorder = record(MockLLMProvider(), "llm", _settings(tmp_path, "record"))
    recorder.chat(_MESSAGES, purpose="analyze_claims")

    lines = (tmp_path / "llm.mock.jsonl").read_text("utf-8").strip().splitlines()
    assert len(lines) == 1
    record_row = json.loads(lines[0])
    assert record_row["kind"] == "llm"
    assert record_row["provider"] == "mock"
    # purpose 进 meta 供人查看，但不参与 key
    assert record_row["meta"]["purpose"] == "analyze_claims"
    assert "定价策略" in record_row["value"]["text"] or record_row["value"]["text"]


def test_search_round_trip_preserves_every_field(tmp_path):
    query = SearchQuery(text="定价", sites=("douyin.com",), freshness="month", limit=3)
    recorder = record(MockSearchProvider(), "search", _settings(tmp_path, "record"))
    hits = list(recorder.search(query))

    player = replay(MockSearchProvider, "search", _settings(tmp_path, "replay"))
    again = list(player.search(query))

    assert [h.url for h in again] == [h.url for h in hits]
    assert [h.published_at for h in again] == [h.published_at for h in hits]
    assert [h.site_name for h in again] == [h.site_name for h in hits]
    assert [h.rank for h in again] == [h.rank for h in hits]


def test_fetch_round_trip_preserves_metadata_and_images(tmp_path):
    recorder = record(MockFetcher(), "fetch", _settings(tmp_path, "record"))
    page = recorder.fetch("https://example.com/a")

    player = replay(MockFetcher, "fetch", _settings(tmp_path, "replay"))
    again = player.fetch("https://example.com/a")

    assert again.text == page.text
    assert again.title == page.title
    assert again.degraded == page.degraded
    assert [img.url for img in again.images] == [img.url for img in page.images]


def test_degraded_fetches_are_recorded_too(tmp_path):
    """降级结果同样要录。只录成功的话，回放里就永远见不到降级路径，
    "降级率"这个指标也就没法在离线测试里被验证。"""
    recorder = record(MockFetcher(), "fetch", _settings(tmp_path, "record"))
    # 直接问 mock 哪条 URL 会失败，而不是碰运气扫——规则是确定的，测试也该是确定的
    url = next(u for u in (f"https://example.com/{i}" for i in range(200)) if MockFetcher.degrades(u))
    assert recorder.fetch(url).degraded is True

    player = replay(MockFetcher, "fetch", _settings(tmp_path, "replay"))
    assert player.fetch(url).degraded is True


# ============================================================
# 缺失必须响亮
# ============================================================


def test_missing_recording_raises_cassette_miss(tmp_path):
    player = replay(MockLLMProvider, "llm", _settings(tmp_path, "replay"))
    with pytest.raises(CassetteMiss):
        player.chat(_MESSAGES)
    assert player.misses == 1


def test_cassette_miss_is_not_retryable():
    """重试一次也不会变出一条录制来。让它可重试只是在浪费时间。"""
    assert CassetteMiss.retryable is False
    assert isinstance(CassetteMiss("缺"), ProviderError)


def test_cassette_miss_explains_how_to_fix_it(tmp_path):
    player = replay(MockLLMProvider, "llm", _settings(tmp_path, "replay"))
    with pytest.raises(CassetteMiss) as excinfo:
        player.chat(_MESSAGES)
    message = str(excinfo.value) + excinfo.value.detail
    assert "CASSETTE_MODE=record" in message


def test_streaming_replay_raises(tmp_path):
    """流式不录制，所以回放必须明确报错而不是返回空迭代器——
    空迭代器会让上层以为"模型没输出"，那是另一个 bug 的样子。"""
    player = replay(MockLLMProvider, "llm", _settings(tmp_path, "replay"))
    with pytest.raises(CassetteMiss):
        player.chat_stream(_MESSAGES)


# ============================================================
# key 由请求内容决定
# ============================================================


def test_changing_the_prompt_causes_a_miss(tmp_path):
    """这是刻意的：改了 prompt 就该 miss 并暴露出来。

    若把 prompt 排除在 key 之外，改了 prompt 仍会命中旧录制，
    测试就变成了一句善意的谎言——它证明的是"以前跑通过"。
    """
    recorder = record(MockLLMProvider(), "llm", _settings(tmp_path, "record"))
    recorder.chat([ChatMessage("user", "原始问题")])

    player = replay(MockLLMProvider, "llm", _settings(tmp_path, "replay"))
    with pytest.raises(CassetteMiss):
        player.chat([ChatMessage("user", "改过的问题")])


def test_purpose_is_not_part_of_the_key(tmp_path):
    """purpose 是调用点标签，不进 key。

    同一段 prompt 在不同环节被复用是正常的（如"写章节"与"深化章节"），
    让标签参与 key 会凭空多出一堆重复录制，而它们的内容完全一样。
    """
    recorder = record(MockLLMProvider(), "llm", _settings(tmp_path, "record"))
    recorder.chat(_MESSAGES, purpose="write")

    player = replay(MockLLMProvider, "llm", _settings(tmp_path, "replay"))
    assert player.chat(_MESSAGES, purpose="refine").text  # 命中，不抛


def test_tier_is_part_of_the_key(tmp_path):
    """换了档位就是换了模型，录制不能混用。"""
    recorder = record(MockLLMProvider(), "llm", _settings(tmp_path, "record"))
    recorder.chat(_MESSAGES, tier="core")

    player = replay(MockLLMProvider, "llm", _settings(tmp_path, "replay"))
    with pytest.raises(CassetteMiss):
        player.chat(_MESSAGES, tier="fast")


def test_json_mode_is_part_of_the_key(tmp_path):
    """同一个 prompt，要 JSON 和不要 JSON 是两次不同的调用。"""
    recorder = record(MockLLMProvider(), "llm", _settings(tmp_path, "record"))
    recorder.chat(_MESSAGES, json_mode=True)

    player = replay(MockLLMProvider, "llm", _settings(tmp_path, "replay"))
    with pytest.raises(CassetteMiss):
        player.chat(_MESSAGES, json_mode=False)


# ============================================================
# 录制本身的成本控制
# ============================================================


def test_recording_reuses_existing_keys_instead_of_paying_twice(tmp_path):
    """重跑录制是常事（加了几条新 query），每次都把旧的重新买一遍没有意义。"""
    inner = MockLLMProvider()
    recorder = record(inner, "llm", _settings(tmp_path, "record"))

    first = recorder.chat(_MESSAGES)
    second = recorder.chat(_MESSAGES)

    assert len(inner.calls) == 1, "第二次不该再打 provider"
    assert recorder.reused == 1
    assert second.text == first.text


def test_recording_appends_rather_than_truncating(tmp_path):
    inner = MockLLMProvider()
    recorder = record(inner, "llm", _settings(tmp_path, "record"))
    recorder.chat([ChatMessage("user", "问题一")])
    recorder.chat([ChatMessage("user", "问题二")])

    lines = (tmp_path / "llm.mock.jsonl").read_text("utf-8").strip().splitlines()
    assert len(lines) == 2


def test_malformed_lines_are_skipped_not_fatal(tmp_path):
    """录制文件是人在看的、也会被手工编辑。一行坏掉不该让整个测试套件起不来。"""
    path = tmp_path / "llm.mock.jsonl"
    path.write_text('{"broken": \n{"key": "x", "value": 1}\n', encoding="utf-8")

    player = replay(MockLLMProvider, "llm", _settings(tmp_path, "replay"))
    assert player.stats()["total"] == 1


# ============================================================
# 回放不要求有密钥
# ============================================================


def test_replay_works_without_any_api_key(tmp_path, monkeypatch):
    """CI 里没有密钥是常态。

    如果回放必须先构造真实适配器，那"离线回放"就变成了"必须先有密钥"——
    自相矛盾，而这正是 ReplayProvider 收类而不是收实例的原因。
    """
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    player = replay(DeepSeekProvider, "llm", _settings(tmp_path, "replay"))

    assert isinstance(player, ReplayProvider)
    assert player.name == "deepseek"
    assert player.capabilities.max_context_tokens > 0

    # 档位映射退化成标签——真正写进 trace 的模型名来自录制里的响应体
    player.resolve_model("core")  # 不抛即可
    assert player.pricing() == {}


def test_real_provider_still_requires_a_key(monkeypatch):
    """对照组：不走回放时，缺密钥必须照旧报错。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ProviderNotConfigured):
        DeepSeekProvider(Settings())


def test_stats_report_hits_and_misses(tmp_path):
    recorder = record(MockLLMProvider(), "llm", _settings(tmp_path, "record"))
    recorder.chat(_MESSAGES)
    assert recorder.stats()["mode"] == "record"

    player = replay(MockLLMProvider, "llm", _settings(tmp_path, "replay"))
    player.chat(_MESSAGES)
    with pytest.raises(CassetteMiss):
        player.chat([ChatMessage("user", "别的")])

    stats = player.stats()
    assert (stats["hits"], stats["misses"]) == (1, 1)
