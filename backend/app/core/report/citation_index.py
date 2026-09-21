"""正文引用编号：一份报告里的 `[1]` 指的是哪条证据。

为什么编号要**存进报告**，而不是渲染时现算
-----------------------------------------
编号原先只在导出时分配（`export._CitationIndex`），它的 docstring 写着
"两处不可能对不上，因为它们是同一个计数器的两次读取"。这句话在
**只有一个渲染器**的时候成立。

报告页是第二个渲染器。它也要把 `[证据: EV-x]` 画成角标，也要让读者
从正文翻到证据表时能对上号。两个渲染器各算一遍，就要保证两次遍历的
顺序、容错（全角冒号、多余空白）、以及"哪些块参与编号"完全一致——
而这种一致没有任何东西会守着：后端给矩阵加一块、页面就整体错一位，
而**错一位的编号看起来完全正常**，读者只会怀疑自己数错了。

这正是 `export.py` 开头那段话要防的事，只是它当初没预料到会有第二个
渲染器。所以编号改成在 `assemble` 时算一次、存进报告正文
（`body["citations"]`），导出与页面都读它。

为什么不连矩阵、定价表那些块一起编号
----------------------------------
只有**正文**参与编号。理由和 `_CitationIndex` 当初选"按出现顺序"而不是
"按证据表顺序"是同一条：读者是从上往下读的，`[1]` 应该是第一个被引用的
证据。表格与附录里的引用不在这条阅读顺序上，给它们编号只会让正文的
编号出现空洞（`[1][2][5]`——`[3][4]` 在表格里），而空洞会被读成漏引。
"""
from __future__ import annotations

import re

#: 正文里的引用标记。写作阶段拼的是 `[证据: EV-xxxxxxxxxxxx]`。
#: 容错到全角冒号与多余空白——这是人写的提示词产出的文本，
#: 而渲染器不该因为一个全角冒号就把角标漏成一片死链接。
MARKER = re.compile(r"\[证据[::]\s*([^\]\s]+)\s*\]")


def build_citations(sections: list[dict], known: set[str]) -> list[dict]:
    """按**正文出现顺序**给被引用的证据编号。

    `known` 是报告自己的证据表。引用了一个不在表里的 id（旧数据、
    手工改过库）时**跳过而不是占号**：页面上那条引用会显示成 `[?]`，
    而 `[?]` 不该把后面的编号整体推后一位——否则"编号对不上"这件事
    会从一处可见的坏（一个 `[?]`）扩散成一片不可见的坏。

    返回 `[{"number": 1, "evidenceId": "EV-..."}, ...]`，按编号升序。
    """
    numbers: dict[str, int] = {}
    for section in sections:
        for evidence_id in MARKER.findall(str(section.get("content") or "")):
            if evidence_id in known and evidence_id not in numbers:
                numbers[evidence_id] = len(numbers) + 1
    return [
        {"number": number, "evidenceId": evidence_id}
        for evidence_id, number in sorted(numbers.items(), key=lambda item: item[1])
    ]


def number_by_id(citations: list[dict] | None) -> dict[str, int]:
    """`citations` 列表 → `evidence_id → 编号`。读的一方统一走它。"""
    out: dict[str, int] = {}
    for item in citations or []:
        evidence_id = str(item.get("evidenceId") or "")
        number = item.get("number")
        if evidence_id and isinstance(number, int):
            out[evidence_id] = number
    return out


__all__ = ["MARKER", "build_citations", "number_by_id"]
