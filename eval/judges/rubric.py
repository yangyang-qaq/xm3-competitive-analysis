"""报告可用性评分：5 个维度各 1–5 分。

为什么是这五个维度
------------------
不是"全面性 / 深度 / 准确性"这类听起来对但没法判的维度——
每一条都必须能被裁判**指着报告里的某处**给出分数，否则它量的就是
"这份报告读起来专不专业"，而那和长度、术语密度高度相关，
最后会变成一个奖励废话的指标。

| 维度 | 判的是什么 | 扣分的典型形态 |
|---|---|---|
| 结构完整性 | 该有的章节在不在、有没有结论 | 有矩阵没结论，读者自己总结 |
| 论证充分性 | 论点有没有被证据撑住 | 结论对但后面没有引用 |
| 可操作性 | 读完能不能做一个决定 | 全是描述，没有"所以呢" |
| 区分度 | 对比有没有真的分出高下 | 功能矩阵一片 4 分 |
| 诚实度 | 不确定与降级有没有说出来 | 证据稀薄却写成板上钉钉 |

**「诚实度」这一维是这个 rubric 里最有意的一格。** 多数报告评测的
rubric 到这里就结束了，而 xm3 的四条铁律里有一条就是"不确定要说出来"。
一份证据稀薄但把自己说得很确定的报告应当在这一维拿低分——
不然"诚实"就只是文档里的一句话，没人真的量它。

裁判看不到指标
--------------
递给裁判的只有报告正文，**不给它看 `metrics`**。给了它就会去抄
"维度覆盖率 0.83"然后给论证充分性打 4 分——那是把确定性指标
换了个说法再说一遍，判官指标就白跑了。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("structure", "结构完整性：该有的章节在不在，有没有给出结论"),
    ("argument", "论证充分性：主要论点是否被引用的证据支撑"),
    ("actionable", "可操作性：读者读完能不能做出一个具体决定"),
    ("differentiation", "区分度：对比是否真的分出了高下，而不是一片相同"),
    ("honesty", "诚实度：证据不足、降级、不确定的地方有没有如实说明"),
)

#: 递给裁判的正文上限。整份报告 JSON 常有几百 KB，
#: 全给会撑爆上下文、也会让裁判只读开头。截断要说明，
#: 否则它会拿"后面没写"去扣分。
BODY_CHARS = 24_000

#: 一份报告最多问裁判几次（含第一次）。
#:
#: 为什么不是 1：`--live --judge` 里 `notion-vs-obsidian` 这一份报告
#: **6 次问了 6 次读不出来**，而且失败形态每次都不一样——
#:   ① `stop` · 874 字 · `{`×6 `}`×5  → 答完了，根对象少一个右括号
#:   ② `stop` · 731 字              → 停在 `"actionable": ` 后面，句子断在半截
#:   ③ 停在字符串中间                 → 连维度键都只出现了 2 个
#: ①可以靠补括号救（`_close_unbalanced` 已经救回来了），②③救不回来：
#: 它们不是"收尾没写完"，是**模型没答完就停了**，补什么都得靠编。
#: 所以修法不是继续加 `max_tokens`（那是第一次猜错的假设），
#: 而是**再问一次**——这不是掩盖失败，是承认单次采样不可靠。
#:
#: 为什么不是 3 或更多：重试是花钱的，而这里要的是一个**能报出去的
#: 失败率**（"33% 的判官输出第一次读不出来，重试后降到 X%"），
#: 不是"把失败藏到零"。留 2 次，剩下的如实记成解析失败。
_MAX_ATTEMPTS = 2

SYSTEM_PROMPT = """你是竞品分析报告的质量评审。给下面的报告按 5 个维度各打 1–5 分。

打分标准：
5 = 这一维几乎无可挑剔
4 = 好，有小瑕疵
3 = 及格，明显能改进
2 = 差，这一维基本没做到
1 = 完全没有这一维

规则：
1. **必须能指出报告里的具体位置**作为给分依据。指不出来就给 3 分。
2. 不要因为报告长就给高分，也不要因为用词朴素就扣分。
3. 五个维度独立打分，不要都打同一个分。
4. 只输出 JSON，不要输出别的。

