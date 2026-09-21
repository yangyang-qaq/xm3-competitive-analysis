"""Tavily 搜索 provider。

方言翻译表
----------
| 归一化表达                  | Tavily 参数                       |
|----------------------------|-----------------------------------|
| `text`                     | `query`                           |
| `sites=("douyin.com",)`    | `include_domains=["douyin.com"]`  |
| `freshness="month"`        | `days=30`（按天数换算）             |
| `limit`                    | `max_results`                     |

**注意 freshness 的形态差异**：博查是枚举字符串，Tavily 是天数。
这正是不能把方言漏进业务代码的原因——业务侧写成 `"oneMonth"` 就绑死博查了，
写成 `30` 就绑死 Tavily 了。归一化表达是 `"month"`，怎么翻译各家自己决定。

另：Tavily 的 API Key 是放在请求体里的（不是 Authorization 头）。
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
    ProviderError,
    ProviderNotConfigured,
    Transient,
    classify_httpx,
    classify_status,
)
from app.providers.registry import register_search

#: 归一化时效性 → 天数。Tavily 用天数而非枚举。
_FRESHNESS_DAYS: dict[Freshness, int | None] = {
    "any": None,
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}

_API_PATH = "/search"


@register_search("tavily")
class TavilySearchProvider:
    name = "tavily"
    capabilities = SearchCapabilities(
        site_filter=True,
        freshness_filter=True,
        long_snippet=True,
        max_results_per_call=20,
    )

    retry_policy = retry_mod.DEFAULT

    def __init__(self, settings: Settings, *, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._cred = settings.credentials(self.name)
        if not self._cred.configured:
            raise ProviderNotConfigured("未配置 TAVILY_API_KEY", provider=self.name)

        base = (self._cred.base_url or "https://api.tavily.com").rstrip("/")
        self._endpoint = f"{base}{_API_PATH}"
        self._client = http_client or httpx.Client(
            timeout=settings.fetch_timeout,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )

    def search(self, query: SearchQuery) -> Sequence[SearchHit]:
        body = retry_mod.with_backoff(
            lambda: self._post(self._build_payload(query)),
            policy=self.retry_policy,
            provider=self.name,
        )
        return self._parse(body)

    def probe(self) -> ProbeResult:
        """策略见 `base.probe_search`。"""
        return probe_search(self)

    def _build_payload(self, query: SearchQuery) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "api_key": self._cred.api_key,
            "query": query.text,
            "max_results": max(1, min(query.limit, self.capabilities.max_results_per_call)),
            "search_depth": "advanced",
        }
        if query.sites:
            payload["include_domains"] = list(query.sites)
        days = _FRESHNESS_DAYS.get(query.freshness)
        if days is not None:
            payload["days"] = days
        return payload

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._client.post(self._endpoint, json=payload)
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

        if "results" not in body:
            raise ProviderError(
                "响应缺少 results 字段", provider=self.name, detail=str(body)[:500]
            )
        return body

    def _parse(self, body: dict[str, Any]) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for rank, item in enumerate(body.get("results") or []):
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            hits.append(
                SearchHit(
                    title=str(item.get("title") or "").strip(),
                    url=url,
                    snippet=str(item.get("content") or "").strip(),
                    site_name=_domain_of(url),
                    published_at=str(item.get("published_date") or "").strip(),
                    provider=self.name,
                    rank=rank,
                )
            )
        return hits

    def close(self) -> None:
        self._client.close()


def _domain_of(url: str) -> str:
    """从 URL 取域名。Tavily 不返回站点名，自己补上——
    可信度评分要用它，而评分不该依赖某个 provider 是否乐意提供这个字段。"""
    try:
        return httpx.URL(url).host or ""
    except Exception:  # noqa: BLE001  # pragma: no cover
        return ""
