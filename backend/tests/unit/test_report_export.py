"""报告导出：角标必须能在附录里找到。

导出是最后一道引用检查，也是最容易漏掉的一道
------------------------------------------------
界面上的角标点得开，点不开当场就会被发现。导成 Markdown 之后，
角标变成一个纯文本标记，**"点不开"这件事没有任何反馈了**：
读者看到 `[3]` 而附录里只有两条时，他会觉得自己数错了。

所以 `citation_problems()` 是本文件的主角：它把"编号对得上"从一句
设计声明变成一条**能红的检查**。而它自己也要被反证——
`test_角标对不上时检查要报出来` 就是干这个的，否则一个恒返回空列表的
检查函数会让下面每一条断言都通过。
"""
from __future__ import annotations

import json
import re

import pytest

from app.core.report.export import citation_problems, to_json, to_markdown

VAGUE_QUERY = "帮我看看那个笔记软件"


@pytest.fixture
async def report(run_mock_pipeline) -> dict:
    """一份真报告。手搓 dict 的话，字段名写错了测试照样绿——
    而这些字段名正是导出器唯一需要写对的东西。"""
    outcome = await run_mock_pipeline()
    return outcome.body


@pytest.fixture
async def degraded_report(run_mock_pipeline) -> dict:
    """一份**必然带降级**的报告（含糊需求 → 自动采用推荐选项）。"""
    outcome = await run_mock_pipeline(query=VAGUE_QUERY)
    return outcome.body


# ============================================================
# 结构
# ============================================================


def test_先有标题再有正文(report: dict) -> None:
    markdown = to_markdown(report)
    assert markdown.startswith(f"# {report['subject']}")
    assert "## 目录" in markdown
    assert "## 证据附录" in markdown


def test_每一章都出现在导出里(report: dict) -> None:
    markdown = to_markdown(report)
    for section in report["sections"]:
        assert f"## {section['title']}" in markdown, f"章节「{section['title']}」没被导出"


def test_章节正文里的引用标记被换成了编号(report: dict) -> None:
    """写作阶段拼的是 `[证据: EV-x]`。导出后不该再有原始标记——
    留着的话那份 markdown 里全是十六进制 id，没人读得下去。"""
    markdown = to_markdown(report)

    assert "[证据:" not in markdown and "[证据：" not in markdown
    body_of_sections = markdown.split("## 质量门")[0]
    assert re.search(r"\[\d+\]", body_of_sections), "正文里一个角标都没有"


def test_编号按首次出现的顺序排(report: dict) -> None:
    """第一个被读到的证据该是 `[1]`。按证据表顺序编号的话，
    附录开头会是一堆正文里根本没提到的条目。"""
    markdown = to_markdown(report)
    first_used = int(re.search(r"\[(\d+)\]", markdown).group(1))
    assert first_used == 1


def test_矩阵与定价都导出成表格(report: dict) -> None:
    markdown = to_markdown(report)
    assert "## 功能对比矩阵" in markdown
    assert "## 定价" in markdown
    for brand in report["matrix"]["brands"]:
        assert brand in markdown


def test_图表不是论点所以不占正文(report: dict) -> None:
    """图表规格是给 ECharts 用的（`series` / `yAxis`），导进 markdown
    只会得到一坨没有意义的 JSON——它支撑的结论已经在矩阵和正文里了。"""
    markdown = to_markdown(report)
    assert "yAxis" not in markdown
    assert "chartId" not in markdown


# ============================================================
# 角标：本文件的重点
# ============================================================


def test_每个角标都能在附录里找到(report: dict) -> None:
    markdown = to_markdown(report)
    appendix = markdown.split("## 证据附录", 1)[1]

    used = {int(m) for m in re.findall(r"\[(\d+)\]", markdown)}
    defined = {int(m) for m in re.findall(r"^\[(\d+)\]", appendix, flags=re.MULTILINE)}

    assert used, "一个角标都没有，这条用例什么都没测到"
    assert used <= defined, f"这些角标在附录里找不到：{sorted(used - defined)}"
    assert defined <= used, f"附录里这些条目没人引用：{sorted(defined - used)}"