输出格式：
{"structure": {"score": 4, "reason": "一句话"},
 "argument": {"score": 4, "reason": "..."},
 "actionable": {"score": 3, "reason": "..."},
 "differentiation": {"score": 5, "reason": "..."},
 "honesty": {"score": 3, "reason": "..."}}"""


@dataclass
class RubricScore:
    dimension: str
    score: int
    reason: str = ""


@dataclass
class RubricResult:
    report: str
    scores: list[RubricScore] = field(default_factory=list)
    #: 只在**解析失败**时非空：`[finish_reason=... len=...]` 加上判官原文的前 1200 字。
    #: 目的不是展示，是让"为什么这次没解析出来"在事后**还能回答**。
    raw: str = ""
    #: 这份分数是从**补过括号**的输出里解析出来的（见 `_close_unbalanced`）。
    #: 单独标出来，是因为"补了一个括号才读出来的 4 分"和"模型本来就这么答的 4 分"
    #: 在数字上完全一样，而它们的可信度不是一回事。
    repaired: bool = False
    #: 问了几次才拿到能解析的输出。>1 说明第一次没答完（见 `_MAX_ATTEMPTS`）。
    #: 同样要标出来：一个"问两次才有的分"和一个"一次就有的分"不该长得一样。
    attempts: int = 1

    @property
    def mean(self) -> float:
        return (
            sum(s.score for s in self.scores) / len(self.scores) if self.scores else 0.0
        )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "report": self.report,
            "mean": round(self.mean, 3),
            "scores": {s.dimension: s.score for s in self.scores},
            "reasons": {s.dimension: s.reason for s in self.scores},
        }
        if not self.scores and self.raw:
            out["unparsed"] = self.raw
        if self.repaired:
            out["repairedBraces"] = True
        if self.attempts > 1:
            out["attempts"] = self.attempts
        return out


def summarize(body: dict[str, Any], *, limit: int = BODY_CHARS) -> str:
    """把报告正文压成裁判能读的一段。

    刻意**不**只发 `sections` 的正文——那样裁判看不到矩阵与表格，
    而"区分度"那一维正是判矩阵的。所以发的是整份 JSON，
    只是砍掉体积最大的证据与思维流：它们是过程数据，
    判官要看的是产出。
    """
    import json

    trimmed = {
        k: v
        for k, v in body.items()
        if k not in ("evidences", "thoughts", "messages", "gallery", "coercion", "metrics")
    }
    # 证据正文最大头，但矩阵/表格里要引用它，所以留一份**只有标题与来源**的索引。
    trimmed["evidencesIndex"] = [
        {
            "evidenceId": ev.get("evidenceId"),
            "title": ev.get("title"),
            "url": ev.get("url"),
            "sourceType": ev.get("sourceType"),
            "credibility": ev.get("credibility"),
        }
        for ev in (body.get("evidences") or [])
    ]

    text = json.dumps(trimmed, ensure_ascii=False, indent=1)
    if len(text) > limit:
        text = text[:limit] + "\n……（正文截断，后面还有内容）"
    return text


def judge_report(
    body: dict[str, Any],
    *,
    name: str,
    tier: str = "core",
    temperature: float = 0.0,
    llm: Any = None,
) -> RubricResult:
    """给一份报告打分。`llm` 传 None 时取当前配置的 provider。"""
    from app.providers.base import ChatMessage

    if llm is None:
        from app.providers.registry import get_llm

        llm = get_llm()

    prompt = (
        "请评审下面这份竞品分析报告。\n\n"
        + "\n".join(f"- {key}：{desc}" for key, desc in DIMENSIONS)
        + "\n\n报告（JSON）：\n"
        + summarize(body)
    )

    messages = [
        ChatMessage(role="system", content=SYSTEM_PROMPT),
        ChatMessage(role="user", content=prompt),
    ]

    attempts = 0
    diagnosis: list[str] = []
    response: Any = None
    scores: list[RubricScore] = []
    repaired = False
    while attempts < _MAX_ATTEMPTS:
        attempts += 1
        response = llm.chat(
            messages,
            tier=tier,  # type: ignore[arg-type]
            temperature=temperature,
            # 2000 而不是 800：5 个维度各要一句 reason，实测每句 90–150 字，
            # 5 × 150 个汉字加 JSON 骨架已经贴着 800 了，留出余量。
            #
            # 但**这个数不是那几次解析失败的修复**——它曾经被当成过，
            # 而实测证明那是错的。当时的推理是"输出被 800 截断了"，
            # 听起来很合理（失败的那几篇恰好正文最长），于是把上限提到 2000，
            # 重跑，**照样失败**。真实情况比一个原因复杂，见 `_MAX_ATTEMPTS`。
            max_tokens=2000,
            json_mode=True,
            purpose="eval.rubric",
        )
        scores, repaired = _parse(response.text)
        if scores:
            break
        diagnosis.append(_diagnose(response))

    if not scores:
        # 解析失败时留下**能判读的那几个事实**，而不是只有一句"没解析出来"。
        # 只有原文不行：原文长 300 字和长 6000 字都只是"一段 JSON"，
        # 分不出"被砍断了"和"模型压根没按格式答"。
        # `finish_reason` 与长度才是判据——`length` 说明是被砍断的，
        # `stop` 说明模型认为自己答完了，那就是格式问题。
        # 加上括号计数：`{` 比 `}` 多几个，一眼看出是不是"少收尾"这种伤。
        # 每次尝试都留一条，因为同一个 body 的失败形态**并不稳定**。
        return RubricResult(
            report=name, scores=[], raw=" | ".join(diagnosis), attempts=attempts
        )
    return RubricResult(
        report=name, scores=scores, repaired=repaired, attempts=attempts
    )


def _diagnose(response: Any) -> str:
    """把一次失败的回答压成一行判据：结束原因 + 长度 + 括号计数 + 原文开头。"""
    text = response.text or ""
    return (
        f"[finish_reason={response.finish_reason} len={len(text)} "
        f"braces={text.count('{')}/{text.count('}')}] " + text[:1200]
    )


def _close_unbalanced(text: str) -> str:
    """给**少了收尾括号**的 JSON 补上右括号。补不动就原样返回。

    为什么要做这件事：`--live --judge` 第一批 15 份里有 5 份解析失败，
    当时记成"可用性 0.00/5"。捞出的原文是 `finish_reason=stop len=874`、
    `{` × 6 / `}` × 5 —— 模型**答完了**，只是根对象的右括号没吐出来。
    一次采样少一个字符，代价是 5 份报告被记成 0 分：

        {"structure": {…}, "argument": {…}, … "honesty": {…}      ← 875 号位置本该是 }
                                                                     实际是字符串末尾

    **只补不猜。** 这个函数一个字都不改、一个维度都不编；它唯一做的事
    是把已经写完的内容收口。所以它救得回的只有"末尾少几个右括号"
    这一种伤，其余一律返回原文，交给调用方按解析失败处理。

    刻意不做的事：
    - 判不了时**不抛**，返回原文——补括号是尽力而为，不是新的失败点。
    - 不处理字符串内的花括号：`in_string` 会跳过它们，
      否则 reason 里写一句 `用 {a,b} 表示` 就会被当成结构字符。
    - 超过 3 层不补。缺 4 个以上右括号说明这段输出坏得不止是收尾，
      这时候"补好再读"更可能读出一份**看着没问题**的错分数。
    """
    import json

    start = text.find("{")
    if start < 0:
        return text
    body = text[start:]

    stack: list[str] = []
    in_string = False
    escaped = False
    pairs = {"}": "{", "]": "["}
    for ch in body:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack[-1] != pairs[ch]:
                return text  # 括号对不上，不是"没写完"，不回原文地猜
            stack.pop()

    if in_string or not stack or len(stack) > 3:
        return text  # 截在字符串中间 / 本来就闭合 / 坏得太多
    candidate = body + "".join("}" if c == "{" else "]" for c in reversed(stack))
    try:
        if not isinstance(json.loads(candidate), dict):
            return text
    except (ValueError, TypeError):
        return text
    return candidate


def _parse(text: str) -> tuple[list[RubricScore], bool]:
    """解析裁判输出，返回 `(分数, 是否补过括号)`。缺的维度**不补 3 分**，直接不出现。

    补一个中间分看起来无害，实际会让 `mean` 变成一个
    "裁判答了几个维度"的函数——一份只答了 2 个维度的报告
    会因为那两个都是 5 分而拿到满分。缺就是缺，报告里要看得见。
    """
    import json

    raw = (text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0:
        return [], False
    data: Any = None
    if end > start:
        try:
            data = json.loads(raw[start : end + 1])
        except (ValueError, TypeError):
            data = None
    repaired = False
    if not isinstance(data, dict):
        fixed = _close_unbalanced(raw)
        if fixed != raw:
            try:
                data = json.loads(fixed)
            except (ValueError, TypeError):
                data = None
            repaired = isinstance(data, dict)
    if not isinstance(data, dict):
        return [], False

    out: list[RubricScore] = []
    for key, _desc in DIMENSIONS:
        item = data.get(key)
        if isinstance(item, dict) and isinstance(item.get("score"), (int, float)):
            score = int(item["score"])
            # 夹到 1–5：裁判偶尔会给 0 或 7。不夹的话均值会被一个
            # 越界的分数拖走，而那看起来像是"这份报告真的更好/更差"。
            out.append(
                RubricScore(
                    dimension=key,
                    score=max(1, min(5, score)),
                    reason=str(item.get("reason") or ""),
                )
            )
    return out, repaired
