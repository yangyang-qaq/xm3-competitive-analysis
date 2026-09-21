"""录制 / 回放：让真实流水线可以离线测。

要解决的问题
------------
完整调研一次要打几十次 LLM 加上百次搜索。如果测试只有"联网跑真 API"一条路，
结果必然是没人跑——而"完整流程还能跑通"恰恰是最该被守护的东西。
mock provider 能测单模块，但它测不了**真实响应结构**：JSON 被截断、
字段是 null、中文编码错乱、返回体里套了三层 data。这些只有真实响应才有。

做法
----
录一次（record），之后无限次离线回放（replay）。同一份 `run_pipeline`，
只换 provider 实现，所以 CI 里跑的就是生产代码路径。

两条守则
--------
1. **缺录制直接抛 `CassetteMiss`，绝不静默联网。** 否则 CI 会悄悄开始花钱，
   而且是间歇性地花——最坏的一种。
2. **key 只由请求内容决定，不含 `purpose` 这类调用点标签。** 这样改了 prompt
   就会 miss 并暴露出来。若把 purpose 编进 key，改了 prompt 仍会命中旧录制，
   测试变成一句善意的谎言。

key 的选择也意味着"录制是脆的"：prompt 改一个字就全部 miss。这是刻意的——
回放的价值在于证明"这条确定的路径能跑通"，而不是假装一切照旧。

回放能证明什么，不能证明什么
----------------------------
**忠实于内容，不忠实于时间。** 实测（见 `问题记录.md` 问题 11）：一次真实录制与
它的离线回放，33 个指标里 31 个逐位相同——成本、token、证据数、独立域名、
维度覆盖、幻觉引用率、质量门、连评审评语都一字不差；只有 `durationMs`
（50859 → 139）、`firstEvidenceMs`、`slowestCallMs` 三个变了，因为回放不发网络请求。

所以：在 cassette 上断言**耗时**会得到一个恒绿的用例（拿 0 和 108 比），
而它守的恰恰是性能。耗时类指标只能在 `pytest -m live` 里测。
`reportId` 同理不可断言——它是 `uuid4()`。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.providers.base import (
    ChatMessage,
    FetchedPage,
    ImageRef,
    LLMCapabilities,
    LLMResponse,
    ModelPricing,
    ProbeResult,
    SearchCapabilities,
    SearchHit,
    SearchQuery,
    Tier,
    TokenUsage,
)
from app.providers.errors import HarnessError, ProviderError

log = logging.getLogger(__name__)


class CassetteMiss(HarnessError):
    """回放时找不到对应的录制。

    `retryable=False`：重试一次也不会变出一条录制来，重试只是浪费时间。
    这条错误必须响亮——它是"测试试图联网"的唯一信号。

    继承 `HarnessError` 而不是直接继承 `ProviderError`，是因为**它必须
    穿透降级逻辑**：改了 prompt 导致 key 变化是常事，那时如果被
    `required=False` 吞掉，回放会拿着一份残缺的录制跑出一份看起来正常的报告。
    见 `HarnessError` 的 docstring。
    """


# ============================================================
# 序列化
# ============================================================


def _digest(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def _llm_key(
    messages: Sequence[ChatMessage],
    tier: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> str:
    return _digest(
        {
            "kind": "llm",
            "messages": [[m.role, m.content] for m in messages],
            "tier": tier,
            "temperature": round(float(temperature), 4),
            "max_tokens": int(max_tokens),
            "json_mode": bool(json_mode),
        }
    )


def _search_key(query: SearchQuery) -> str:
    return _digest({"kind": "search", **query.cache_key()})


def _fetch_key(url: str) -> str:
    return _digest({"kind": "fetch", "url": url})


def _encode_llm(resp: LLMResponse) -> dict:
    return {
        "text": resp.text,
        "model": resp.model,
        "provider": resp.provider,
        "usage": asdict(resp.usage),
        "latency_ms": resp.latency_ms,
        "finish_reason": resp.finish_reason,
    }


def _decode_llm(value: dict) -> LLMResponse:
    return LLMResponse(
        text=str(value.get("text", "")),
        model=str(value.get("model", "")),
        provider=str(value.get("provider", "")),
        usage=TokenUsage(**(value.get("usage") or {})),
        latency_ms=int(value.get("latency_ms", 0)),
        finish_reason=str(value.get("finish_reason", "stop")),
    )


def _encode_search(hits: Sequence[SearchHit]) -> list[dict]:
    return [asdict(hit) for hit in hits]


def _decode_search(value: list[dict]) -> list[SearchHit]:
    return [SearchHit(**hit) for hit in value]


def _encode_fetch(page: FetchedPage) -> dict:
    data = asdict(page)
    data["images"] = [asdict(img) for img in page.images]
    return data


def _decode_fetch(value: dict) -> FetchedPage:
    data = dict(value)
    data["images"] = [ImageRef(**img) for img in (data.get("images") or [])]
    return FetchedPage(**data)


# ============================================================
# 文件读写
# ============================================================


class _Store:
    """JSONL 存储。一行一条记录，便于 git diff 与人工检查。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._records: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for lineno, line in enumerate(self.path.read_text("utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                log.warning("cassette %s 第 %d 行不是合法 JSON，已跳过", self.path.name, lineno)
                continue
            key = record.get("key")
            if key:
                self._records[key] = record

    def get(self, key: str) -> dict | None:
        return self._records.get(key)

    def put(self, key: str, record: dict) -> None:
        """写入并立即 flush。

        立即 flush 是刻意的：录制往往在长时间跑动中途被打断，
        缓冲区里没落盘的记录会白花一次 API 调用，下次还得再花一次。
        """
        with self._lock:
            self._records[key] = record
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()

    def __len__(self) -> int:
        return len(self._records)


# ============================================================
# 录制
# ============================================================


class RecordingProvider:
    """透传调用并落盘。对外接口与被包装的 provider 完全一致。"""

    def __init__(self, inner, kind: str, settings: Settings) -> None:
        self._inner = inner
        self._kind = kind
        self.name = getattr(inner, "name", kind)
        self.capabilities = getattr(inner, "capabilities", None)
        self._store = _Store(settings.cassette_path / f"{kind}.{self.name}.jsonl")
        self.written = 0
        self.reused = 0

    def resolve_model(self, tier: Tier) -> str:
        return self._inner.resolve_model(tier)

    def pricing(self) -> Mapping[str, ModelPricing]:
        return self._inner.pricing()

    def search(self, query: SearchQuery) -> Sequence[SearchHit]:
        key = _search_key(query)
        cached = self._store.get(key)
        if cached is not None:
            # key 已存在就不再打网络请求。重跑录制是常事（加了几条新 query），
            # 每跑一次都把旧的重新买一遍没有意义。要强制重录就删掉 jsonl。
            self.reused += 1
            return _decode_search(cached["value"])

        hits = self._inner.search(query)
        self._write(key, {"query": query.cache_key()}, _encode_search(hits))
        return hits

    def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        tier: Tier = "aux",
        temperature: float = 0.6,
        max_tokens: int = 2048,
        json_mode: bool = False,
        purpose: str = "",
        evidence_ids: Sequence[str] | None = None,
    ) -> LLMResponse:
        key = _llm_key(messages, tier, temperature, max_tokens, json_mode)
        cached = self._store.get(key)
        if cached is not None:
            self.reused += 1
            return _decode_llm(cached["value"])

        resp = self._inner.chat(
            messages,
            tier=tier,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            purpose=purpose,
            evidence_ids=evidence_ids,
        )
        # purpose 只进 meta，不进 key——见模块 docstring
        self._write(key, {"purpose": purpose, "tier": tier}, _encode_llm(resp))
        return resp

    def chat_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tier: Tier = "aux",
        temperature: float = 0.6,
        max_tokens: int = 2048,
    ) -> Iterator[str]:
        # 流式不录制：录制的是"最终结果"，而流式的价值在于过程。
        # 流水线里流式只用于给人看的场合，不承担可复现性。
        return self._inner.chat_stream(
            messages, tier=tier, temperature=temperature, max_tokens=max_tokens
        )

    def fetch(self, url: str, *, fallback_snippet: str = "") -> FetchedPage:
        key = _fetch_key(url)
        cached = self._store.get(key)
        if cached is not None:
            self.reused += 1
            return _decode_fetch(cached["value"])

        page = self._inner.fetch(url, fallback_snippet=fallback_snippet)
        self._write(key, {"url": url}, _encode_fetch(page))
        return page

    def probe(self) -> ProbeResult:
        """直接透传给被包装的 provider。

        注意探测**绕开了录制层**：`self._inner.probe()` 用的是内层自己的
        调用路径，不经过这里的 `chat` / `search` / `fetch`，所以探测请求不会被写进
        cassette。这是想要的——cassette 应该只含流水线真正用到的那几次调用，
        混进去一条探测记录会让"这份录制对应哪次报告"变得说不清。
        """
        return self._inner.probe()

    def _write(self, key: str, meta: dict, value: Any) -> None:
        self._store.put(
            key,
            {"key": key, "kind": self._kind, "provider": self.name, "meta": meta, "value": value},
        )
        self.written += 1

    def stats(self) -> dict:
        return {"mode": "record", "file": self._store.path.name, "written": self.written, "reused": self.reused, "total": len(self._store)}


