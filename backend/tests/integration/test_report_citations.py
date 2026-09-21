"""正文引用编号：一份报告里的 `[1]` 现在就只有一个答案。

这个文件守的是一条**刚刚才成立**的性质：编号存在报告里
（`body["citations"]`），而不是留给每个渲染器现算。

为什么值得一个文件
----------------
报告页出现之前，导出是唯一的渲染器，`export._CitationIndex` 那段
"两处不可能对不上，因为它们是同一个计数器的两次读取"是对的。
第二个渲染器一来，这句话就变成了一句**没有机制支撑的声明**：
两个渲染器各算一遍编号，要保证遍历顺序、容错、参与编号的块完全
一致，而没有任何东西会守着它——后端给矩阵加一块，页面就整体错一位，
而错一位的编号看起来完全正常。

所以这里的主断言是**等价性**：把编号种子去掉之后重新渲染，
必须与带着种子的渲染结果**逐字节相同**。它同时证明两件事——
编号存下来没有改变导出的输出（不是一次行为变更），
以及页面拿到的编号就是导出里印出来的那些（两个渲染器不会分家）。

等价性断言有个已知的假阳性风险：`to_markdown` 里如果**根本没读**
`citations` 这个键，比较也会通过。所以下面另有一条
`test_种子被真的读进去了`，它给的种子是**故意错位的**——
现算会得到与它不同的编号。两条合起来，才排除了"比较通过是因为
两边都没用它"。（写这个文件时先写的是等价性，跑绿之后才意识到
它单独并不足以证明种子被读了。）
"""
from __future__ import annotations

import copy
import json
import re

import pytest

from app.core.report.citation_index import build_citations, number_by_id
from app.core.report.export import citation_problems, to_markdown


@pytest.fixture
async def report(run_mock_pipeline) -> dict:
    """一份真报告。手搓 dict 的话，证据表的键名写错了测试照样绿——
    而键名正是编号函数唯一需要写对的东西。"""
    outcome = await run_mock_pipeline()
    return outcome.body


def _section_content(section: dict) -> str:
    return str(section.get("content") or "")


def _prose_order(body: dict) -> list[str]:
    """正文里被引用的证据 id，按首次出现顺序。**刻意独立实现一遍**，
    不复用被测的 `build_citations`——复用它的话，顺序写错了也测不出来。"""
    from app.core.report.citation_index import MARKER

    known = {str(e["evidenceId"]) for e in body["evidences"]}
    seen: list[str] = []
    for section in body["sections"]:
        for evidence_id in MARKER.findall(_section_content(section)):
            if evidence_id in known and evidence_id not in seen:
                seen.append(evidence_id)
    return seen


def _mini_body() -> dict:
    """一份**够小、但每个性质都看得见**的报告。

    为什么要有它：Mock 流水线产出的那份报告里，**每一章引用的都是同样
    那四条证据、而且顺序相同**（实测：4 章 × 4 条，`fwd == reversed`）。
    于是"编号按章节顺序分配"这条规则在它身上**观察不到**——
    把遍历改成倒序，所有断言照样绿。

    这不是假设，是第一次跑突变校验时被抓到的：那条"编号改按正文倒序
    分配"的突变留在绿区，追下去才发现是数据的性质，不是代码的性质。
    所以顺序这条规则改用一个**亲手构造、顺序可观察**的样本去钉：

      - 第 1 章先引乙、再引甲
      - 第 2 章引丙
      - 矩阵里引了丁——**丁只在矩阵里出现**，用来观察"附录接着种子编"

    正文顺序 = 乙、甲、丙；章节倒过来的话是 丙、乙、甲。
    两个映射不同，所以倒序的突变会红。
    """
    return {
        "subject": "甲与乙",
        "brands": ["甲", "乙"],
        "evidences": [
            {"evidenceId": "EV-a", "url": "https://a.example/1", "title": "甲的来源"},
            {"evidenceId": "EV-b", "url": "https://b.example/1", "title": "乙的来源"},
            {"evidenceId": "EV-c", "url": "https://c.example/1", "title": "丙的来源"},
            {"evidenceId": "EV-d", "url": "https://d.example/1", "title": "丁的来源"},
        ],
        "sections": [
            {
                "key": "executive_summary",
                "title": "执行摘要",
                "content": "先说乙[证据: EV-b]，再说甲[证据: EV-a]。",
            },
            {"key": "conclusion", "title": "结论", "content": "只看丙[证据: EV-c]。"},
        ],
        "matrix": {
            "dimensions": ["规模"],
            "brands": ["甲"],
            "scores": [[3.0]],
            "evidenceIds": ["EV-a", "EV-d"],
        },
    }


# ============================================================
# 报告里确实存了编号
# ============================================================


def test_报告里存了正文引用编号(report: dict) -> None:
    citations = report.get("citations")
    assert isinstance(citations, list), "报告正文里没有 citations，编号又被留给渲染器现算了"
    assert citations, "一份有引用的报告，citations 不该是空的"


def test_编号从1开始且连续(report: dict) -> None:
    numbers = sorted(number_by_id(report["citations"]).values())
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"编号有空洞或重复：{numbers}。空洞会让读者以为自己漏看了一段。"
    )


def test_真报告的编号与正文顺序一致(report: dict) -> None:
    """在真报告上核对一遍存下来的编号。

    它守的是"存进报告的那份与正文顺序一致"，**不是**"编号是按章节顺序
    分配的"——Mock 报告每章引用同样四条证据，正序倒序得到的映射相同
    （见 `_mini_body` 的 docstring），所以那条规则钉不住，得下面那条。
    """
    assert number_by_id(report["citations"]) == {
        evidence_id: index for index, evidence_id in enumerate(_prose_order(report), start=1)
    }


