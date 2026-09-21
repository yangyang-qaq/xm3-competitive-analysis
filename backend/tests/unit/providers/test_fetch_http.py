"""抓取：编码嗅探、正文抽取降级、元信息抽取。

这些用例大多直接打模块级的私有函数。理由：编码与日期归一化是纯粹的字符串变换，
单独测它们比每次构造一个假 HTTP 响应清楚得多，失败时也能一眼看出坏在哪一步。
"""
from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.providers import retry as retry_mod
from app.providers.fetch.http import (
    HttpFetcher,
    _decode,
    _extract_title,
    _normalize_date,
    _paragraph_text,
)

_LONG_BODY = "这是一段用于测试的中文正文内容，需要足够长才能通过正文质量检查。" * 12


def _html(body: str = _LONG_BODY, *, head: str = "") -> str:
    return f"<html><head><title>默认标题</title>{head}</head><body><p>{body}</p></body></html>"


def _fetcher(handler, monkeypatch, **kwargs) -> HttpFetcher:
    monkeypatch.setattr(HttpFetcher, "retry_policy", retry_mod.NO_RETRY)
    return HttpFetcher(
        Settings(**kwargs),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _ok(html: str) -> httpx.Response:
    return httpx.Response(200, content=html.encode("utf-8"), headers={"content-type": "text/html"})


# ============================================================
# 编码
# ============================================================


def test_decode_prefers_real_encoding_over_bogus_latin1():
    """服务器不配编码时会默认发 iso-8859-1，而页面其实是 GBK。

    照它解会得到一堆乱码，且**不会报错**——乱码正文一路流进证据库，
    可信度照给、报告照印。所以这个默认值必须被忽略。
    """
    raw = "中文内容测试".encode("gb18030")
    assert _decode(raw, "iso-8859-1") == "中文内容测试"


def test_decode_detects_gb18030_when_nothing_is_declared():
    raw = "中文内容测试".encode("gb18030")
    assert _decode(raw, None) == "中文内容测试"


def test_decode_respects_declared_utf8():
    assert _decode("中文".encode(), "utf-8") == "中文"


def test_decode_reads_meta_charset_from_the_page():
    """HTTP 头说 utf-8、页面 meta 说 gbk 时，以页面为准：
    服务器把 charset 写错比页面写错更常见。"""
    raw = "<meta charset=\"gbk\">中文".encode("gb18030")
    assert "中文" in _decode(raw, "utf-8")


def test_decode_strips_utf8_bom():
    raw = "﻿中文".encode()
    decoded = _decode(raw, None)
    # BOM 会变成第一个字符，前端渲染时表现为标题前面多一个看不见的方块
    assert not decoded.startswith("﻿")


def test_gbk_page_through_a_full_fetch(monkeypatch):
    """端到端确认：GBK 页面、无 charset 头，抓出来的正文里没有替换字符。"""
    raw = _html().encode("gb18030")
    fetcher = _fetcher(
        lambda r: httpx.Response(200, content=raw, headers={"content-type": "text/html"}),
        monkeypatch,
    )
    page = fetcher.fetch("https://example.com/a")
    assert "这是一段用于测试的中文正文内容" in page.text
    assert "�" not in page.text


# ============================================================
# 日期归一化
# ============================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 带偏移的要**换算**到 UTC，而不是把偏移丢掉
        ("2026-08-14T00:00:00+08:00", "2026-08-13T16:00:00+00:00"),
        ("2026-08-14T00:00:00Z", "2026-08-14T00:00:00+00:00"),
        # 中文格式
        ("2026年8月14日", "2026-08-14T00:00:00+00:00"),
        ("2026年8月14日 15:30", "2026-08-14T15:30:00+00:00"),
        # 斜杠与非补零
        ("2026/08/14", "2026-08-14T00:00:00+00:00"),
        ("2026/8/4", "2026-08-04T00:00:00+00:00"),
        ("2026-08-14 15:30", "2026-08-14T15:30:00+00:00"),
        # 纯日期
        ("2026-08-14", "2026-08-14T00:00:00+00:00"),
    ],
)
def test_normalize_date(raw, expected):
    assert _normalize_date(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "昨天", "2026", "not a date", "13月45日"])