# ============================================================
# 回放
# ============================================================


class ReplayProvider:
    """只从录制里取结果。**任何缺失都抛 `CassetteMiss`。**

    注意它不持有真 provider 实例：CI 里通常没有 API Key，构造真适配器会直接抛
    `ProviderNotConfigured`，于是"回放"这件事反而要求你有密钥——这很荒唐。
    所以这里持有的是**类**，能力与档位映射从类属性上取。
    """

    def __init__(self, cls: type, kind: str, settings: Settings) -> None:
        self._cls = cls
        self._settings = settings
        self._kind = kind
        self.name = str(getattr(cls, "name", cls.__name__.lower()))
        self.capabilities = getattr(cls, "capabilities", None) or (
            LLMCapabilities() if kind == "llm" else SearchCapabilities()
        )
        self._store = _Store(settings.cassette_path / f"{kind}.{self.name}.jsonl")
        self.hits = 0
        self.misses = 0

        # 有密钥时用真适配器回答 resolve_model / pricing，保持"档位映射只有一处实现"。
        # 没密钥时退到下面的标签逻辑——CI 里这属于正常情况，不是错误。
        self._delegate = None
        with contextlib.suppress(ProviderError):
            self._delegate = cls(settings)

    # ---- LLM 元信息 ----

    def resolve_model(self, tier: Tier) -> str:
        if self._delegate is not None:
            return self._delegate.resolve_model(tier)
        # 无密钥回放时这只是个**标签**，不是可调用的模型名：
        # 真正写进 trace 的模型名来自录制里的响应体。
        models = getattr(self._cls, "default_models", {}) or {}
        return self._settings.credentials(self.name).model_for(tier, models.get(tier, "")) or f"{self.name}:{tier}"

    def pricing(self) -> Mapping[str, ModelPricing]:
        if self._delegate is not None:
            return self._delegate.pricing()
        return {}

    # ---- 数据面 ----

    def search(self, query: SearchQuery) -> Sequence[SearchHit]:
        return _decode_search(self._take(_search_key(query), f"search({query.text!r})"))

    def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        tier: Tier = "aux",
        temperature: float = 0.6,
        max_tokens: int = 2048,
        json_mode: bool = False,
        purpose: str = "",
        evidence_ids: Sequence[str] | None = None,
    ) -> LLMResponse:
        _ = (purpose, evidence_ids)
        key = _llm_key(messages, tier, temperature, max_tokens, json_mode)
        return _decode_llm(self._take(key, f"chat(purpose={purpose!r}, tier={tier})"))

    def chat_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tier: Tier = "aux",
        temperature: float = 0.6,
        max_tokens: int = 2048,
    ) -> Iterator[str]:
        raise CassetteMiss(
            "流式调用不录制，无法回放", provider=self.name, detail=f"tier={tier}"
        )

    def fetch(self, url: str, *, fallback_snippet: str = "") -> FetchedPage:
        _ = fallback_snippet
        return _decode_fetch(self._take(_fetch_key(url), f"fetch({url})"))

    def probe(self) -> ProbeResult:
        """回放模式下的探测**报不可用**，并说明原因。

        这是实情：回放时不与任何真实服务通信，`ok=true` 会是在说"这家 provider
        现在能用"，而这一轮里根本没有去问过它。宁可返回一个带解释的红，
        也不要一个无意义的绿——后者会让人在真连不上的时候以为连接是好的。
        """
        return ProbeResult(
            ok=False,
            detail=f"回放模式：不发出真实请求（cassette 文件 {self._store.path.name}）",
            error="CassetteReplay",
        )

    def _take(self, key: str, what: str) -> Any:
        record = self._store.get(key)
        if record is None:
            self.misses += 1
            raise CassetteMiss(
                f"回放缺失：{what}",
                provider=self.name,
                detail=(
                    f"cassette 文件 {self._store.path} 中没有这条记录。"
                    "请以 CASSETTE_MODE=record 重新运行一次；"
                    "若只是改了 prompt 或 query，这是预期行为（key 由请求内容决定）。"
                ),
            )
        self.hits += 1
        return record["value"]

    def stats(self) -> dict:
        return {
            "mode": "replay",
            "file": self._store.path.name,
            "hits": self.hits,
            "misses": self.misses,
            "total": len(self._store),
        }


# ============================================================
# 入口
# ============================================================


def record(provider, kind: str, settings: Settings) -> RecordingProvider:
    """包一层录制。`kind` 取 llm / search / fetch。"""
    return RecordingProvider(provider, kind, settings)


def replay(cls: type, kind: str, settings: Settings) -> ReplayProvider:
    """按类回放。刻意收类而非实例——见 `ReplayProvider` 的说明。"""
    return ReplayProvider(cls, kind, settings)
