"""判官少吐一个右括号，5 份报告被记成「可用性 0.00/5」。

现象
----
`eval/run_eval.py --live --judge` 第一批 15 份报告，5 份的可用性是
`0.00/5`，而它们恰好是正文最长、最有的可说的那几篇
（一个查询的 3 次 repeat 全军覆没）。`--judge` 不参与闸门，
所以闸门 12 项全绿，这个数只是安静地进了汇总表。

第一次猜错了
------------
当时的推理是"800 的 `max_tokens` 截断了输出"——很合理，失败的
恰好是最长的那几篇，而 5 个维度各要一句 90–150 字的 reason，
5 × 150 个汉字加 JSON 骨架确实贴着 800。于是把上限提到 2000，
**重跑，5 份照样失败**。

真正的原因在原文里，只是当时没看：
`finish_reason=stop`——模型认为**自己答完了**，不是被砍断的。
`len=874`，`{` 六个、`}` 五个：五个维度对象都闭合了，
**根对象的右括号没吐出来**。一次采样少一个字符。

一个字符怎么就变成 0 分的
------------------------
`_parse` 用 `raw.rfind("}")` 找结尾——它找的是**最后一个**右括号，
而那个右括号属于 `honesty`，不属于根对象。于是切出来的片段
少一层收口，`json.loads` 抛错，返回空列表；
空列表经 `RubricResult.mean` 变成 `0.0`，而那和"真的打了最低分"
在数字上完全一样。

下面这份 `_CAPTURED` 是**逐字**捕获的原文（含模型原样写出的
中文引号与省略号）。测试不再去问"补括号的函数写得对不对"，
而是问"这条真实输入现在读不读得出分数"。
"""
from __future__ import annotations

import json

import pytest
from eval.judges.rubric import _close_unbalanced, _parse

#: 2026-09 的 `--live --judge` 里，一份报告的真实判官输出：
#: `finish_reason=stop`、`len=874`、`{` × 6 / `}` × 5，根对象缺右括号。
_CAPTURED = r"""{"structure": {"score": 4, "reason": "执行摘要、功能对比、定价分析、结论与建议四个章节齐全，且结论章节给出了明确分野判断，但缺少独立的'数据主权与本地化能力'章节，该维度仅在 dimensions 中声明却未单独成节。"}, "argument": {"score": 4, "reason": "主要论点均带 [证据: EV-xxx] 标注，如'万物皆 Block'引 EV-e0103a651dca、Obsidian 双链引 EV-e8abe5f39c13，且 claims 中标注了 crossValidated 与 independentDomains；但部分关键论点如 Obsidian 估值 3.5 亿美元仅单一来源（CL-f871c03a82，independentDomains=1），论证强度有限。"}, "actionable": {"score": 2, "reason": "结论与建议章节只复述了定位、架构、组织差异，未给出任何'选 Notion 还是 Obsidian'的决策建议或适用场景推荐，读者读完无法做出具体决定。"}, "differentiation": {"score": 4, "reason": "matrix 中 Notion 与 Obsidian 在'数据主权与本地化能力'上给出 2 vs 5、'核心功能与技术架构'5 vs 4 的差异化打分，功能对比也明确点出'块与数据库'vs'纯文本与双向链接'两条路线；但'定价与商业模式'两品牌均打 2 分，未分出高下。"}, "honesty": {"score": 5, "reason": "定价分析中明确写'Obsidian 的收费方式、免费与付费功能划分……当前公开材料均未覆盖'，并声明'上述内容属于公司规模与资本结构的事实陈述，并不等同于定价信息'，featureTrees 中 Obsidian 协作/AI 能力标注 support:none 并注明'未获取到相关证据'，降级与不确定处说明充分。"}"""

_EXPECTED = {
    "structure": 4,
    "argument": 4,
    "actionable": 2,
    "differentiation": 4,
    "honesty": 5,
}


def test_少了根右括号照样读得出五个维度() -> None:
    """这是这次修复的全部目的：真实输入不再变成 0 分。"""
    scores, repaired = _parse(_CAPTURED)
    assert {s.dimension: s.score for s in scores} == _EXPECTED
    assert repaired is True


def test_补的只有一个右括号没有别的动作() -> None:
    """「只补不猜」要能被验证：补完的文本 = 原文 + 一个 `}`，一字未改。"""
    assert _close_unbalanced(_CAPTURED) == _CAPTURED + "}"


def test_旧写法在这条输入上就是失败的() -> None:
    """自我证伪：证明这份 fixture 真的能掀翻修复前的代码。

    修复前走的是这三行（`raw.find("{")` / `raw.rfind("}")` / `json.loads`）。
    它必须抛错——否则这份 fixture 量的不是它声称的那个缺陷。
    """
    raw = _CAPTURED.strip()
    start, end = raw.find("{"), raw.rfind("}")
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw[start : end + 1])


def test_每个维度的理由都还在() -> None:
    """补括号如果顺手改写了正文，理由会缺——所以理由也要断言。"""
    scores, _ = _parse(_CAPTURED)
    assert all(s.reason.strip() for s in scores)
    assert "降级与不确定处说明充分" in scores[-1].reason