def test_normalize_date_rejects_garbage(raw):
    """宁可返回空，也不要猜一个日期出来——猜出来的日期会污染时效性评分。"""
    assert _normalize_date(raw) == ""


def test_normalize_date_handles_epoch_seconds_and_milliseconds():
    assert _normalize_date("1755100800") == _normalize_date("1755100800000")


def test_published_at_is_normalized_end_to_end(monkeypatch):
    html = _html(head='<meta property="article:published_time" content="2026年8月14日">')
    fetcher = _fetcher(lambda r: _ok(html), monkeypatch)
    assert fetcher.fetch("https://example.com/a").published_at == "2026-08-14T00:00:00+00:00"


def test_published_at_falls_back_to_time_element(monkeypatch):
    html = _html(head='<time datetime="2025-03-08T10:00:00+08:00">3月8日</time>')
    fetcher = _fetcher(lambda r: _ok(html), monkeypatch)
    assert fetcher.fetch("https://example.com/a").published_at == "2025-03-08T02:00:00+00:00"


def test_missing_published_at_is_empty_not_guessed(monkeypatch):
    fetcher = _fetcher(lambda r: _ok(_html()), monkeypatch)
    assert fetcher.fetch("https://example.com/a").published_at == ""


# ============================================================
# 元信息
# ============================================================


def test_og_title_wins_over_title_tag():
    """站点自己声明的分享标题通常比 <title> 干净——后者常带" - 首页"这类后缀。"""
    html = '<html><head><meta property="og:title" content="干净的标题"><title>标题 - 某某网</title></head></html>'
    assert _extract_title(html) == "干净的标题"


def test_title_tag_used_when_no_og():
    assert _extract_title("<html><head><title>普通标题</title></head></html>") == "普通标题"


def test_images_are_absolutized(monkeypatch):
    """相对路径的图片存进去，前端渲染不出来。"""
    html = (
        "<html><head><title>t</title>"
        '<meta property="og:image" content="/og.png"></head>'
        f'<body><p>{_LONG_BODY}</p><img src="images/a.png" alt="图A">'
        '<img src="https://cdn.test/b.png" alt="图B"></body></html>'
    )
    fetcher = _fetcher(lambda r: _ok(html), monkeypatch)
    page = fetcher.fetch("https://example.com/post/1")

    assert page.og_image == "https://example.com/og.png"
    urls = [img.url for img in page.images]
    assert "https://example.com/post/images/a.png" in urls
    assert "https://cdn.test/b.png" in urls
    assert page.images[0].alt == "图A"


def test_data_uri_images_are_skipped(monkeypatch):
    """内联 base64 图片存进证据库只会把库撑爆，没有任何佐证价值。"""
    html = (
        "<html><head><title>t</title></head><body>"
        f"<p>{_LONG_BODY}</p>"
        '<img src="data:image/png;base64,iVBORw0KGgo=" alt="内联">'
        "</body></html>"
    )
    fetcher = _fetcher(lambda r: _ok(html), monkeypatch)
    assert fetcher.fetch("https://example.com/a").images == []


def test_duplicate_images_collapse(monkeypatch):
    html = (
        "<html><head><title>t</title></head><body>"
        f"<p>{_LONG_BODY}</p>"
        '<img src="https://cdn.test/a.png"><img src="https://cdn.test/a.png">'
        "</body></html>"
    )
    fetcher = _fetcher(lambda r: _ok(html), monkeypatch)
    assert len(fetcher.fetch("https://example.com/a").images) == 1


# ============================================================
# 降级路径
# ============================================================


