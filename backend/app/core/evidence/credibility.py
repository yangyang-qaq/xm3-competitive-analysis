"""可解释的可信度评分。

一个数字为什么不够
------------------
参考实现返回一个裸整数。后果是"这条为什么 82 分"在界面上、报告里、
答辩时都说不出来——而一个说不出来的数字，读者只能选择信或不信，
不能质疑。评分从"判断"退化成"权威"。

所以这里返回 `CredibilityBreakdown`，四个分项相加再减掉扣分项，
**总分必须等于各项之和**（有一条测试守这个恒等式）。
于是任何一次评分都可以摊开看：来源占了多少、时效占了多少、
扣在哪里。争议就从"这个分数对不对"变成"来源该给 60 还是 50"——
后者是可以讨论的。

绝不做的事
----------
参考实现用 `(len(domain) % 4) - 1` 微调分数，只为让数字看起来不那么整。
那是在制造一个假的方差：它看起来像信号，实际是哈希噪声，
而且它让"这两个来源的可信度差 1 分"变得完全没有意义。
这里每一分都来自一个能指出来的事实。

四个分项的量纲是**设计**出来的，不是拟合出来的：来源 60 分（谁说的最重要）、
时效 15 分、正文 15 分、交叉印证 10 分。这个分配是可辩护的，也是可调的——
关键是它在代码里是一个常量表，不在某个函数的算术中间。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta

from app.core.evidence.sourcetypes import independent_domain, source_base_score
from app.core.models import CredibilityBreakdown, Evidence

# ============================================================
# 分项满分
# ============================================================

MAX_SOURCE = 60.0
MAX_FRESHNESS = 15.0
MAX_CONTENT = 15.0
MAX_CROSS_REF = 10.0

#: 时效分档：(最大年龄天数, 得分)。顺序敏感，取第一个命中的档。
_FRESHNESS_BANDS: tuple[tuple[int, float], ...] = (
    (30, 15.0),
    (90, 13.0),
    (180, 11.0),
    (365, 8.0),
    (730, 5.0),
)
_FRESHNESS_FLOOR = 2.0
#: 没有发布时间。给 6 分而不是 0：它确实没法证明自己新鲜，
#: 但"没写日期"和"三年前的日期"是不同的坏法，不该得同一个分。
_FRESHNESS_UNKNOWN = 6.0
#: 日期在未来。多半是页面写错或时区解析出错，不按"最新"给满分。
_FRESHNESS_FUTURE = 12.0

#: 正文长度分档：(最大字符数, 得分)。顺序敏感。
_CONTENT_BANDS: tuple[tuple[int, float], ...] = (
    (0, 3.0),
    (200, 5.0),
    (800, 9.0),
    (2500, 12.0),
    (8000, 14.0),
)
_CONTENT_CEILING = 15.0

#: 交叉印证分档：(独立域名数, 得分)。1 个域名得 0 分——
#: 它不是"扣分"，而是"没有加分项"，这两件事在报告里的措辞完全不同。
_CROSS_REF_BANDS: tuple[tuple[int, float], ...] = (
    (3, 10.0),
    (2, 7.0),
)

# 扣分项
_PENALTY_DEGRADED = -10.0
_PENALTY_UNKNOWN_SOURCE = -6.0
_PENALTY_NO_TITLE = -3.0
_PENALTY_BAD_URL = -25.0
_PENALTY_BOILERPLATE = -8.0

#: 判定"日期离谱"的提前量。服务器时间与页面时间差几小时是正常的。
_FUTURE_TOLERANCE = timedelta(days=2)


def _parse_dt(raw: str) -> datetime | None:
    """解析时间戳。失败返回 None 而不是抛错。

    一条日期格式奇怪的证据不该让整个采集阶段崩掉——它只是拿不到时效分。
    """
    if not raw:
        return None
    text = raw.strip()
    if text.isdigit() and len(text) >= 10:
        # 秒或毫秒时间戳。位数下限不能省：页面里一个光秃秃的 `2026`
        # 更可能是年份，按时间戳解会得到一个 1970 年的日期。
        value = int(text[:13])
        if len(text) >= 13:
            value //= 1000
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp


def _freshness_score(published_at: str, now: datetime) -> tuple[float, str]:
    stamp = _parse_dt(published_at)
    if stamp is None:
        return _FRESHNESS_UNKNOWN, "无发布时间，时效分按未知计"
    age = now - stamp
    if age < -_FUTURE_TOLERANCE:
        return _FRESHNESS_FUTURE, f"发布时间在未来（{published_at}），不按最新计分"
    days = max(0, age.days)
    for limit, score in _FRESHNESS_BANDS:
        if days <= limit:
            return score, f"发布于 {days} 天前"
    return _FRESHNESS_FLOOR, f"发布于 {days} 天前，已明显过时"


def _content_score(ev: Evidence, boilerplate: float) -> tuple[float, str]:
    # 加了 boilerplate 折扣的长度：一份 5000 字的正文如果 80% 是导航模板，
    # 它的有效信息量不如一篇 1500 字的干净文章。
    effective = int(len(ev.full_text) * (1.0 - boilerplate))
    for limit, score in _CONTENT_BANDS:
        if effective <= limit:
            return score, f"有效正文约 {effective} 字"
    return _CONTENT_CEILING, f"有效正文约 {effective} 字"


def _cross_ref_score(ev: Evidence, peers: Iterable[Evidence]) -> tuple[float, str]:
    domains = {independent_domain(ev.url)}
    for peer in peers:
        if peer.evidence_id == ev.evidence_id:
            continue
        peer_domain = independent_domain(peer.url)
        if peer_domain:
            domains.add(peer_domain)
    domains.discard("")
    count = len(domains)
    for need, score in _CROSS_REF_BANDS:
        if count >= need:
            return score, f"同一议题有 {count} 个独立域名覆盖"
    return 0.0, "仅 1 个独立域名覆盖，未通过交叉印证"


def score_evidence(
    ev: Evidence,
    *,
    peers: Sequence[Evidence] = (),
    now: datetime | None = None,
    boilerplate: float = 0.0,
) -> CredibilityBreakdown:
    """给一条证据打分，返回可摊开的分项明细。

    `peers` 是**同一品牌同一维度**下的其它证据。交叉印证必须限定在同一议题内——
    拿全库的证据数来加分，会让一个热门话题下的每条证据都自动拿满分，
    而"交叉验证"的意思恰恰是"这件事有别人也说了"。

    `boilerplate` 是正文里模板噪声的占比（0–1），来自 `textquality`。
    用比例而不是布尔量传入，是为了让"这篇 30% 是导航栏"和
    "这篇 90% 是导航栏"得到不同的分数。
    """
    now = now or datetime.now(UTC)
    source_type = ev.source_type or "unknown"
    notes: list[str] = []

    source_score = source_base_score(source_type)
    if source_type == "unknown":
        notes.append("来源类型未识别，按最低档计分")

    fresh_score, fresh_note = _freshness_score(ev.published_at, now)
    notes.append(fresh_note)

    content_score, content_note = _content_score(ev, boilerplate)
    notes.append(content_note)

    cross_score, cross_note = _cross_ref_score(ev, peers)
    notes.append(cross_note)

    penalties = 0.0
    if ev.degraded:
        penalties += _PENALTY_DEGRADED
        notes.append(f"正文抓取失败，仅有搜索摘要（{_PENALTY_DEGRADED:+.0f}）")
    if source_type == "unknown":
        penalties += _PENALTY_UNKNOWN_SOURCE
        notes.append(f"来源无法归类（{_PENALTY_UNKNOWN_SOURCE:+.0f}）")
    if not ev.title.strip():
        penalties += _PENALTY_NO_TITLE
        notes.append(f"无标题（{_PENALTY_NO_TITLE:+.0f}）")
    if not ev.url.lower().startswith(("http://", "https://")):
        penalties += _PENALTY_BAD_URL
        notes.append(f"URL 非法（{_PENALTY_BAD_URL:+.0f}）")
    if boilerplate > 0.5:
        penalties += _PENALTY_BOILERPLATE
        notes.append(f"正文中模板噪声占比 {boilerplate:.0%}（{_PENALTY_BOILERPLATE:+.0f}）")

    return CredibilityBreakdown(
        source_type_score=round(source_score, 2),
        freshness_score=round(fresh_score, 2),
        content_score=round(content_score, 2),
        cross_ref_score=round(cross_score, 2),
        penalties=round(penalties, 2),
        notes=notes,
    )


def apply_score(
    ev: Evidence,
    *,
    peers: Sequence[Evidence] = (),
    now: datetime | None = None,
    boilerplate: float = 0.0,
) -> Evidence:
    """打分并写回证据对象。返回同一个对象，方便链式调用。"""
    breakdown = score_evidence(ev, peers=peers, now=now, boilerplate=boilerplate)
    ev.credibility = breakdown.total
    ev.credibility_breakdown = breakdown
    return ev


def score_batch(
    evidences: Sequence[Evidence],
    *,
    now: datetime | None = None,
    boilerplate: dict[str, float] | None = None,
) -> list[Evidence]:
    """给一批证据打分，自动按 (品牌, 维度) 分组算交叉印证。

    分组的键是 `(brand, dimension)`：同一条证据可能命中多个维度，
    那它就跟所有命中维度的证据互为 peer。这样"同议题目击者"的定义
    与维度覆盖率的口径一致——两处用同一个定义，报告里的两个数字才互相对得上。
    """
    now = now or datetime.now(UTC)
    boilerplate = boilerplate or {}

    buckets: dict[tuple[str, str], list[Evidence]] = {}
    for ev in evidences:
        for dimension in ev.matched_dimensions or [""]:
            buckets.setdefault((ev.brand, dimension), []).append(ev)

    for ev in evidences:
        peers: list[Evidence] = []
        for dimension in ev.matched_dimensions or [""]:
            for candidate in buckets.get((ev.brand, dimension), ()):
                if candidate.evidence_id != ev.evidence_id and candidate not in peers:
                    peers.append(candidate)
        apply_score(
            ev,
            peers=peers,
            now=now,
            boilerplate=boilerplate.get(ev.evidence_id, 0.0),
        )
    return list(evidences)


def band_of(total: float) -> str:
    """把分数归到可读的档位。报告里显示"较高（74）"比只显示"74"更好懂，
    也更容易在正文里被引用。"""
    if total >= 75:
        return "高"
    if total >= 55:
        return "较高"
    if total >= 35:
        return "中等"
    return "偏低"
