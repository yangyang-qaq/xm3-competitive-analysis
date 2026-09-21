"""来源分类与独立域名判定。

这张表是手维护的，所以它一定会漂移：新平台出现、老平台改名。
把它单独放一个文件、单独测，是为了让"要维护的东西在哪"一眼可见——
而不是散在评分函数中间的一个字典字面量。

**注意这里没有厂商名。** 表里是站点（`zhihu.com`），不是搜索源（`bocha`）。
两者容易混：站点是"这条材料来自哪里"，搜索源是"我们通过谁找到它的"。
可信度只关心前者。
"""
from __future__ import annotations

from urllib.parse import urlparse

#: 来源类型的基准分（0–60）。可信度总分里它占大头，因为
#: "谁说的"比"说得多长"更能决定一条材料值不值得引用。
#:
#: 分档的理由，逐条可辩护：
#: - `official` 官方文档/定价页：厂商对自己产品的陈述，事实性最强的来源，
#:   但**有立场**——它不会说自己产品的缺点。所以是 60 而不是 100。
#: - `news` 正规媒体：有编辑流程，但可能有通稿成分。
#: - `review` 垂直媒体/独立评测：有实测，但可能带商业合作。
#: - `zhihu`/`bilibili`/`xiaohongshu`/`douyin`：UGC。个体经验真实但不可推广，
#:   且平台算法会放大极端评价。抖音/小红书最低，因为视频与图文的
#:   文字信息密度最低，可核验性最差。
#: - `web` 个人博客：无编辑流程。
#: - `unknown` 无法归类：不能因为"认不出"就当成可信，也不能当垃圾——
#:   给一个偏低的中间值，并在明细里标注"来源类型未识别"。
SOURCE_TYPE_BASE: dict[str, float] = {
    "official": 60.0,
    "news": 50.0,
    "review": 46.0,
    "zhihu": 36.0,
    "bilibili": 32.0,
    "xiaohongshu": 26.0,
    "douyin": 24.0,
    "web": 28.0,
    "unknown": 22.0,
}

#: 域名后缀片段 → 来源类型。按**顺序**匹配，先精确后宽泛：
#: `www.zhihu.com` 与 `zhuanlan.zhihu.com` 都该归到知乎，
#: 而 `zhihu.com.evil.test` 不该——所以匹配的是主机名的**结尾标签**，
#: 不是子串包含。
_DOMAIN_RULES: tuple[tuple[str, str], ...] = (
    # 官方站点靠"域名里有没有品牌词"判断，见 classify_source
    ("36kr.com", "news"),
    ("tmtpost.com", "news"),
    ("huxiu.com", "news"),
    ("ifanr.com", "news"),
    ("geekpark.net", "news"),
    ("ithome.com", "news"),
    ("sspai.com", "review"),
    ("zealer.com", "review"),
    ("zhihu.com", "zhihu"),
    ("bilibili.com", "bilibili"),
    ("xiaohongshu.com", "xiaohongshu"),
    ("douyin.com", "douyin"),
    ("kuaishou.com", "douyin"),
    ("weibo.com", "xiaohongshu"),
    ("jianshu.com", "web"),
    ("csdn.net", "web"),
    ("cnblogs.com", "web"),
    ("medium.com", "web"),
    ("substack.com", "web"),
    ("wordpress.com", "web"),
    ("blogspot.com", "web"),
    ("github.io", "web"),
    ("notion.site", "web"),
)


def host_of(url: str) -> str:
    """取主机名，去掉 `www.`。

    去掉 `www.` 是必须的：`www.example.com` 与 `example.com`
    是同一个信源，不去掉的话"独立信源数"会被同一个站点刷高。
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def independent_domain(url: str) -> str:
    """独立信源的判据。

    用主机名而不是完整 URL：同一个站点下的两篇文章不是两个独立信源，
    它们共享同一套编辑立场与同一批信源。铁律二数的是"几个人说了同样的话"，
    不是"有几篇文章"。

    已知代价：`zhuanlan.zhihu.com` 与 `www.zhihu.com` 会被算成两个域名
    （子域不同）。对知乎专栏这类"同一平台不同作者"的情况，算成两个
    其实是合理的——不同作者确实是独立陈述。而如果是
    `docs.example.com` 与 `example.com`，算成两个会略微高估。
    权衡后选择主机名：误判为"独立"比误判为"同源"更安全，
    因为前者让交叉验证的门槛看起来更容易过（更保守的结论），
    后者会把真独立的信源当成一个而误报"未通过交叉验证"。
    """
    return host_of(url)


def classify_source(url: str, site_name: str = "", *, brand: str = "") -> str:
    """把 URL 归到一档来源类型。

    `brand` 传入时，"域名里含品牌词"的站点会被判为官方——
    这是识别官方站点的唯一可行办法，因为我们不可能维护一张
    "所有产品的官网域名"表。代价是会误判：一篇标题带品牌词的
    第三方文章如果域名里也有品牌词会被当成官方。所以要求
    **主机名的某个标签等于品牌词**，而不是包含。
    """
    host = host_of(url)
    if not host:
        return "unknown"

    labels = host.split(".")
    if brand:
        # 归一化：品牌名可能含空格/大写/中文。中文品牌名不会出现在域名里，
        # 所以只对 ASCII 部分做匹配。
        token = "".join(ch for ch in brand.lower() if ch.isalnum() or ch == "-")
        if token and token in labels:
            return "official"

    for suffix, source_type in _DOMAIN_RULES:
        if host == suffix or host.endswith("." + suffix):
            return source_type

    # 站点自带的名字里带"官方"字样——中文产品常见的自我标注。
    if site_name and ("官方" in site_name or "官网" in site_name):
        return "official"

    return "unknown" if site_name else "web"


def source_base_score(source_type: str) -> float:
    """未知类型不抛错，退回 `unknown` 档并偏低。

    抛错会让采集阶段整批失败；返回 0 会让数据静默降级成噪声。
    偏低值是唯一诚实的选项：它既参与排序，又不假装可信。
    """
    return SOURCE_TYPE_BASE.get(source_type, SOURCE_TYPE_BASE["unknown"])
