"""博查 Bocha 搜索 provider。

方言翻译表
----------
| 归一化表达                  | 博查参数                          |
|----------------------------|----------------------------------|
| `text`                     | `query`                          |
| `sites=("douyin.com",)`    | `include="douyin.com"`（`\\|` 连接）|
| `freshness="month"`        | `freshness="oneMonth"`           |
| `limit`                    | `count`（上限 50）                |

需要注意的是**错误码**：博查在 HTTP 200 的响应体里用 `code` 字段报业务错误。
只看 HTTP 状态码会把它当成功，然后拿到一个空的 `data`，
表现为"搜索没结果"而不是"密钥不对"——这类静默失败最难查。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from app.core.config import Settings
from app.providers import retry as retry_mod
from app.providers.base import (
    Freshness,
    ProbeResult,
    SearchCapabilities,
    SearchHit,
    SearchQuery,
    probe_search,
)
from app.providers.errors import (
    AuthFailed,
    BadRequest,
    ProviderError,
    ProviderNotConfigured,
    RateLimited,
    Transient,
    classify_httpx,
    classify_status,
)
from app.providers.registry import register_search

#: 归一化时效性 → 博查方言。这是整个适配层存在的理由的缩影：
#: 业务代码写 "month"，翻译成本家的事。
_FRESHNESS: dict[Freshness, str] = {
    "any": "noLimit",
    "day": "oneDay",
    "week": "oneWeek",
    "month": "oneMonth",
    "year": "oneYear",
}

_API_PATH = "/web-search"
_MAX_RESULTS = 50


@register_search("bocha")
class BochaSearchProvider:
    name = "bocha"
    capabilities = SearchCapabilities(
        site_filter=True,
        freshness_filter=True,
        long_snippet=True,
        max_results_per_call=_MAX_RESULTS,
    )

    #: 搜索比 LLM 便宜且快，失败重试两次足够。重试太多次会拖慢整个采集阶段。
    retry_policy = retry_mod.DEFAULT

    def __init__(self, settings: Settings, *, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._cred = settings.credentials(self.name)
        if not self._cred.configured:
            raise ProviderNotConfigured("未配置 BOCHA_API_KEY", provider=self.name)

        base = (self._cred.base_url or "https://api.bocha.cn/v1").rstrip("/")
        self._endpoint = f"{base}{_API_PATH}"

        # 复用单个 client：一次调研任务会发上百条搜索请求，
        # 每条都新建 Client 会开出上百个连接池（参考实现的问题之一）。
        self._client = http_client or httpx.Client(
            timeout=settings.fetch_timeout,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            headers={"Content-Type": "application/json"},
        )

    # ------------------------------------------------------------------

    def search(self, query: SearchQuery) -> Sequence[SearchHit]:
        payload = self._build_payload(query)
        body = retry_mod.with_backoff(
            lambda: self._post(payload),
            policy=self.retry_policy,
            provider=self.name,
        )
        return self._parse(body)

    def probe(self) -> ProbeResult:
        """策略见 `base.probe_search`。博查特有的部分是：业务错误码在
        HTTP 200 的响应体里，由 `_parse` 映射成异常，探测因此能捕获到。"""
        return probe_search(self)

    def _build_payload(self, query: SearchQuery) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query": query.text,
            "summary": True,  # 要长摘要：能减少抓取次数
            "count": max(1, min(query.limit, _MAX_RESULTS)),
            "freshness": _FRESHNESS.get(query.freshness, "noLimit"),
        }
        if query.sites:
            payload["include"] = "|".join(query.sites)
        return payload

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._client.post(
                self._endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {self._cred.api_key}"},
            )
        except Exception as exc:  # noqa: BLE001
            raise classify_httpx(exc, provider=self.name) from exc

        if resp.status_code != 200:
            raise classify_status(resp.status_code, provider=self.name, detail=resp.text[:500])

        try:
            body = resp.json()
        except ValueError as exc:
            raise Transient(
                "响应不是合法 JSON", provider=self.name, detail=resp.text[:500]
            ) from exc

        # HTTP 200 但业务码报错——不检查的话会静默返回空结果
        self._raise_for_business_code(body)
        return body

    def _raise_for_business_code(self, body: dict[str, Any]) -> None:
        code = body.get("code")
        if code is None or int(code) == 200:
            return
        message = str(body.get("msg") or body.get("message") or "未知错误")
        detail = str(body)[:500]

        if int(code) in (401, 403):
            raise AuthFailed(f"博查鉴权失败：{message}", provider=self.name, detail=detail)
        if int(code) == 429:
            raise RateLimited(f"博查限流：{message}", provider=self.name, detail=detail)
        if int(code) in (400, 422):
            raise BadRequest(f"博查请求不合法：{message}", provider=self.name, detail=detail)
        raise ProviderError(f"博查错误码 {code}：{message}", provider=self.name, detail=detail)

    def _parse(self, body: dict[str, Any]) -> list[SearchHit]:
        data = body.get("data") or {}
        pages = (data.get("webPages") or {}).get("value") or []

        hits: list[SearchHit] = []
        for rank, page in enumerate(pages):
            url = str(page.get("url") or "").strip()
            if not url:
                continue
            hits.append(
                SearchHit(
                    title=str(page.get("name") or "").strip(),
                    url=url,
                    # summary 比 snippet 长，优先用；没有再退回 snippet
                    snippet=str(page.get("summary") or page.get("snippet") or "").strip(),
                    site_name=str(page.get("siteName") or "").strip(),
                    published_at=str(page.get("datePublished") or "").strip(),
                    provider=self.name,
                    rank=rank,
                )
            )
        return hits

    def close(self) -> None:
        self._client.close()
