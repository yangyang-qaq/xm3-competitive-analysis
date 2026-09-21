"""HTTP 网页抓取 provider。

三个必须正面处理的问题
----------------------
**编码。** 中文站点大量使用 GBK/GB18030，而相当一部分服务器根本不发
`Content-Type: charset`，或者发一个无意义的 `iso-8859-1`。httpx 在缺省时按 UTF-8 解码，
结果是整页乱码——更糟的是它**不报错**，于是乱码正文一路流进证据库，
可信度评分照给，报告照印。这里自己嗅探：先看 BOM，再看 meta charset，
再看 Content-Type，最后逐个候选编码试解，用"替换字符比例"挑最好的那个。

**正文抽取。** trafilatura 对新闻/博客很准，但对文档站、SPA 渲染页会返回 None。
这时退到 BeautifulSoup 按段落聚合。两条路都失败才用搜索摘要兜底，且必须 `degraded=True`——
摘要支撑的论点和正文支撑的论点不是一个可信度等级，这个区别不能丢。

**降级必须显式。** `ok=False` 时 `text` 仍有内容（摘要），调用方若只看 `ok` 会误判，
所以 `degraded` 是独立字段，且默认 True——忘了设置时保守地认为它是降级的。
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

import httpx

from app.core.config import Settings
from app.providers import retry as retry_mod
from app.providers.base import PROBE_URL, FetchedPage, ImageRef, ProbeResult, run_probe
from app.providers.errors import ProviderError, classify_httpx
from app.providers.registry import register_fetcher

log = logging.getLogger(__name__)

_API_LIKE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36 xm3-research-bot/0.1"
)

#: 抽取出的正文短于这个长度就当作失败。太短的"正文"多半是导航栏或验证码页。
_MIN_CHARS = 200

#: 图片最多留这么多张。证据图是用来佐证的，不是图库。
_MAX_IMAGES = 12

#: 编码嗅探的候选顺序。utf-8 在前（现在绝大多数站点），gb18030 是 GBK 的超集。
_ENCODINGS = ("utf-8", "gb18030", "big5", "shift_jis")

_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?\s*([\w-]+)""", re.I)