def test_short_extraction_degrades_to_the_search_snippet(monkeypatch):
    """正文太短多半是验证码页或导航壳。

    此时**必须**显出 degraded=True：拿摘要当证据和拿正文当证据不是一个可信度等级，
    这个区别一旦丢了，报告会显得比实际更有把握。
    """
    fetcher = _fetcher(lambda r: _ok("<html><body><p>短。</p></body></html>"), monkeypatch)
    page = fetcher.fetch("https://example.com/a", fallback_snippet="搜索摘要内容")

    assert page.degraded is True
    assert page.text == "搜索摘要内容"
    assert page.error


def test_http_error_degrades_with_snippet(monkeypatch):
    fetcher = _fetcher(lambda r: httpx.Response(404, text="gone"), monkeypatch)
    page = fetcher.fetch("https://example.com/a", fallback_snippet="搜索摘要内容")

    assert page.ok is False
    assert page.degraded is True
    assert page.status == 404
    assert page.text == "搜索摘要内容"
    assert "404" in page.error


def test_transport_failure_degrades_instead_of_raising(monkeypatch):
    """抓取失败不该中断整批采集。一个 URL 挂了，剩下几十个还得继续跑。"""

    def boom(request):
        raise httpx.ConnectError("连不上", request=request)

    fetcher = _fetcher(boom, monkeypatch)
    page = fetcher.fetch("https://example.com/a", fallback_snippet="兜底摘要")

    assert page.ok is False
    assert page.degraded is True
    assert page.text == "兜底摘要"


def test_non_html_content_is_degraded(monkeypatch):
    fetcher = _fetcher(
        lambda r: httpx.Response(
            200, json={"a": 1}, headers={"content-type": "application/json"}
        ),
        monkeypatch,
    )
    page = fetcher.fetch("https://api.example.com/x", fallback_snippet="摘要")
    assert page.degraded is True
    assert "非 HTML" in page.error


def test_captured_at_is_set_even_when_degraded(monkeypatch):
    """降级也要记采集时间：时效性评分依赖它，缺了就只能按"很旧"处理。"""
    fetcher = _fetcher(lambda r: httpx.Response(500, text="err"), monkeypatch)
    page = fetcher.fetch("https://example.com/a", fallback_snippet="s")
    assert page.captured_at.startswith("20")


def test_text_is_truncated_to_the_configured_limit(monkeypatch):
    """正文要进 LLM 上下文，不设上限的话一份长文就能吃掉整个窗口。"""
    fetcher = _fetcher(lambda r: _ok(_html("很长的一段话。" * 5000)), monkeypatch, fetch_max_chars=500)
    page = fetcher.fetch("https://example.com/a")
    assert len(page.text) == 500


# ============================================================
# 段落兜底
# ============================================================


def test_paragraph_fallback_collects_block_text():
    # 两块文本都要超过 12 字：短于这个长度的块会被当成按钮文字丢掉
    html = (
        "<html><body><h1>章节标题</h1>"
        "<p>第一段足够长的中文正文内容</p>"
        "<ul><li>这里的列表项也需要写得足够长</li></ul></body></html>"
    )
    text = _paragraph_text(html)
    assert "第一段足够长的中文正文内容" in text
    assert "这里的列表项也需要写得足够长" in text


def test_paragraph_fallback_drops_scripts_and_nav():
    html = (
        "<html><body><nav>导航栏里的很长一段文字</nav>"
        "<script>var x = '一段很长的脚本内容不该出现';</script>"
        "<p>正文段落足够长</p></body></html>"
    )
    text = _paragraph_text(html)
    assert "正文段落足够长" in text
    assert "脚本内容" not in text
    assert "导航栏" not in text


def test_paragraph_fallback_drops_short_button_text():
    html = "<html><body><p>阅读更多</p><p>正文段落足够长的内容示例</p></body></html>"
    assert "阅读更多" not in _paragraph_text(html)


def test_paragraph_fallback_collapses_adjacent_duplicates():
    """响应式站点常把同一段渲染两遍（桌面版 + 移动版），不去重会浪费上下文。"""
    block = "同一段足够长的正文内容示例"
    html = f"<html><body><p>{block}</p><p>{block}</p></body></html>"
    assert _paragraph_text(html).count(block) == 1
