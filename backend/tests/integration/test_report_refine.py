"""「深化本节」：批注驱动的二次调研。

这一组守的是**一条链子**，链子上任何一环断开，缺陷都会换个样子出现：

    批注 → 补采 → 重写那一章 → 新证据并进正文 → 指标重算 → 质量门重判

最隐蔽的一环是"并进正文"。新证据只写进 `evidences` 表、不并进
`data["evidences"]` 的话，重写后的章节会引用一个正文里不存在的 id——
**这一环断了不会报任何错**，它只会在导出时变成 `[?]`，
而页面上那一节看起来完全正常。

所以下面有三条断言在不同深度上守同一件事：
`test_深化后导出没有引用异常`（末端）、
`test_深化把补到的证据并进正文`（中途）、
`test_深化后指标跟着走`（面板与正文一致）。

两条容易被写成恒真的断言，这里都提前处理了
------------------------------------------------
- "补到了新证据"：mock 的搜索按 query 文本生成 URL，所以新查询必然
  产出新 id。但如果深化误用了**首轮**预算池，首轮池在首轮结束时已经
  见底，一次搜索都不会发出去 —— 那时 `addedEvidences` 是 0。
  所以断言的是 `> 0`，而且失败信息里点明了这条错路。
- "质量门重判了"：默认跑出来的报告覆盖率已经是 100%，深化前后一样，
  断言会**恒真**。所以 `test_深化后质量门跟着重判` 手工把质量门摆回
  "未通过 + 覆盖率不足"的老状态，造出一次跨门槛的跳变。
"""
from __future__ import annotations

import pytest

from app.core.report.citation_index import MARKER, build_citations
from app.core.report.export import citation_problems
from app.core.report.refine import MAX_DEEPEN_QUERIES
from app.db.repo import reports as reports_repo

REFINE_ANNOTATION = "这一节太笼统了，我想知道具体怎么收费的"
SECTION = "executive_summary"


def _detail(client, report_id: str) -> dict:
    response = client.get(f"/api/reports/{report_id}")
    assert response.status_code == 200
    return response.json()