def _sniff_encoding(raw: bytes, declared: str | None) -> str:
    """决定用哪种编码解码。

    顺序：BOM > 页面内 meta > HTTP 头 > 逐候选试解。
    HTTP 头刻意排在 meta 之后：服务器把 charset 写错的情况比页面写错更常见。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"

    match = _META_CHARSET.search(raw[:4096])
    if match:
        candidate = match.group(1).decode("ascii", errors="ignore").lower()
        if candidate:
            return candidate

    if declared:
        candidate = declared.lower()
        # iso-8859-1 是服务器没配编码时的默认值，几乎总是错的——忽略它
        if candidate not in ("iso-8859-1", "latin-1", "ascii"):
            return candidate

    return "utf-8"


def _decode(raw: bytes, declared: str | None) -> str:
    """解码并选出乱码最少的结果。

    判据是"替换字符 U+FFFD 的占比"：解码器报错的地方会被替换成它，
    所以占比最低的那个编码就是最可能正确的。这个办法不需要额外依赖，
    对 GBK 当成 UTF-8 解这类典型错误很有效。
    """
    primary = _sniff_encoding(raw, declared)
    candidates = [primary, *(e for e in _ENCODINGS if e != primary)]

    best_text = ""
    best_bad = 2.0
    for encoding in candidates:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            # 严格解码失败也能说明问题：GBK 页按 UTF-8 解通常直接抛错
            try:
                text = raw.decode(encoding, errors="replace")
            except LookupError:
                continue
            bad = text.count("�") / max(1, len(text))
            if bad < best_bad:
                best_text, best_bad = text, bad
            continue
        return text  # 严格解码成功，直接用

    return best_text or raw.decode("utf-8", errors="replace")


@register_fetcher("http")
class HttpFetcher:
    """httpx + trafilatura，BeautifulSoup 兜底。"""

    name = "http"

    #: 抓取失败重试一次就够。整批采集有几十上百个 URL，
    #: 单个 URL 反复重试会拖垮整个采集阶段的耗时。
    retry_policy = retry_mod.FETCH

    def __init__(self, settings: Settings, *, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._max_chars = settings.fetch_max_chars
        self._client = http_client or httpx.Client(
            timeout=settings.fetch_timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            headers={
                "User-Agent": _API_LIKE_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

    # ------------------------------------------------------------------

    def fetch(self, url: str, *, fallback_snippet: str = "") -> FetchedPage:
        captured = datetime.now(UTC).isoformat(timespec="seconds")
        try:
            resp = retry_mod.with_backoff(
                lambda: self._get(url),
                policy=self.retry_policy,
                provider=self.name,
            )
        except ProviderError as exc:
            # 抓取失败不该中断整批采集：降级成摘要，把失败如实记下来
            return self._degraded(url, fallback_snippet, captured, str(exc), status=0)

        if resp.status_code != 200:
            return self._degraded(
                url,
                fallback_snippet,
                captured,
                f"HTTP {resp.status_code}",
                status=resp.status_code,
                final_url=str(resp.url),
            )

        content_type = resp.headers.get("content-type", "")
        if "html" not in content_type and "xml" not in content_type and content_type:
            return self._degraded(
                url,
                fallback_snippet,
                captured,
                f"非 HTML 内容：{content_type}",
                status=resp.status_code,
                final_url=str(resp.url),
            )

        html = _decode(resp.content, resp.charset_encoding)
        return self._extract(url, str(resp.url), html, fallback_snippet, captured, resp.status_code)

    def probe(self) -> ProbeResult:
        """取一次 `PROBE_URL`，并要求**真的抽出了正文**。

        判据是正文而非状态码：抓取器最常见的故障不是连不上，而是页面拿回来了
        但正文抽取规则对不上，于是每条证据都静默退化成只有摘要、可信度被扣分，
        而整条链路看上去一切正常。只查状态码的探测恰好在此时是绿的。
        """
        url = self.probe_url

        def call() -> str:
            page = self.fetch(url)
            if page.degraded or not page.text.strip():
                raise ProviderError(f"取回了页面但没能抽出正文：{page.error or '正文为空'}")
            return f"抽出正文 {len(page.text)} 字（HTTP {page.status}）"

        return run_probe(call)

    #: 探测目标。做成类属性而不是写死，好让网络环境特殊时在子类里换掉。
    probe_url: str = PROBE_URL

    def _get(self, url: str) -> httpx.Response:
        try:
            return self._client.get(url)
        except Exception as exc:  # noqa: BLE001
            raise classify_httpx(exc, provider=self.name) from exc

    # ------------------------------------------------------------------
    # 抽取
    # ------------------------------------------------------------------

    def _extract(
        self,
        url: str,
        final_url: str,
        html: str,
        fallback_snippet: str,
        captured: str,
        status: int,
    ) -> FetchedPage:
        text = _extract_main_text(html, final_url)
        degraded = len(text) < _MIN_CHARS

        if degraded:
            # 抽取结果太短，不可信。**有搜索摘要时优先用摘要**：
            # 摘要至少是"关于这个页面的描述"，而不足 200 字的抽取结果多半是
            # 导航栏、验证码页或脚本残留——留着它只是把噪声冒充成正文。
            text = fallback_snippet or text

        return FetchedPage(
            url=url,
            final_url=final_url,
            ok=True,
            text=text[: self._max_chars],
            title=_extract_title(html),
            published_at=_extract_published_at(html),
            status=status,
            images=_extract_images(html, final_url),
            og_image=_extract_og_image(html, final_url),
            captured_at=captured,
            degraded=degraded,
            error="" if not degraded else "正文抽取不完整，已回退到摘要",
        )

    def _degraded(
        self,
        url: str,
        snippet: str,
        captured: str,
        error: str,
        *,
        status: int = 0,
        final_url: str = "",
    ) -> FetchedPage:
        return FetchedPage(
            url=url,
            final_url=final_url or url,
            ok=False,
            text=snippet,
            status=status,
            captured_at=captured,
            degraded=True,
            error=error,
        )

    def close(self) -> None:
        self._client.close()


# ============================================================
# 抽取工具（模块级，便于单测）
# ============================================================


def _extract_main_text(html: str, url: str) -> str:
    """主正文。trafilatura 优先，失败退到按段落聚合。"""
    try:
        import trafilatura

        extracted = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_precision=False,
        )
        if extracted and len(extracted.strip()) >= _MIN_CHARS:
            return extracted.strip()
    except Exception as exc:  # noqa: BLE001 - 抽取器崩了不该让整次抓取失败
        log.debug("trafilatura 抽取失败 %s：%s", url, exc)

    return _paragraph_text(html)


def _paragraph_text(html: str) -> str:
    """BeautifulSoup 兜底：把块级文本按段落拼回来。

    比 trafilatura 糙，但比"返回空"好得多——它对 SPA 渲染后的静态壳、
    以及 trafilatura 判定为"非文章页"的文档站仍然有效。
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - 依赖缺失时明确降级而非崩溃
        return ""

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 - lxml 不可用时退到标准库解析器
        soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "template", "nav", "footer", "header", "form"]):
        tag.decompose()

    blocks: list[str] = []
    for element in soup.find_all(["p", "li", "h1", "h2", "h3", "h4", "blockquote", "td"]):
        chunk = element.get_text(" ", strip=True)
        # 太短的块多半是"阅读更多"这类按钮文字
        if len(chunk) >= 12:
            blocks.append(chunk)

    if not blocks:
        body = soup.body or soup
        return body.get_text("\n", strip=True)

    # 相邻重复段落去掉：响应式站点常把同一段渲染两遍
    deduped: list[str] = []
    for block in blocks:
        if not deduped or deduped[-1] != block:
            deduped.append(block)
    return "\n\n".join(deduped)