def test_编号从1连续到N(report: dict) -> None:
    """跳号会让读者以为自己漏看了一条。"""
    markdown = to_markdown(report)
    appendix = markdown.split("## 证据附录", 1)[1]
    defined = sorted(int(m) for m in re.findall(r"^\[(\d+)\]", appendix, flags=re.MULTILINE))

    assert defined == list(range(1, len(defined) + 1))


def test_附录条目带网址和来源(report: dict) -> None:
    """点不开的角标在 markdown 里无法被发现，所以**网址必须印出来**——
    它是读者唯一能自己去核对的路径。"""
    markdown = to_markdown(report)
    for evidence in report["evidences"]:
        if evidence["evidenceId"] in markdown:
            continue
        break
    assert "    https://" in markdown


def test_引用了不存在的证据时角标是问号而不是静默跳过() -> None:
    """正文引了一条证据表里没有的 id（手工改过库、或旧版本数据）。

    静默跳过会让这句话少一个角标而不留任何痕迹；
    渲染成 `[?]` 则**看得见**，并且会在末尾单列一段说明。
    """
    body = {
        "subject": "测试",
        "query": "测试",
        "brands": ["A"],
        "generatedAt": "2026-01-01T00:00:00+00:00",
        "sections": [
            {"key": "executive_summary", "title": "执行摘要",
             "content": "结论[证据: EV-真实] 与 [证据: EV-不存在]。"}
        ],
        "evidences": [{"evidenceId": "EV-真实", "title": "真", "url": "https://a.example.com"}],
        "claims": [], "metrics": {}, "quality": {},
    }

    markdown = to_markdown(body)
    # **只在前半段找 `[?]`。** 末尾那段"引用异常"的说明文字里也写着
    # `[?]` 这个词（"已在正文中标记为 `[?]`"），全篇搜索的话，
    # 正文里到底有没有标记就分不出来了——实测：把渲染改成返回空串，
    # 全篇搜索的断言**照样通过**。断言必须落在正文那一段里。
    article = markdown.split("## 引用异常")[0]

    assert "[1]" in article, "真实的那条应该正常编号"
    assert "[?]" in article, "不存在的证据必须看得见"
    assert "EV-不存在" in markdown, "至少要报出是哪个 id，否则无从查起"


def test_角标对不上时检查要报出来() -> None:
    """**反证 `citation_problems` 自己。** 一个恒返回空列表的检查函数
    会让上面每一条断言都通过，而它看起来完全正常。"""
    body = {
        "subject": "测试", "query": "测试", "brands": ["A"],
        "generatedAt": "2026-01-01T00:00:00+00:00",
        "sections": [{"key": "executive_summary", "title": "执行摘要",
                      "content": "结论[证据: EV-有] [证据: EV-没有]。"}],
        "evidences": [{"evidenceId": "EV-有", "title": "有", "url": "https://a.example.com"}],
        "claims": [], "metrics": {}, "quality": {},
    }

    problems = citation_problems(body)

    assert problems, "正文引用了一条不存在的证据（渲染成 `[?]`），检查却说没问题"


def test_干净报告的检查结果为空(report: dict) -> None:
    assert citation_problems(report) == []


# ============================================================
# 降级：必须说，而且要在前面
# ============================================================


def test_没有降级时不出现降级横幅(report: dict) -> None:
    """这条同时也是下面那条的**前提**：默认需求跑出来的报告没有降级，
    所以"降级横幅出现在最前面"必须换一份必然降级的报告去测。"""
    assert report["degraded"] == [], "默认需求本该没有降级，夹具变了"
    assert "降级" not in to_markdown(report).split("## 目录")[0]