def test_截在字符串中间不补() -> None:
    """被 max_tokens 砍断是另一种伤，补右括号救不回，也不该假装救得回。"""
    truncated = '{"structure": {"score": 4, "reason": "执行摘要、功能对比、定价'
    assert _close_unbalanced(truncated) == truncated
    assert _parse(truncated) == ([], False)


def test_括号对不上就不猜() -> None:
    """不是"没写完"而是"写坏了"。这时候补出来的是另一份东西。"""
    broken = '{"structure": {"score": 4, "reason": "x"]}'
    assert _close_unbalanced(broken) == broken


def test_缺得太深不补() -> None:
    """缺 4 层以上说明坏的不止是收尾，补好再读更可能读出一份看着没问题的错分数。"""
    deep = '{"a": {"b": {"c": {"d": 1'
    assert _close_unbalanced(deep) == deep
    assert _parse(deep) == ([], False)


def test_本来就闭合的输出原样通过() -> None:
    good = json.dumps(
        {k: {"score": 3, "reason": "还行"} for k in _EXPECTED}, ensure_ascii=False
    )
    scores, repaired = _parse(good)
    assert len(scores) == 5
    assert repaired is False


def test_理由里带花括号不会被打乱() -> None:
    """扫描器要跳过字符串里的花括号，否则 reason 里写个 `{a}` 就会算错层数。"""
    payload = '{"structure": {"score": 4, "reason": "矩阵用 | a } b | c { d 表示"}'
    assert _close_unbalanced(payload) == payload + "}"
    scores, repaired = _parse(payload)
    assert repaired is True
    assert scores[0].reason == "矩阵用 | a } b | c { d 表示"


def test_报告里标出这个分数是补过括号的() -> None:
    """同一个 4 分，"补了一个括号才读出来"和"模型本来就这么答的"不是一回事。"""
    from eval.judges.rubric import RubricResult, RubricScore

    repaired = RubricResult(report="r", scores=[RubricScore("honesty", 5)], repaired=True)
    intact = RubricResult(report="r", scores=[RubricScore("honesty", 5)])
    assert repaired.as_dict()["repairedBraces"] is True
    assert "repairedBraces" not in intact.as_dict()


def test_解析失败时原文里带着括号计数() -> None:
    """失败也留下判据：`{`/`}` 的计数一眼看出是不是"少收尾"这种伤。"""
    from eval.judges import rubric

    res = rubric.judge_report({"sections": []}, name="r", llm=_Fake([_CUT_MID_STRING]))
    assert res.scores == []
    assert "finish_reason=length" in res.raw
    assert "braces=2/0" in res.raw


#: 停在字符串中间：连"收尾"都不算，是模型没答完就停了。
_CUT_MID_STRING = '{"structure": {"score": 4, "reason": "砍在这里'

_GOOD = (
    '{"structure": {"score": 4, "reason": "齐"},'
    ' "argument": {"score": 4, "reason": "实"},'
    ' "actionable": {"score": 2, "reason": "弱"},'
    ' "differentiation": {"score": 4, "reason": "分"},'
    ' "honesty": {"score": 5, "reason": "诚"}}'
)


class _Fake:
    """按顺序吐准备好的回答，并记下被问了几次。"""

    def __init__(self, texts: list[str]) -> None:
        self._texts = texts
        self.calls = 0

    def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003, ANN201
        from app.providers.base import LLMResponse, TokenUsage

        text = self._texts[min(self.calls, len(self._texts) - 1)]
        self.calls += 1
        return LLMResponse(
            text=text,
            model="m",
            provider="p",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1,
            finish_reason="length" if text is _CUT_MID_STRING else "stop",
        )


def test_第一次没答完会再问一次() -> None:
    """单次采样不可靠，而重试的代价（一次判官调用）远低于一个假的 0 分。"""
    from eval.judges import rubric

    fake = _Fake([_CUT_MID_STRING, _GOOD])
    res = rubric.judge_report({"sections": []}, name="r", llm=fake)
    assert fake.calls == 2
    assert res.attempts == 2
    assert len(res.scores) == 5
    assert res.as_dict()["attempts"] == 2


def test_补括号救回来的就不重试() -> None:
    """能读出来就不该多花一次钱——补括号和重试是两条不同的路。"""
    from eval.judges import rubric

    fake = _Fake([_CAPTURED])
    res = rubric.judge_report({"sections": []}, name="r", llm=fake)
    assert fake.calls == 1
    assert res.repaired is True
    assert res.attempts == 1


def test_两次都读不出来才算失败并且两次原文都留着() -> None:
    """重试是提高成功率，不是消灭失败率。失败率本身要能被算出来。"""
    from eval.judges import rubric

    fake = _Fake([_CUT_MID_STRING, _CAPTURED[:40]])
    res = rubric.judge_report({"sections": []}, name="r", llm=fake)
    assert fake.calls == 2
    assert res.scores == []
    assert res.attempts == 2
    assert res.raw.count("finish_reason=") == 2