def _meta_content(html: str, **attrs: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        soup = BeautifulSoup(html, "html.parser")

    tag = soup.find("meta", attrs=attrs)
    if tag:
        return str(tag.get("content") or "").strip()
    return ""


def _extract_title(html: str) -> str:
    """og:title 优先：站点自己声明的分享标题通常比 <title> 干净
    （后者常带" - 首页"这类后缀）。"""
    for attrs in (
        {"property": "og:title"},
        {"name": "twitter:title"},
        {"name": "title"},
    ):
        value = _meta_content(html, **attrs)
        if value:
            return value[:300]

    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()[:300]
    return ""


#: 发布时间在页面里的常见位置。顺序即优先级。
_PUBLISHED_META: tuple[dict[str, str], ...] = (
    {"property": "article:published_time"},
    {"property": "og:published_time"},
    {"itemprop": "datePublished"},
    {"name": "publishdate"},
    {"name": "pubdate"},
    {"name": "date"},
    {"name": "weibo: article:create_at"},
)


def _extract_published_at(html: str) -> str:
    """抽取发布时间并归一化成 ISO 8601。

    归一化很重要：下游要用它算"证据时效性"，
    而原始值有 `2026年8月14日` / `2026/08/14` / 时间戳等多种形态，
    让每个调用方各自解析必然漂移。
    """
    for attrs in _PUBLISHED_META:
        value = _meta_content(html, **attrs)
        normalized = _normalize_date(value)
        if normalized:
            return normalized

    match = re.search(r"<time[^>]+datetime=[\"']([^\"']+)[\"']", html, re.I)
    if match:
        normalized = _normalize_date(match.group(1))
        if normalized:
            return normalized
    return ""


def _normalize_date(value: str) -> str:
    """归一化成 **UTC** 的 ISO 8601。

    为什么要转 UTC 而不是保留原时区：这个字段的用途是算"证据有多新"，
    而 `2026-08-14T00:00:00+08:00` 与 `2026-08-13T16:00:00Z` 是同一时刻。
    保留本地时区会让 freshness 分档在国内站点与海外站点之间产生系统性偏差。

    早先的实现把 `+08:00` 直接丢掉当成 UTC，等于把每条国内证据的时间认为晚了 8 小时——
    对按天/周分档影响不大，但它是**错的**，而且错得看不出来。
    """
    if not value:
        return ""
    raw = value.strip()

    # 纯数字时间戳（秒或毫秒）。
    # 位数下限不能省：页面里一个光秃秃的 `2026` 更可能是年份而不是 1970 年的第 2026 秒，
    # 按时间戳解会得到一个 1970 年的日期——错得离谱，而且因为它"看起来是个合法日期"，
    # 不会触发任何告警。真实时间戳至少 10 位（秒）或 13 位（毫秒）。
    if raw.isdigit() and len(raw) >= 10:
        number = int(raw)
        if number > 10**11:
            number //= 1000
        try:
            return datetime.fromtimestamp(number, tz=UTC).isoformat(timespec="seconds")
        except (OSError, ValueError, OverflowError):
            return ""

    # 先试标准 ISO：这条路径能保住时区偏移
    stamp: datetime | None = None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        stamp = None

    # 退回正则：处理 `2026年8月14日` 这类中文格式与非补零的月日
    if stamp is None:
        cleaned = (
            raw.replace("年", "-")
            .replace("月", "-")
            .replace("日", " ")
            .replace("/", "-")
            .strip()
        )
        match = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T]+(\d{1,2}):(\d{2}))?", cleaned)
        if not match:
            return ""
        year, month, day, hour, minute = match.groups()
        try:
            stamp = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0))
        except ValueError:
            return ""

    # 没带时区的按 UTC 处理：页面上的裸日期没有更多信息可用，
    # 硬猜一个 Asia/Shanghai 会让海外站点的日期同样出错，不如统一。
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).isoformat(timespec="seconds")


def _absolutize(src: str, base: str) -> str:
    """相对路径转绝对。图片大多是相对路径，存相对路径的话前端渲染不出来。"""
    if not src:
        return ""
    try:
        return str(httpx.URL(base).join(src))
    except Exception:  # noqa: BLE001
        return src


def _extract_og_image(html: str, base: str) -> str:
    for attrs in ({"property": "og:image"}, {"name": "twitter:image"}):
        value = _meta_content(html, **attrs)
        if value:
            return _absolutize(value, base)
    return ""


def _extract_images(html: str, base: str) -> list[ImageRef]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        soup = BeautifulSoup(html, "html.parser")

    images: list[ImageRef] = []
    seen: set[str] = set()
    for tag in soup.find_all("img"):
        src = str(tag.get("src") or tag.get("data-src") or tag.get("data-original") or "").strip()
        if not src or src.startswith("data:"):
            continue
        url = _absolutize(src, base)
        if url in seen:
            continue
        seen.add(url)
        images.append(ImageRef(url=url, alt=str(tag.get("alt") or "").strip()))
        if len(images) >= _MAX_IMAGES:
            break
    return images