def test_降级横幅在目录之前(degraded_report: dict) -> None:
    """**这是导出与界面的关键差异。**

    界面上有降级横幅，导出的文件里没有。如果那几处降级被排在文末，
    等于没披露——而"静默降级正是最危险的那种失败"。
    """
    body = degraded_report
    assert body["degraded"], "这份报告本该有降级，夹具失效了"

    markdown = to_markdown(body)
    banner = markdown.index("处降级")
    toc = markdown.index("## 目录")

    assert banner < toc, "降级横幅排在目录后面，读者会读到正文才发现材料不全"
    for item in body["degraded"]:
        assert item in markdown


def test_降级章节会被标出来() -> None:
    body = {
        "subject": "测试", "query": "测试", "brands": ["A"],
        "generatedAt": "2026-01-01T00:00:00+00:00",
        "sections": [
            {"key": "executive_summary", "title": "执行摘要", "content": "正文。", "degraded": True},
        ],
        "evidences": [], "claims": [], "metrics": {}, "quality": {},
    }

    markdown = to_markdown(body)

    assert "本节内容降级" in markdown
    assert "执行摘要（降级）" in markdown, "目录里也要标，不然翻到才发现"


def test_不能发布时必须说出缺了什么(report: dict) -> None:
    """`passed` 与 `publishable` 是**两件事**：证据够了不代表内容齐了。

    导出里并排给一个"通过"和一个"否"，读者第一反应是这报告自相矛盾。
    列出缺的块之后，它变成一条可执行的信息（"补上舆情标注再发"）。
    """
    quality = report["quality"]
    assert quality["publishable"] is False, "快速模式缺舆情标注，本该不可发布，夹具变了"

    markdown = to_markdown(report)
    missing = report["completeness"]["missing"]
    labels = report["completeness"]["labels"]

    assert missing
    assert "不能发布" in markdown
    for key in missing:
        assert labels.get(key, key) in markdown, f"缺了「{labels.get(key, key)}」却没说"


def test_完整度分数出现在质量门里(report: dict) -> None:
    """0 和"没算"在 markdown 里长得一样，所以分数要有单位、要有出处。"""
    markdown = to_markdown(report)
    score = report["completeness"]["score"]

    assert f"{score:.0%}" in markdown, f"完整度 {score} 没被导出"


def test_返工过的章节会被标出来() -> None:
    body = {
        "subject": "测试", "query": "测试", "brands": ["A"],
        "generatedAt": "2026-01-01T00:00:00+00:00",
        "sections": [
            {"key": "executive_summary", "title": "执行摘要", "content": "正文。", "reworked": True},
        ],
        "evidences": [], "claims": [], "metrics": {}, "quality": {},
    }

    assert "本节在返工后重写过" in to_markdown(body)


# ============================================================
# JSON
# ============================================================


def test_JSON_默认不带正文(report: dict) -> None:
    """216 条证据带上正文是十几 MB，而导出的用途是分享与存档。
    备份是数据库自己的事。"""
    payload = json.loads(to_json(report))

    assert "fullText" not in payload["evidences"][0]
    assert payload["evidences"][0]["url"], "剥掉的是正文，不是整条证据"
    assert len(payload["sections"]) == len(report["sections"])


def test_JSON_可以带全文(report: dict) -> None:
    payload = json.loads(to_json(report, include_full_text=True))

    assert "fullText" in payload["evidences"][0]


def test_JSON_默认不修改原报告(report: dict) -> None:
    """`to_json` 只该返回一份新对象。就地删掉 `fullText` 的话，
    调用方手里那份报告会**静默失去正文**——而它可能正要拿去落库。"""
    before = len(report["evidences"][0])
    to_json(report)

    assert "fullText" in report["evidences"][0]
    assert len(report["evidences"][0]) == before


def test_JSON_是合法且可读的(report: dict) -> None:
    text = to_json(report)
    assert json.loads(text)["subject"] == report["subject"]
    assert "\\u" not in text, "中文被转义了，导出的文件没法直接读"
