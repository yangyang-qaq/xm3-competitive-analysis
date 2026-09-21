"""正文质量评估：区分"一篇文章"和"一堆模板"。

存在的理由
----------
正文抽取器（trafilatura / 段落回退）会在**任何** HTML 上返回点什么。
它不区分"这是一篇 3000 字的评测"和"这是一个验证码页"——
后者也能抽出 400 字，而且看起来像正文。

下游的可信度评分如果只看长度，就会给验证码页打出一个中等的正文分。
所以长度必须乘上一个"有效信息占比"，而这个占比得有人算。

只输出比例，不输出结论
----------------------
这里不算"总分"，只给一个 `boilerplate` 比例和几条信号。原因是
这些信号在意的地方不同：可信度用它做折扣，采集阶段用它决定
要不要重抓，报告里用它做"降级披露"。让一个模块替所有人下结论，
等于把三种不同的判断压成一个数，谁都用不好。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

#: 页面级故障的标志词。命中即认定"这根本不是内容页"，
#: 直接把 boilerplate 顶到高位——不需要再去做比例计算。
#: 这些是抓取失败中最常见的几种，而且它们**返回 HTTP 200**：
#: 验证码页、限流页、需要 JS 的 SPA 空壳，状态码都是 200。
_PAGE_FAILURE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("验证码", ("请输入验证码", "滑动验证", "人机验证", "captcha", "verify you are human")),
    ("限流", ("访问过于频繁", "访问频率", "请稍后再试", "too many requests", "rate limit")),
    ("需JS", ("请开启 javascript", "请启用 javascript", "enable javascript",
              "javascript is required", "请升级您的浏览器")),
    ("不存在", ("页面不存在", "内容已删除", "该内容已被发布者删除", "page not found")),
)

#: 句子结束标点。一行以它结尾就算"像正文"。
_SENTENCE_END = "。！？.!?；;：:"

#: 短行阈值。短于它的行如果没有句子结束标点，按导航/标签行计。
_SHORT_LINE = 14

#: 重复行至少出现几次才计入噪声。2 次可能是正常的呼应，
#: 3 次以上一定是模板（"相关阅读"、"版权声明"）。
_REPEAT_MIN = 3

_URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class TextQuality:
    #: 模板噪声占正文字符的比例，0–1。可信度评分用它做折扣。
    boilerplate: float
    is_empty: bool
    signals: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "boilerplate": round(self.boilerplate, 3),
            "isEmpty": self.is_empty,
            "signals": list(self.signals),
        }


_EMPTY = TextQuality(1.0, True, ("正文为空",))


def assess_text(text: str, *, min_chars: int = 200) -> TextQuality:
    """评估一段正文。

    `min_chars` 与抓取器的阈值保持一致（默认 200）：两处用不同的阈值
    会导致"抓取认为合格、质量评估认为不合格"的边界地带，
    而那里的行为没人能预测。
    """
    if not text or not text.strip():
        return _EMPTY

    stripped = text.strip()
    if len(stripped) < min_chars:
        return TextQuality(
            0.5,
            False,
            (f"正文仅 {len(stripped)} 字，低于 {min_chars} 字阈值",),
        )

    for kind, markers in _PAGE_FAILURE_MARKERS:
        for marker in markers:
            if marker in stripped:
                # 整页都是"这是错误页"的内容，占比直接记满，不再算比例。
                return TextQuality(0.9, False, (f"正文含{kind}页面标记：{marker}",))

    signals: list[str] = []
    lines = [line.strip() for line in stripped.splitlines()]
    lines = [line for line in lines if line]
    total = len(stripped)
    noise = 0

    # 1) 高频重复行。模板的特征不是"出现了某个词"，而是"同一句话出现了很多次"。
    counts = Counter(line for line in lines if len(line) >= 4)
    repeated = {line for line, n in counts.items() if n >= _REPEAT_MIN}
    if repeated:
        noise += sum(len(line) * (counts[line] - 1) for line in repeated)
        signals.append(f"{len(repeated)} 行重复出现 3 次以上")

    # 2) 短行。导航栏、标签云、面包屑都是一行两个词。
    short_chars = sum(
        len(line)
        for line in lines
        if len(line) < _SHORT_LINE and not line.endswith(tuple(_SENTENCE_END))
    )
    if short_chars:
        noise += short_chars
        signals.append(f"短行合计 {short_chars} 字，疑似导航或标签")

    # 3) 裸链接。正文里不该密集成串出现 URL。
    url_chars = sum(len(m.group()) for m in _URL_RE.finditer(stripped))
    if url_chars > total * 0.05:
        noise += url_chars
        signals.append(f"裸链接占 {url_chars / total:.0%}")

    ratio = round(min(1.0, noise / total), 3)
    if not signals:
        signals.append("未检测到明显模板噪声")
    return TextQuality(ratio, False, tuple(signals))


def strip_boilerplate_lines(text: str, *, min_repeat: int = _REPEAT_MIN) -> str:
    """删掉高频重复行，返回更干净的正文。

    刻意**不做**别的清理：删短行会误伤列表式的正文（"支持 Windows"），
    而正文一旦被改坏，引用忠实度评估就失去了基准。
    宁可留着一点噪声，也不要让证据失真。
    """
    lines = text.splitlines()
    counts = Counter(line.strip() for line in lines if len(line.strip()) >= 4)
    repeated = {line for line, n in counts.items() if n >= min_repeat}
    if not repeated:
        return text
    kept = [line for line in lines if line.strip() not in repeated]
    return "\n".join(kept).strip()