def test_编号按正文出现顺序分配_而不是证据表顺序() -> None:
    """**这是钉住"顺序"那一条的用例。**

    用 `_mini_body`：正文顺序是乙、甲、丙，而证据表顺序是甲、乙、丙、丁。
    按证据表编号会得到 甲=1；按正文编号得到 乙=1、甲=2。两者不同，
    所以实现的遍历一改（比如改成倒着扫章节），这条就会红。
    """
    body = _mini_body()
    known = {str(e["evidenceId"]) for e in body["evidences"]}
    assert number_by_id(build_citations(body["sections"], known)) == {
        "EV-b": 1,
        "EV-a": 2,
        "EV-c": 3,
    }


def test_编号表里没有不存在的证据(report: dict) -> None:
    known = {str(e["evidenceId"]) for e in report["evidences"]}
    strays = sorted(set(number_by_id(report["citations"])) - known)
    assert not strays, f"编号表里有报告证据表里没有的 id：{strays}"


# ============================================================
# 等价性：存下编号没有改变导出的输出
# ============================================================


def test_去掉编号种子后导出的结果逐字节相同(report: dict) -> None:
    """这条是"这次改动不是一次行为变更"的证明。

    如果它红了，说明导出的角标编号变了——对已经导出发出去的报告来说，
    那意味着同一份报告在新版代码下会印出不同的编号。
    """
    without = copy.deepcopy(report)
    without.pop("citations", None)
    assert to_markdown(without) == to_markdown(report)


def test_去掉编号种子后角标仍然对得上(report: dict) -> None:
    """旧报告（库里那 12 份，都没有这个键）走的正是这条路。"""
    without = copy.deepcopy(report)
    without.pop("citations", None)
    assert citation_problems(without) == []


def test_种子被真的读进去了(report: dict) -> None:
    """**上面那条等价性单独是证不出"种子被读了"的。**

    `to_markdown` 里如果压根没读 `citations`，两边现算，比较照样通过。
    所以这里给一份**故意错位**的种子：把编号整体加 100。
    现算的话第一个角标是 `[1]`；种子被读了的话是 `[101]`。
    """
    shifted = copy.deepcopy(report)
    shifted["citations"] = [
        {"number": int(item["number"]) + 100, "evidenceId": item["evidenceId"]}
        for item in report["citations"]
    ]
    markdown = to_markdown(shifted)
    first = number_by_id(shifted["citations"])[_prose_order(report)[0]]
    assert f"[{first}]" in markdown
    assert "[1]" not in markdown

    # 这里**刻意不去问 `citation_problems`**：它有一条"附录编号必须
    # 从 1 开始且连续"的不变量，而这份故意错位的种子按定义就违反它
    # （实测会报「附录编号不连续：[101, 102, …]」）。那条不变量由
    # `test_编号从1开始且连续` 守着。在这里顺手断言它，等于把
    # "种子被读了"和"种子是合规矩的"搅成一个问题，而前者才是这条
    # 用例要问的。


def test_附录接着种子往后编号_不从头重来() -> None:
    """种子里有编号 101–103 时，附录里**新出现**的那条证据该拿 104。

    这条是"附录的起始号从哪算"唯一的观察窗口，同样得用 `_mini_body`：
    在 Mock 报告上，矩阵引的证据全都在正文里出现过，附录因此一条新号都不发，
    于是"从最大号起算"和"从种子长度起算"这两种实现给出**完全相同**的输出。
    第一次跑突变校验时那条突变就留在绿区，这是补上它的原因。

    丁（`EV-d`）只在矩阵里被引用，所以它必然在附录里首次拿到编号。
    """
    body = _mini_body()
    body["citations"] = [
        {"number": 101, "evidenceId": "EV-b"},
        {"number": 102, "evidenceId": "EV-a"},
        {"number": 103, "evidenceId": "EV-c"},
    ]
    markdown = to_markdown(body)

    # 正文三个角标整体位移
    assert "[101]" in markdown and "[103]" in markdown
    # **丁接在 103 之后**，而不是从 3 开始（种子有 3 条）
    assert "[104]" in markdown, "附录的编号没有接着种子往后排"
    assert "[3]" not in markdown
    used = {int(m) for m in re.findall(r"\[(\d+)\]", markdown)}
    assert min(used) == 101, f"最小的角标是 {min(used)}，附录从头编号了"



# ============================================================
# 异常数据
# ============================================================


def test_引用了不存在的证据时不占号(report: dict) -> None:
    """未知 id 渲染成 `[?]`。它**不该占一个号**——

    占了的话，后面的编号会整体推后一位，于是"一处看得见的坏"
    （一个 `[?]`）扩散成"一片看不见的坏"（所有编号都位移了）。
    """
    known = {str(e["evidenceId"]) for e in report["evidences"]}
    sections = [
        {"content": "先说一句[证据: EV-不存在]。再说一句[证据: " + sorted(known)[0] + "]。"}
    ]
    citations = build_citations(sections, known)
    assert citations == [{"number": 1, "evidenceId": sorted(known)[0]}]


def test_编号表能往返_json(report: dict) -> None:
    """报告正文要落库成一列 JSON。编号表里混进不可序列化的东西
    （比如 `dict_keys`）时，**落库那一刻**才会炸——而那已经是
    流水线跑完十几分钟之后了。"""
    assert json.loads(json.dumps(report["citations"])) == report["citations"]
