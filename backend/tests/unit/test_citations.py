"""引用解析测试。

这个模块守着一个**已经真实发生过**的故障：`resolve_citations` 曾经把 id
整体转成小写去比对，而系统里的 id 是 `EV-`（大写前缀）+ 小写十六进制。
于是每一次看起来合法的引用都被判成幻觉引用、从正文里删掉，报告里所有章节
的引用数变成 0，而「幻觉引用率」显示 0%——因为被删掉的角标不计入分母。
两个指标同时看起来很正常，报告却是错的。所以这里的用例按"能不能发现
那次故障"来写。
"""
from __future__ import annotations

import pytest

from app.core.pipeline.write import resolve_citations

CAT = "EV-a1b2c3d4e5f6"
DOG = "EV-0f1e2d3c4b5a"
KNOWN = {CAT, DOG}


def test_valid_citation_survives_and_is_collected() -> None:
    text = f"该产品的定价明显高于同类 [证据: {CAT}]，但功能覆盖更全。"
    cleaned, valid, phantom = resolve_citations(text, KNOWN)

    assert cleaned == text
    assert valid == [CAT]
    assert phantom == []


def test_lowercase_id_resolves_to_the_canonical_id() -> None:
    """回归用例：模型把小写形式写回来时，必须取回系统里那个大写 id。

    断言的是"返回的 id 与系统里的完全相同"，而不是"没被删掉"——
    整体转小写那种写法也能通过后者，但它会让 `section.evidence_ids`
    与 `Evidence.evidence_id` 对不上，前端点开角标是空的。
    """
    cleaned, valid, phantom = resolve_citations(f"见 [{CAT.lower()}]", KNOWN)

    assert valid == [CAT]
    assert phantom == []


def test_uppercase_id_resolves_too() -> None:
    """大小写两个方向都要通——折叠表是双向匹配，不是"只容忍小写"。"""
    _, valid, phantom = resolve_citations(f"见 [{CAT.upper()}]", KNOWN)

    assert valid == [CAT]
    assert phantom == []


def test_phantom_citation_is_deleted_from_the_body() -> None:
    """不存在的角标要**从正文里删掉**，不是留着。

    留着的角标指向空处，比"这里没有引用"更糟：它看起来是有据可查的。
    """
    text = "结论是 A 更便宜 [证据: EV-ffffffffffff]，B 更全。"
    cleaned, valid, phantom = resolve_citations(text, KNOWN)

    assert "EV-ffffffffffff" not in cleaned
    assert valid == []
    assert phantom == ["EV-ffffffffffff"]


def test_phantom_counts_only_unknown_ids() -> None:
    """合法引用不能被算进幻觉数——否则幻觉率会随引用增多而上升。"""
    text = f"甲 [证据: {CAT}] 乙 [证据: EV-999999999999] 丙 [证据: {DOG}]"
    _, valid, phantom = resolve_citations(text, KNOWN)

    assert valid == [CAT, DOG]
    assert phantom == ["EV-999999999999"]


def test_repeated_citation_counts_once_per_section() -> None:
    """同一份证据在一章里引用三次只算一次。

    否则"这一章引用了多少证据"这个数会随文风波动——话多的章节看起来
    依据更足，而那只是重复了同一份材料。`evidence_ids` 因此是去重后的。
    """
    text = f"甲 [{CAT}]，乙 [{CAT}]，丙 [{CAT}]"
    _, valid, phantom = resolve_citations(text, KNOWN)

    assert valid == [CAT]
    assert phantom == []


@pytest.mark.parametrize(
    "written",
    [
        f"[证据: {CAT}]",       # 提示词给的规范写法
        f"[证据:{CAT}]",        # 冒号后没空格
        f"[证据：{CAT}]",        # 全角冒号
        f"[evidence: {CAT}]",   # 模型写成英文
        f"[{CAT}]",             # 没有前缀
        f"证据: {CAT}",          # 没有方括号
        f"[ {CAT} ]",           # 方括号里有空格
    ],
)
def test_recognized_citation_forms(written: str) -> None:
    """容忍这些写法是刻意的：提示词只规定了其中一种，模型不会严格照做。

    判据是"id 是否对得上"，而不是"格式是否规范"——按格式判会把合法引用
    误判成幻觉，那正是这个模块要防的事。
    """
    _, valid, phantom = resolve_citations(f"正文 {written} 结束", KNOWN)

    assert valid == [CAT]
    assert phantom == []


def test_short_id_is_left_alone() -> None:
    """太短的 `EV-xxx` 不被当作引用角标，**留在正文里**。

    这是一个已知的取舍，写下来免得日后被当成 bug："删除幻觉角标"的能力
    依赖正则认出它。把下限放宽到 2–3 位，`EV-2024` 这类正常文字也会被
    删掉，而误删正文比漏掉一个残缺角标严重得多。代价是被截断的角标
    （`EV-abc`）既不算合法引用、也不进幻觉计数——它会在正文里显形，
    读者看得见，只是指标看不到。修法留待真出现时再放宽。
    """
    text = "这里有个残缺的角标 [证据: EV-abc] 没被识别。"
    cleaned, valid, phantom = resolve_citations(text, KNOWN)

    assert cleaned == text
    assert valid == []
    assert phantom == []


def test_text_without_citations_is_returned_unchanged() -> None:
    text = "这一章没有引用任何证据。"
    cleaned, valid, phantom = resolve_citations(text, KNOWN)

    assert cleaned == text
    assert (valid, phantom) == ([], [])


def test_empty_text_is_safe() -> None:
    """空正文不能抛异常：模型偶尔返回空串是正常路径，不是错误。"""
    assert resolve_citations("", KNOWN) == ("", [], [])


def test_blank_lines_left_by_deletion_are_collapsed() -> None:
    """删掉角标后留下的多空格与空行要收掉，否则导出 Markdown 里
    会看到莫名其妙的空行，像是内容缺失。"""
    text = f"第一句 [{CAT}]\n\n\n\n第二句 [EV-ffffffffffff]\n"
    cleaned, _, _ = resolve_citations(text, KNOWN)

    assert "\n\n\n" not in cleaned
    assert "  " not in cleaned
    assert cleaned == f"第一句 [{CAT}]\n\n第二句"