def _refine(client, report_id: str, **overrides) -> dict:
    body = {"annotation": REFINE_ANNOTATION, "sectionKey": SECTION}
    body.update(overrides)
    response = client.post(f"/api/reports/{report_id}/refine", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ============================================================
# 重写那一章
# ============================================================


def test_深化会重写那一节(client, report_id) -> None:
    original = next(
        s for s in _detail(client, report_id)["data"]["sections"] if s["key"] == SECTION
    )

    result = _refine(client, report_id)

    assert result["sectionKey"] == SECTION
    assert result["section"]["reworked"] is True, "重写过的章节要标出来"
    assert result["section"]["content"] != original["content"], "正文没变，等于没深化"

    stored = next(
        s for s in _detail(client, report_id)["data"]["sections"] if s["key"] == SECTION
    )
    assert stored["reworked"] is True, "重写没落到库里，下次打开就看不见了"
    assert stored["content"] == result["section"]["content"]


def test_深化只动那一节(client, report_id) -> None:
    """深化一章却改了别的章，用户下次翻到那里会以为是模型自己乱动。"""
    before = _detail(client, report_id)["data"]["sections"]

    _refine(client, report_id)

    after = _detail(client, report_id)["data"]["sections"]
    changed = {
        old["key"]
        for old, new in zip(before, after, strict=True)
        if old["content"] != new["content"]
    }
    assert changed == {SECTION}, f"被改动的章节不止一节：{changed}"


def test_章节顺序不变(client, report_id) -> None:
    before = [s["key"] for s in _detail(client, report_id)["data"]["sections"]]

    _refine(client, report_id)

    after = [s["key"] for s in _detail(client, report_id)["data"]["sections"]]
    assert after == before


# ============================================================
# 补采的证据要一路走到底
# ============================================================


def test_深化把补到的证据并进正文(client, report_id) -> None:
    """**本文件的重点。** 补采到的证据必须同时出现在两个地方：

    1. `data["evidences"]`（正文的证据表，导出附录从这里生成）
    2. `evidences` 表（证据库页、图谱从那里读）

    只做 2 的话，重写后的章节引用了一个正文里没有的 id。
    """
    before = len(_detail(client, report_id)["data"]["evidences"])

    result = _refine(client, report_id)

    assert result["addedEvidences"] > 0, (
        "一条新证据都没补到。mock 的搜索按 query 文本生成 URL，新查询必然"
        "产出新 id —— 补不到说明这次深化**根本没发出搜索**，"
        "多半是误用了首轮预算池：首轮池在首轮结束时已经被 planned[:budget] 花到底了。"
    )
    after = _detail(client, report_id)["data"]["evidences"]
    assert len(after) == before + result["addedEvidences"]


def test_补到的证据进了证据库(client, report_id) -> None:
    """正文里有了、证据库里没有的话，报告页点角标能点开，
    但证据库页搜不到那一条——两处对不上，用户会以为是缓存问题。"""
    from app.db.repo import evidences as evidences_repo

    task_id = reports_repo.get(report_id).task_id
    before = {ev.evidence_id for ev in evidences_repo.list_by_task(task_id)}

    _refine(client, report_id)

    after = {ev.evidence_id for ev in evidences_repo.list_by_task(task_id)}
    assert len(after) > len(before), "补采的证据没进证据库"


def test_深化后导出没有引用异常(client, report_id) -> None:
    """链子最末端的守卫：新证据没并进正文的话，
    重写后的章节就会带一个 `[?]`，这条断言会红。"""
    _refine(client, report_id)

    data = _detail(client, report_id)["data"]
    assert citation_problems(data) == [], "深化之后导出出现了对不上的角标"


def test_深化后正文编号跟着重发(client, report_id) -> None:
    """深化重写了一章，正文引用编号必须跟着重发。

    **这条守的是一个没有任何东西会报的缺陷。** `citations` 是存在报告里的
    （`assemble` 算的），导出与报告页都读那一份。深化补进新证据、
    把那一章重写一遍，存下来的那份编号里就**没有新证据**了：
    导出接在种子之后给它发一个新号，报告页读到 `citations` 非空就不再现算，
    同一条引用显示成 `[?]`。两边各自看起来都完全正常。

    `citation_problems` 抓不到它——那条检查比对的是导出自己渲染出来的
    "正文 `[N]`"与"附录 `[N]`"，导出内部自洽。

    这条用例是**先读到这个缺陷、再补的修复**，不是补覆盖率。
    """
    _refine(client, report_id)

    data = _detail(client, report_id)["data"]
    known = {item["evidenceId"] for item in data["evidences"]}
    expected = build_citations(data["sections"], known)

    assert data["citations"] == expected, (
        "存下来的编号与重写后的正文对不上：导出会按自己那份接在种子后头发号，"
        "报告页读存的这份，于是同一条引用在两处是不同的东西"
    )

    # 光比对两份编号还不够：正文如果一条新证据都没引到，两边都是同一份，
    # 上面那条断言恒真。这里把"正文真的引到了东西"钉住。
    cited = {
        evidence_id
        for section in data["sections"]
        for evidence_id in MARKER.findall(str(section.get("content") or ""))
        if evidence_id in known
    }
    assert cited, "夹具变了：深化后的正文一条引用都没有，上面那条断言证明不了什么"
    assert {item["evidenceId"] for item in data["citations"]} == cited, (
        "有条被正文引用的证据没拿到编号——导出会给它发一个号，"
        "报告页却只能显示 [?]"
    )


def test_深化后指标跟着走(client, report_id) -> None:
    """证据数变了而 `metrics.evidences` 没变，就是一份**自己和自己打架**
    的报告：附录里有 240 条，指标面板写着 216 条。"""
    result = _refine(client, report_id)

    data = _detail(client, report_id)["data"]
    assert data["metrics"]["evidences"] == len(data["evidences"])
    assert data["metrics"]["evidences"] == 216 + result["addedEvidences"]


def test_深化后质量门跟着重判(client, report_id) -> None:
    """**这里的"深化前"是手工摆出来的状态。**

    真实场景：一份报告因为覆盖率不够没过门，用户深化了最弱的一章，
    补进来一批证据把覆盖率顶上去。手工把 `quality` 改回
    "未通过 + 覆盖率不足"，就是为了造出这次跨门槛的跳变——
    不造的话覆盖率前后都是 100%，断言会**恒真**。

    不重判的后果是导出里的自相矛盾：并排两行，
    一行"维度覆盖 4/4"，一行"维度覆盖率 25% 低于下限"。
    """
    record = reports_repo.get(report_id)
    record.data["quality"] = {
        **record.data["quality"],
        "passed": False,
        "coverage": 0.25,
        "dimensionsCovered": 1,
        "failedBecause": ["维度覆盖率 25% 低于下限 60%"],
    }
    reports_repo.save(record)

    _refine(client, report_id)

    data = _detail(client, report_id)["data"]
    quality = data["quality"]

    assert quality["coverage"] == data["metrics"]["dimensionCoverage"], (
        "质量门的覆盖率与指标面板的覆盖率是两个数——导出把这两行并排印出来"
    )
    assert quality["passed"] is True, "新证据把覆盖率顶上去之后，质量门该重判"
    assert not any("覆盖率" in reason for reason in quality["failedBecause"]), (
        f"覆盖率已经不低了，`failedBecause` 里还留着旧的那句：{quality['failedBecause']}"
    )


def test_深化不凭空造出没有论点的结论(client, report_id) -> None:
    """质量门有一条判据是"没有产出任何论点"。

    深化时把 `ctx.claims` 留空的话，重判会凭空添上这条 blocker——
    一份有几十条论点的报告会被告知"你没有论点"。
    """
    _refine(client, report_id)

    data = _detail(client, report_id)["data"]
    assert data["claims"], "夹具变了：这份报告本该有论点"
    assert not any("论点" in reason for reason in data["quality"]["failedBecause"])


def test_深化不改论点表(client, report_id) -> None:
    """深化只重写一章正文。论点表动了的话，`无证据立论率`、`交叉验证率`
    这些指标就跟不上，而它们这次**没有**被重算。"""
    before = _detail(client, report_id)["data"]["claims"]

    _refine(client, report_id)

    after = _detail(client, report_id)["data"]["claims"]
    assert [c["text"] for c in after] == [c["text"] for c in before]


# ============================================================
# 留痕
# ============================================================


def test_深化留下记录和一条refine批注(client, report_id) -> None:
    """`reworked=True` 只说改过，不说**为什么**改、谁提的意见。"""
    _refine(client, report_id)

    entries = _detail(client, report_id)["data"]["refinements"]
    assert len(entries) == 1
    assert entries[0]["sectionKey"] == SECTION
    assert entries[0]["annotation"] == REFINE_ANNOTATION
    assert entries[0]["addedEvidences"] > 0
    assert entries[0]["at"], "没有时间戳，就说不清是哪一轮改的"

    kinds = [item["kind"] for item in _detail(client, report_id)["feedback"]]
    assert "refine" in kinds, "深化要留一条 `refine` 批注：批注是要求，深化是执行"


def test_深化两次会留下两条记录(client, report_id) -> None:
    """和批注一样**永远追加**。覆盖的话，"这一节改了几轮"就查不出来了。"""
    for index in range(2):
        _refine(client, report_id, annotation=f"第 {index + 1} 次深化")

    entries = _detail(client, report_id)["data"]["refinements"]
    assert len(entries) == 2
    assert [item["annotation"] for item in entries] == ["第 1 次深化", "第 2 次深化"]


def test_深化不污染任务的决策回放(client, report_id) -> None:
    """深化的事件不该落进 `task_events`。

    落了的话，决策回放的滑杆会多走一段谁也解释不了的尾巴——
    那一轮不在流水线的 DAG 上，没有 stage 对应它。
    """
    from app.db.repo import events as events_repo

    task_id = reports_repo.get(report_id).task_id
    before = len(events_repo.list_since(task_id, 0))

    _refine(client, report_id)

    assert len(events_repo.list_since(task_id, 0)) == before


# ============================================================
# 一次深化花多少钱
# ============================================================


def test_一次深化最多发几条查询(client, report_id) -> None:
    """**这是一条成本守卫，不只是"够不够用"的守卫。**

    `MAX_DEEPEN_QUERIES = 2` 是刻意的：批注是自然语言，不是审计给的靶子，
    多发的每一条查询都是拿用户的钱去赌他批注里的词恰好是好查询。

    它同时钉住了**走返工那条路**这件事：`build_queries(rework=False)`
    会把维度轮、角度轮、平台轮全排出来（quick 档上限 24 条），
    而 `rework=True` 只跑批注生成的那几条目标。误用首轮计划的话，
    这一条会直接红——而 `addedEvidences` 那几条断言**看不出来**，
    因为条数只会变多，看起来"补得更多了"。
    """
    from app.providers.registry import get_search

    search = get_search()
    before = len(search.calls)

    result = _refine(client, report_id)

    issued = len(search.calls) - before
    assert result["addedEvidences"] > 0, "先确认这次深化真的搜了，否则下面的上限恒成立"
    assert issued <= MAX_DEEPEN_QUERIES, (
        f"一次深化发了 {issued} 条查询（上限 {MAX_DEEPEN_QUERIES}）。"
        f"多半是走成了首轮查询计划——那会把整个维度轮和角度轮重跑一遍。"
    )


# ============================================================
# 不搜索那条路
# ============================================================


def test_不搜索时只重写(client, report_id) -> None:
    """`search=false`：我只要换个说法，别再去搜。

    这条和 `test_深化把补到的证据并进正文` 是**一对**。
    只有后者的话，`search=false` 这条路根本没人走过；
    只有前者的话，"一次搜索都没发"和"补采坏了"分不开。
    """
    before = len(_detail(client, report_id)["data"]["evidences"])

    result = _refine(client, report_id, search=False)

    assert result["addedEvidences"] == 0
    assert result["section"]["reworked"] is True, "不搜索也要重写"
    assert len(_detail(client, report_id)["data"]["evidences"]) == before


def test_不搜索时不留降级(client, report_id) -> None:
    """没发搜索，就不该报"搜索预算用尽"。那种降级是**给采集用的**，
    出现在一次"我明确说了别搜"的请求上，读者会以为系统出了问题。"""
    result = _refine(client, report_id, search=False)
    assert result["degraded"] == []


# ============================================================
# 错误路径
# ============================================================


def test_深化不存在的章节是404(client, report_id) -> None:
    response = client.post(
        f"/api/reports/{report_id}/refine",
        json={"annotation": "x", "sectionKey": "no_such_section"},
    )
    assert response.status_code == 404


def test_章节留空时深化第一章(client, report_id) -> None:
    """批注挂在**整份报告**上（`sectionKey` 为空）时，"深化"落到第一章。
    报 404 让用户去猜填什么，是在为难人。"""
    sections = _detail(client, report_id)["data"]["sections"]

    result = _refine(client, report_id, sectionKey="")

    assert result["sectionKey"] == sections[0]["key"]


def test_深化空批注是422(client, report_id) -> None:
    response = client.post(
        f"/api/reports/{report_id}/refine",
        json={"annotation": "", "sectionKey": SECTION},
    )
    assert response.status_code == 422


def test_深化不存在的报告是404(client) -> None:
    response = client.post("/api/reports/RP-nope/refine", json={"annotation": "x"})
    assert response.status_code == 404


def test_深化失败时不写坏报告(client, report_id, monkeypatch) -> None:
    """写作阶段抛异常时，报告必须**原样不动**。

    半写回的状态是最糟的：章节换了新的、指标还是旧的，
    而报告看起来是完整的一份。
    """
    from app.core.pipeline import write

    before = _detail(client, report_id)["data"]

    def boom(*args, **kwargs):
        raise RuntimeError("模型炸了")

    monkeypatch.setattr(write, "refine_section", boom)

    with pytest.raises(RuntimeError):
        _refine(client, report_id, search=False)

    after = _detail(client, report_id)["data"]
    assert [s["content"] for s in after["sections"]] == [
        s["content"] for s in before["sections"]
    ], "深化失败了，正文却被改了"
    assert after["metrics"] == before["metrics"]
    assert "refinements" not in after, "失败的那次不该留下记录"
