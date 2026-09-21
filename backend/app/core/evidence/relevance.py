"""相关性过滤。

为什么它不在适配器里
--------------------
参考实现把这段逻辑写在搜索适配器内部。后果是：换一家搜索源，这段过滤
**会静默消失**——新适配器没抄这段，报告里就多出一堆无关结果，
而且没有任何地方会报错。这恰恰是适配层要防的事：适配器的职责是
"把厂商方言翻译成归一化结果"，不是"决定什么算相关"。

过滤分两层，**判据完全不同**：

- `keep` 只由硬规则决定（URL 非法 / 命中噪声词 / 完全没提到调研对象）。
  每一条都能指着具体的字符串说"这条为什么被丢"。
- `score` 是连续的打分，只用于**排序与报告**，不用于丢弃。

这个划分是刻意的。如果用一个阈值（"分数低于 0.35 就丢"）来过滤，
那么阈值附近的结果会时丢时留，而没人能解释为什么——
一个不能解释的过滤器和随机丢弃没有区别。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 噪声词。命中标题即丢弃。
#:
#: 分类记录理由，因为这张表会被反复调整，而"为什么加这个词"
#: 比"加了什么词"更容易忘：
#: - 招聘类：搜产品名常常撞上这家公司的招聘页
#: - 下载/破解类：命中即垃圾，且是版权风险
#: - 促销类：返利/优惠券站点内容无信息量
#: - 登录/错误页：正文抽取必然失败，抓了也是白抓
_NOISE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("招聘", ("招聘", "招人", "职位", "内推", "校招", "社招", "简历投递", "hiring", "careers")),
    ("下载破解", ("破解", "免费下载", "下载地址", "绿色版", "crack", "keygen", "serial key", "nulled")),
    ("促销", ("优惠券", "返利", "折扣码", "领券", "coupon", "promo code")),
    ("登录页", ("登录", "注册账号", "sign in", "log in", "sign up")),
    ("错误页", ("404", "页面不存在", "访问被拒绝", "access denied", "403 forbidden")),
)

#: 平台名。这些词出现在标题里不代表相关——搜索引擎的"相关推荐"栏
#: 会把平台名拼进标题，但页面讲的是别的事。
#: 不直接丢弃，只在打分时降权。
_PLATFORM_WORDS = ("知乎", "微博", "抖音", "小红书", "bilibili", "哔哩哔哩")

_MIN_SNIPPET_FOR_BONUS = 60


@dataclass(frozen=True)
class RelevanceVerdict:
    keep: bool
    score: float
    reasons: tuple[str, ...]

    @property
    def label(self) -> str:
        if not self.keep:
            return "丢弃"
        if self.score >= 0.75:
            return "高相关"
        if self.score >= 0.5:
            return "中等相关"
        return "弱相关"


def _hits(text: str, needles: tuple[str, ...]) -> str:
    """返回第一个命中的词，没有则空串。"""
    lowered = text.lower()
    for needle in needles:
        if needle.lower() in lowered:
            return needle
    return ""


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and needle.lower() in haystack.lower()


def judge_relevance(
    *,
    url: str,
    title: str = "",
    snippet: str = "",
    brand: str = "",
    dimension: str = "",
    extra_tokens: tuple[str, ...] = (),
) -> RelevanceVerdict:
    """判定一条搜索结果值不值得留下。

    `brand` 为空时跳过"是否提及调研对象"这条规则——品类搜索
    （"知识管理工具对比"）本来就不该要求每条结果都提到某个具体品牌。
    """
    reasons: list[str] = []

    if not url.lower().startswith(("http://", "https://")):
        return RelevanceVerdict(False, 0.0, ("URL 不是 http(s)",))

    noise_kind = _hits(title, tuple(w for _, words in _NOISE_PATTERNS for w in words))
    if noise_kind:
        for kind, words in _NOISE_PATTERNS:
            if noise_kind in words:
                return RelevanceVerdict(False, 0.0, (f"标题命中「{kind}」噪声词：{noise_kind}",))
        return RelevanceVerdict(False, 0.0, (f"标题命中噪声词：{noise_kind}",))

    haystack = f"{title}\n{snippet}\n{url}"
    in_title = _contains(title, brand)
    in_body = _contains(f"{title}\n{snippet}", brand)

    if brand and not _contains(haystack, brand):
        # 这是唯一一条会把结果整条丢掉的软规则。它值得这个力度：
        # 调研对象完全没被提到的页面，对"这个产品怎么样"没有回答能力。
        return RelevanceVerdict(False, 0.0, (f"全文未提及调研对象「{brand}」",))

    score = 0.0
    if brand:
        if in_title:
            score += 0.35
            reasons.append("标题含调研对象")
        elif in_body:
            score += 0.25
            reasons.append("摘要含调研对象")
        else:
            # 只在 URL 里出现（比如 /notion-review）：很可能是导航页或列表页。
            score += 0.1
            reasons.append("仅在 URL 中提及调研对象")
    else:
        score += 0.3
        reasons.append("品类检索，无品牌要求")

    if dimension and _contains(f"{title}\n{snippet}", dimension):
        score += 0.25
        reasons.append(f"命中维度「{dimension}」")

    for token in extra_tokens:
        if _contains(f"{title}\n{snippet}", token):
            score += 0.16
            reasons.append(f"命中关键词「{token}」")

    if len(snippet) >= _MIN_SNIPPET_FOR_BONUS:
        score += 0.1
        reasons.append("摘要信息量充足")

    platform_word = _hits(title, _PLATFORM_WORDS)
    if platform_word and brand and not in_title:
        # 标题里只有平台名没有品牌名，多半是"XX 平台上的相关推荐"。
        score -= 0.15
        reasons.append(f"标题仅含平台名「{platform_word}」")

    score = round(max(0.0, min(1.0, score)), 3)
    return RelevanceVerdict(True, score, tuple(reasons))
