"""报告导出：Markdown 给人看，JSON 给机器读。

导出是**最后一道**引用检查，而且是最容易漏掉的一道
------------------------------------------------
正文里的角标在界面上是能点开的，点不开会当场被发现；一旦导成 Markdown，
角标变成一个纯文本标记，**点不开这件事就没有任何反馈了**。
读者看到 `[3]` 而附录里只有两条时，他不会觉得"这个系统有问题"，
他会觉得自己数错了。

所以：编号在渲染正文的过程中**就地分配**（`_CitationIndex`），附录按同一份
编号渲染。两处不可能对不上，因为它们是同一个计数器的两次读取。
万一正文引用了证据表里没有的 id（手工改过库、或旧版本数据），
渲染成 `[?]` 而不是静默跳过——**看得见的坏比看不见的坏好**。

降级横幅放在最前面，不是放在附录
--------------------------------
界面上有降级横幅，导出的文件里没有。读者拿到的是一份 markdown，
如果那 3 处降级被排在文末的"附录 D"，等于没披露：**静默降级正是最危险的
那种失败**（`PipelineContext.degrade` 的 docstring）。所以它紧跟标题，
在目录之前。

编号的来源变了：现在读报告里存的那份
----------------------------------
编号原先由本模块的 `_CitationIndex` 现算，那时它是对的——导出是唯一的
渲染器，下面那段"两处不可能对不上"说的就是它自己内部的两次读取。
报告页出现之后就多了一个渲染器，而两个渲染器各算一遍编号，意味着
"顺序、容错、参与编号的块"必须永远一致，且没有任何东西守着它。
所以编号改为在 `assemble` 时算好、存进 `body["citations"]`
（见 `citation_index.py`），本模块改成**读它**。

本模块的遍历顺序没变（正文在前、附录在后），所以对那些**没有**
`citations` 键的旧报告，现算的结果与从前逐字节一致；对新报告，种子
来自正文、现算的部分只覆盖表格与附录，编号接在正文之后——与从前也是
同一个数。有测试守着这条等价性（`tests/integration/test_report_citations.py`）。

`fullText` 默认不进 JSON
-----------------------
一条证据的正文可以有几万字符，216 条证据的报告带上它就十几 MB——
而导出的用途是**分享与存档**，不是备份（备份是数据库自己的事）。
所以默认剥掉，留一个开关给真正需要全量的场景。
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.core.report.citation_index import MARKER as _MARKER
from app.core.report.citation_index import number_by_id


class _CitationIndex:
    """正文里出现过的证据 → 编号。**按首次出现的顺序**分配。

    按出现顺序而不是按证据表的顺序，是因为读者是从上往下读的：
    第一个被引用的证据应该是 `[1]`。按证据表编号的话，
    附录开头会是一堆正文里根本没提到的条目，读者无从核对。

    `seeded` 是 `assemble` 存进报告的那份正文编号（`body["citations"]`）。
    有了它，正文里的引用拿到的编号与报告页上显示的**是同一个数**；
    表格、附录这些不参与正文编号的地方，继续往后接。
    旧报告没有这个键，`seeded` 为空，退回现算——结果与从前一致。
    """

    def __init__(self, known: set[str], seeded: dict[str, int] | None = None) -> None:
        self._known = known
        # 只收下"既在证据表里、又真的有编号"的那些：种子里出现一个
        # 未知 id 的话，`number()` 会把它当已知发一个号出去，
        # 而附录里就会多出一条打不开的引用。
        self._numbers: dict[str, int] = {
            evidence_id: number
            for evidence_id, number in (seeded or {}).items()
            if evidence_id in known
        }
        # 下一个新号要接在种子之后，不能从 1 重新开始——否则附录里
        # 表格的 `[1]` 会和正文的 `[1]` 指两条不同的证据。
        self._next = max(self._numbers.values(), default=0) + 1
        self._unknown: list[str] = []

    def number(self, evidence_id: str) -> str:
        """取编号，必要时分配一个。返回要写进正文的文本（含方括号）。"""
        if evidence_id not in self._known:
            # 不认识就明说。返回空串会让这句话少一个角标而不留痕迹。
            if evidence_id not in self._unknown:
                self._unknown.append(evidence_id)
            return "[?]"
        existing = self._numbers.get(evidence_id)
        if existing is None:
            existing = self._next
            self._next += 1
            self._numbers[evidence_id] = existing
        return f"[{existing}]"

    def number_of(self, evidence_id: str) -> int | None:
        return self._numbers.get(evidence_id)

    @property
    def cited(self) -> list[tuple[int, str]]:
        """`(编号, evidence_id)`，按编号排序。"""
        return sorted((n, eid) for eid, n in self._numbers.items())

    @property
    def unknown(self) -> list[str]:
        return list(self._unknown)


def _rewrite_markers(text: str, cite: _CitationIndex) -> str:
    """把正文里的 `[证据: EV-x]` 换成 `[1]`。"""
    return _MARKER.sub(lambda match: cite.number(match.group(1)), text)


def _cell(value: Any) -> str:
    """表格单元格：竖线会撕开表格，换行也会。两者都要挡掉。"""
    text = str(value if value is not None else "").strip()
    return text.replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    if not rows:
        return []
    lines = ["| " + " | ".join(_cell(h) for h in headers) + " |"]
    lines.append("|" + "---|" * len(headers))
    lines.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    return lines


def to_markdown(body: dict) -> str:
    """把报告正文渲染成 Markdown。"""
    evidences = {
        str(item.get("evidenceId", "")): item
        for item in (body.get("evidences") or [])
        if item.get("evidenceId")
    }
    # 编号优先取报告里存的那份（`assemble` 算的）。旧报告没有这个键，
    # `number_by_id` 返回空字典，于是一路退回现算。
    cite = _CitationIndex(set(evidences), seeded=number_by_id(body.get("citations")))
    lines: list[str] = []

    subject = str(body.get("subject") or body.get("query") or "竞品分析")
    mode = body.get("mode") or {}
    lines.append(f"# {subject} · 竞品分析报告")
    lines.append("")

    # ---- 抬头 ----
    meta = [
        f"调研模式：{mode.get('label') or mode.get('key') or '未知'}",
        f"生成时间：{body.get('generatedAt') or '未知'}",
    ]
    brands = [str(b) for b in (body.get("brands") or [])]
    if brands:
        meta.append("涉及品牌：" + "、".join(brands))
    if body.get("taskId"):
        meta.append(f"任务编号：{body['taskId']}")
    lines.append("　·　".join(meta))
    lines.append("")

    # ---- 降级：紧跟标题，在目录之前 ----
    degraded = [str(item) for item in (body.get("degraded") or [])]
    if degraded:
        lines.append(f"## ⚠️ 本次运行有 {len(degraded)} 处降级")
        lines.append("")
        lines.append("下面这些内容没有按预期完成，读的时候请留意：")
        lines.append("")
        lines.extend(f"- {item}" for item in degraded)
        lines.append("")

    sections = body.get("sections") or []

    # ---- 目录 ----
    if sections:
        lines.append("## 目录")
        lines.append("")
        for index, section in enumerate(sections, start=1):
            title = str(section.get("title") or section.get("key") or "")
            flag = "（降级）" if section.get("degraded") else ""
            lines.append(f"{index}. {title}{flag}")
        lines.append("")

    # ---- 正文 ----
    # 引用编号要在渲染正文时分配，所以附录必须等正文渲染完再写。
    for section in sections:
        title = str(section.get("title") or section.get("key") or "")
        lines.append(f"## {title}")
        lines.append("")
        if section.get("degraded"):
            lines.append("> 本节内容降级：材料不足以支撑完整论述。")
            lines.append("")
        if section.get("reworked"):
            lines.append("> 本节在返工后重写过。")
            lines.append("")
        content = _rewrite_markers(str(section.get("content") or "").strip(), cite)
        lines.append(content or "_（本节没有正文）_")
        lines.append("")

    # ---- 质量门与指标 ----
    quality = body.get("quality") or {}
    if quality:
        completeness = body.get("completeness") or {}
        labels = completeness.get("labels") or {}
        missing = [str(key) for key in (completeness.get("missing") or [])]
        score = completeness.get("score")

        lines.append("## 质量门")
        lines.append("")
        verdict = "通过" if quality.get("passed") else "未通过"
        lines.append(f"结论：**{verdict}**")
        lines.append("")
        rows = [
            ["证据与覆盖门槛", verdict],
            [
                "完整度",
                f"{score:.0%}" if isinstance(score, int | float) else str(score or "未知"),
            ],
            ["可发布", "是" if quality.get("publishable") else "否"],
            [
                "维度覆盖",
                f"{quality.get('dimensionsCovered', 0)}/{quality.get('dimensionsPlanned', 0)}",
            ],
            ["严重问题", quality.get("blockers", 0)],
        ]
        lines.extend(_table(["项", "值"], rows))
        lines.append("")
        # `passed` 与 `publishable` 是两件事，光给两个"通过／否"读者会以为矛盾：
        # 证据够了不代表内容齐了。缺了什么必须列出来。
        if not quality.get("publishable") and missing:
            lines.append("不能发布，因为下面这些整块内容缺失：")
            lines.append("")
            lines.extend(f"- {labels.get(key, key)}" for key in missing)
            lines.append("")
        # 未通过时说出原因。只给一个"未通过"的结论，读者无从知道该补什么。
        failed = [str(item) for item in (quality.get("failedBecause") or [])]
        if failed:
            lines.append("未通过的原因：")
            lines.append("")
            lines.extend(f"- {item}" for item in failed)
            lines.append("")

    metrics = body.get("metrics") or {}
    if metrics:
        lines.append("## 量化指标")
        lines.append("")
        rows = [
            ["证据条数", metrics.get("evidences", 0)],
            ["独立信源数", metrics.get("independentDomains", 0)],
            [
                "维度覆盖",
                f"{metrics.get('dimensionCoverage', 0):.0%}"
                if isinstance(metrics.get("dimensionCoverage"), int | float)
                else metrics.get("dimensionCoverage", ""),
            ],
            ["论点条数", metrics.get("claims", 0)],
            ["无证据立论率", metrics.get("unsupportedClaimRate", 0)],
            ["幻觉引用率", metrics.get("hallucinationRate", 0)],
            ["交叉验证率", metrics.get("crossValidationRate", 0)],
            ["降级证据率", metrics.get("degradedRate", 0)],
        ]
        lines.extend(_table(["指标", "值"], rows))
        lines.append("")

    # ---- 矩阵 ----
    matrix = body.get("matrix") or {}
    matrix_brands = [str(b) for b in (matrix.get("brands") or [])]
    dimensions = [str(d) for d in (matrix.get("dimensions") or [])]
    scores = matrix.get("scores") or []
    if matrix_brands and dimensions and scores:
        lines.append("## 功能对比矩阵")
        lines.append("")
        rows = []
        for brand_index, brand in enumerate(matrix_brands):
            row_scores = scores[brand_index] if brand_index < len(scores) else []
            rows.append([brand, *[row_scores[i] if i < len(row_scores) else "" for i in range(len(dimensions))]])
        lines.extend(_table(["品牌", *dimensions], rows))
        lines.append("")
        # 图表也是论点，铁律一同样适用。矩阵的证据挂在整块上，
        # 所以角标写在表格后面而不是每一格里。
        markers = [cite.number(str(eid)) for eid in (matrix.get("evidenceIds") or [])]
        if markers:
            lines.append("支撑证据：" + "".join(markers))
            lines.append("")

    # ---- 定价 ----
    pricing_blocks = body.get("pricingModels") or []
    if pricing_blocks:
        lines.append("## 定价")
        lines.append("")
        for block in pricing_blocks:
            brand = str(block.get("brand") or "未标注品牌")
            if block.get("degraded"):
                lines.append(f"### {brand}（降级：没解析出完整档位）")
            else:
                lines.append(f"### {brand}")
            lines.append("")
            facts = []
            if block.get("modelType"):
                facts.append(f"模式：{block['modelType']}")
            if block.get("currency"):
                facts.append(f"币种：{block['currency']}")
            if block.get("freeTier"):
                facts.append(f"免费档：{block['freeTier']}")
            if block.get("entryPrice") is not None:
                facts.append(f"入门价：{block['entryPrice']}")
            if facts:
                lines.append("　·　".join(str(f) for f in facts))
                lines.append("")
            rows = []
            for tier in block.get("tiers") or []:
                rows.append([
                    tier.get("name", ""),
                    tier.get("priceText", ""),
                    tier.get("period", ""),
                    tier.get("unit", ""),
                    "、".join(str(x) for x in (tier.get("includes") or [])),
                ])
            lines.extend(_table(["档位", "价格", "周期", "单位", "包含"], rows))
            lines.append("")

    # ---- 论点 ----
    claims = body.get("claims") or []
    if claims:
        lines.append("## 论点清单")
        lines.append("")
        rows = []
        for claim in claims:
            markers = "".join(cite.number(str(eid)) for eid in (claim.get("evidenceIds") or []))
            rows.append([
                claim.get("text", ""),
                claim.get("confidence", ""),
                "是" if claim.get("crossValidated") else "否",
                claim.get("independentDomains", 0),
                markers,
            ])
        lines.extend(_table(["论点", "置信度", "交叉验证", "独立信源", "证据"], rows))
        lines.append("")

    # ---- 证据附录 ----
    cited = cite.cited
    lines.append("## 证据附录")
    lines.append("")
    if not cited:
        lines.append("_本次报告没有引用任何证据。_")
        lines.append("")
    for number, evidence_id in cited:
        item = evidences[evidence_id]
        site = item.get("siteName") or item.get("site_name") or ""
        credibility = item.get("credibility")
        head = f"[{number}] {item.get('title') or '（无标题）'}"
        if site:
            head += f" — {site}"
        if isinstance(credibility, int | float) and credibility:
            head += f"（可信度 {credibility:.0f}）"
        lines.append(head)
        lines.append("")
        lines.append(f"    {item.get('url', '')}")
        lines.append("")

    unknown = cite.unknown
    if unknown:
        # 不抛异常：一份缺了几条引用的报告仍然该被读。但必须**说出来**，
        # 否则 `[?]` 会被当成排版噪音。
        lines.append("## 引用异常")
        lines.append("")
        lines.append(
            f"正文引用了 {len(unknown)} 个不在证据表里的编号，已在正文中标记为 `[?]`："
        )
        lines.append("")
        lines.extend(f"- {eid}" for eid in unknown)
        lines.append("")

    # ---- 术语表 ----
    glossary = body.get("glossary") or []
    if glossary:
        lines.append("## 术语表")
        lines.append("")
        for item in glossary:
            lines.append(f"- **{item.get('term', '')}**：{item.get('definition', '')}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def to_json(body: dict, *, include_full_text: bool = False) -> str:
    """把报告正文导成 JSON。

    `include_full_text=False` 是本来的行为（理由见模块 docstring），
    `True` 给"我要一份能离线复现的完整快照"这种场景。
    """
    payload = body
    if not include_full_text and body.get("evidences"):
        payload = dict(body)
        payload["evidences"] = [
            {key: value for key, value in item.items() if key != "fullText"}
            for item in body["evidences"]
        ]
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)


def _referenced_ids(body: dict) -> list[str]:
    """报告正文**引用了**的全部 evidence_id。

    刻意从 `body` 里独立扫一遍，而不是复用 `to_markdown` 的中间结果：
    复用的话，渲染器漏掉某一处的引用时，检查也跟着漏——
    检查器和被检查者是同一个人的话，它只能发现"他自己知道的那些错"。
    """
    found: list[str] = []

    for section in body.get("sections") or []:
        found.extend(_MARKER.findall(str(section.get("content") or "")))
        found.extend(str(eid) for eid in (section.get("evidenceIds") or []))

    for claim in body.get("claims") or []:
        found.extend(str(eid) for eid in (claim.get("evidenceIds") or []))

    matrix = body.get("matrix") or {}
    found.extend(str(eid) for eid in (matrix.get("evidenceIds") or []))

    for chart in body.get("charts") or []:
        found.extend(str(eid) for eid in (chart.get("evidenceIds") or []))

    for block in body.get("pricingModels") or []:
        for tier in block.get("tiers") or []:
            found.extend(str(eid) for eid in (tier.get("evidenceIds") or []))

    return found


def citation_problems(body: dict) -> list[str]:
    """导出的 Markdown 里有没有对不上的角标。

    这是"角标点开是空的"在导出层面的等价物，也是本模块唯一一条
    可以独立验证的**不变量**。`to_markdown` 自己保证编号一致，
    但那句话需要一条能红的检查来支撑——否则它只是一句设计声明。

    两条**互相独立**的检查
    ---------------------
    1. **语义**：正文引用的 id 是不是都在证据表里。这条从 `body` 重新扫，
       不看渲染结果。
    2. **编号**：渲染出来的 `[N]` 与附录里的 `[N]` 是不是同一批。

    只有第 2 条的话，一个"引用了不存在的证据"的缺陷会被漏掉——
    它渲染成 `[?]`，压根不是个数字，编号比对看不见它。
    （写这条函数时第一版就只做了第 2 条，被自己的测试抓住了。）
    """
    evidences = {
        str(item.get("evidenceId", ""))
        for item in (body.get("evidences") or [])
        if item.get("evidenceId")
    }

    problems: list[str] = []
    unknown = sorted({eid for eid in _referenced_ids(body) if eid and eid not in evidences})
    for evidence_id in unknown:
        problems.append(f"正文引用了不存在的证据 {evidence_id}（导出时为 `[?]`）")

    markdown = to_markdown(body)

    # 正文里出现的所有 `[N]`
    used = {int(m) for m in re.findall(r"\[(\d+)\]", markdown)}
    # 附录里定义的所有 `[N]`
    appendix = markdown.split("## 证据附录", 1)
    defined = (
        {int(m) for m in re.findall(r"^\[(\d+)\]", appendix[1], flags=re.MULTILINE)}
        if len(appendix) == 2
        else set()
    )

    for number in sorted(used - defined):
        problems.append(f"正文里的角标 [{number}] 在证据附录里找不到")
    for number in sorted(defined - used):
        problems.append(f"证据附录里的 [{number}] 在正文里没有被引用过")

    numbering = sorted(defined)
    if numbering and numbering != list(range(1, len(numbering) + 1)):
        problems.append(f"附录编号不连续：{numbering}")

    return problems
