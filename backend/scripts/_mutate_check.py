"""一次性突变校验：把测试**真的弄红**才算数。

这个脚本不进仓库的正式流程，是我用来回答一个问题的：
"这几条测试是抓住了 bug，还是只是在描述现状？"
一条永远绿的测试比没有测试更坏——它给人已经守住了的错觉。

两条规矩（见 CLAUDE.md）
----------------------
1. **每条突变先数命中次数。** 替换字符串 0 次命中意味着这个突变根本
   没生效，那么"测试没红"说明不了任何事，结论无效。
2. **"这条代码守不住"也必须是被验证过的结论，不是一句搪塞。**
   所以每条突变带一个 `expected`：

   - `caught` —— 期望变红。仍然绿 = 测试是假的，**这是失败**。
   - `defensive` —— 期望**照样绿**，因为该代码在当前 schema 下
     无法被证伪（比如 `COUNT(DISTINCT)` 在一个已有复合主键约束掉
     重复行的表上）。这里红 = **我的注释写错了**，也是失败。

   第二种比第一种容易被放过：一个"反正测不出来"的说法听起来
   很合理，所以它必须和别的突变一样跑一遍。

用法：python -m scripts._mutate_check
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EV = "app/db/repo/evidences.py"
LIB = "tests/unit/test_evidence_library.py"
RUNNER = "app/core/pipeline/runner.py"
DASH = "app/api/routes_dashboard.py"
EXPERTS = "app/api/routes_experts.py"
TASKS = "app/db/repo/tasks.py"
REPORTS = "app/db/repo/reports.py"

T_IDENTITY = "tests/integration/test_identity_sync.py"
T_DASH = "tests/integration/test_dashboard_api.py"
T_EXPERTS = "tests/integration/test_experts_api.py"
T_EVID = "tests/integration/test_evidences_api.py"
T_INTERRUPT = "tests/unit/test_task_interrupt.py"
T_TEAM = "tests/unit/test_team_usage.py"
CONFTEST = "tests/conftest.py"
T_HEALTH = "tests/integration/test_health_api.py"

CITE = "app/core/report/citation_index.py"
EXPORT = "app/core/report/export.py"
ASSEMBLE = "app/core/pipeline/assemble.py"
T_CITE = "tests/integration/test_report_citations.py"
REFINE = "app/core/report/refine.py"
T_REFINE = "tests/integration/test_report_refine.py"
ANALYZE = "app/core/pipeline/analyze.py"
CHARTS = "app/core/analysis/charts.py"
T_MATRIX = "tests/integration/test_matrix_ingest.py"
T_CHARTS = "tests/unit/test_charts_matrix.py"
T_GUARDS = "tests/integration/test_analyze_guards.py"
TASKS_API = "app/api/routes_tasks.py"
TRACES_REPO = "app/db/repo/traces.py"
T_TRACE = "tests/integration/test_task_trace_api.py"
TRACE_PY = "app/core/observability/trace.py"
T_TRACE_UNIT = "tests/unit/test_trace.py"

# (标签, 文件, 原文, 改后, 期望, 目标测试文件)
MUTATIONS = [
    (
        "去掉知识库的 GROUP BY（去重整个失效）",
        EV, '"GROUP BY evidence_id "', '" "', "caught", LIB,
    ),
    (
        "count_library 改数行数而不是来源",
        EV,
        "SELECT COUNT(DISTINCT evidence_id) AS n FROM evidences {where}",
        "SELECT COUNT(*) AS n FROM evidences {where}",
        "caught", LIB,
    ),
    (
        "知识库改按可信度排序（复用次数不再优先）",
        EV,
        '"ORDER BY task_count DESC, credibility DESC, evidence_id LIMIT ? OFFSET ?"',
        '"ORDER BY credibility DESC LIMIT ? OFFSET ?"',
        "caught", LIB,
    ),
    (
        "筛选面改回按行数（选项与列表不同口径）",
        EV,
        "SELECT source_type, COUNT(DISTINCT evidence_id) AS n FROM evidences ",
        "SELECT source_type, COUNT(*) AS n FROM evidences ",
        "caught", LIB,
    ),
    (
        "mentions 也去重（行数与来源数撞成同一个数）",
        EV,
        # 锚在 `count_rows()` 的实现体上。这个函数是仪表盘 `mentions`
        # 与知识库筛选面 `mentions` **共用的唯一实现**（原来是两处
        # 各写一遍的 `COUNT(*)`，见 `问题记录.md` 问题 27），
        # 所以一处突变同时打中两条路径。
        # 带上 `SELECT` 前缀与 `{where}`：`count_library()` 里那句
        # `COUNT(DISTINCT evidence_id) ... {where}` 与它只差一个词。
        'f"SELECT COUNT(*) AS n FROM evidences {where}"',
        'f"SELECT COUNT(DISTINCT evidence_id) AS n FROM evidences {where}"',
        "caught", LIB,
    ),
    (
        "翻页的 LIMIT/OFFSET 参数传反（第一页变空）",
        EV,
        # 锚在第一步那次取数上。这条曾锚在 `_library_rows` 改成两步之前的
        # 老写法（`return get_conn().execute(sql, ...)`）上，函数重写之后
        # 锚点命中 0 次、突变静默失效——脚本只会说"结论无效"，
        # 不会说"这条守卫已经没人验了"。见问题记录 52。
        "        (*params, limit, offset),\n",
        "        (*params, offset, limit),\n",
        "caught", LIB,
    ),
    (
        "taskCount 去掉 DISTINCT",
        EV,
        # 这个字符串在文件里出现两次（宽列版与窄列版各一份），
        # 带上收尾的 `)` 才唯一。不带的话命中 2 次、突变不生效。
        '"COUNT(DISTINCT task_id) AS task_count",\n)',
        '"COUNT(*) AS task_count",\n)',
        # 主键 (task_id, evidence_id) 已经让"同一任务同一来源两行"不可能存在，
        # 所以这两者在当前 schema 下**等价**。留着 DISTINCT 是防御，
        # 不是一条测试能守住的东西——注释里就是这么写的。
        "defensive", LIB,
    ),
    (
        "翻页排序去掉 evidence_id tiebreaker",
        EV,
        '"ORDER BY task_count DESC, credibility DESC, evidence_id LIMIT ? OFFSET ?"',
        '"ORDER BY task_count DESC, credibility DESC LIMIT ? OFFSET ?"',
        # GROUP BY evidence_id 本来就按分组键有序输出，最后那个键是冗余的。
        "defensive", LIB,
    ),
    # ---- 调研对象同步（工作台标题为空那个 bug）----
    (
        "「在持久化路径上同步身份」这一步不做了",
        RUNNER,
        "        self._apply_event(event, persist=True)\n        self._sync_identity()",
        "        self._apply_event(event, persist=True)",
        "caught", T_IDENTITY,
    ),
    (
        "同步的只写一次标记去掉（每个事件都写）",
        RUNNER,
        "        if self._identity_synced:\n            return",
        "        if False:\n            return",
        "caught", T_IDENTITY,
    ),
    (
        "调研对象还没解析出来就写（空值也被写进去）",
        RUNNER,
        "        if ctx is None or not ctx.subject:\n            return",
        "        if ctx is None:\n            return",
        "caught", T_IDENTITY,
    ),
    (
        "只写数据库不更新内存状态",
        RUNNER,
        "        self.state.subject = ctx.subject\n        self.state.brands = list(ctx.brands)",
        "        pass",
        "caught", T_IDENTITY,
    ),
    # ---- 仪表盘的口径 ----
    (
        "mentions 改回「各报告自报之和」（与知识库不同源）",
        DASH,
        "    mentions = evidences_repo.count_rows()",
        '    mentions = int(_sum(reports, "evidences"))',
        "caught", T_DASH,
    ),
    (
        "交叉验证率改成逐份报告求平均",
        DASH,
        '            "crossValidationRate": _ratio(cross_validated, claims),',
        '            "crossValidationRate": round(sum(float(r["metrics"].get("crossValidationRate") or 0) for r in reports) / report_count, 4) if report_count else 0.0,',
        "caught", T_DASH,
    ),
    (
        "按档位的分母改用报告总数",
        DASH,
        '        bucket["avgEvidences"] = round(bucket["evidences"] / bucket["reports"], 1)',
        '        bucket["avgEvidences"] = round(bucket["evidences"] / report_count, 1)',
        "caught", T_DASH,
    ),
    (
        "品牌数按每份报告的品牌数相加（不去重）",
        DASH,
        "            \"brands\": len(brands),",
        "            \"brands\": sum(len(row[\"brands\"]) for row in reports),",
        "caught", T_DASH,
    ),
    (
        "可发布读成通过质量门（两个判断合成一个）",
        DASH,
        '    publishable = sum(1 for row in reports if row["quality"].get("publishable"))',
        '    publishable = sum(1 for row in reports if row["quality"].get("passed"))',
        "caught", T_DASH,
    ),
    # ---- 专家名册 ----
    (
        "页头人数改用筛选后的条数",
        EXPERTS,
        '        "rosterSize": roster_size(),',
        '        "rosterSize": len(items),',
        "caught", T_EXPERTS,
    ),
    (
        "层级改成按字典序排（L1 排到最前）",
        EXPERTS,
        '    ("L3", "决策层", "决定研究边界与最终取舍"),\n    ("L2", "战略层", "各自从一条战略维度切进去"),\n    ("L1", "执行层", "行业与职能专才，负责取证"),',
        '    ("L1", "执行层", "行业与职能专才，负责取证"),\n    ("L2", "战略层", "各自从一条战略维度切进去"),\n    ("L3", "决策层", "决定研究边界与最终取舍"),',
        "caught", T_EXPERTS,
    ),
    (
        "参与度不从报告里数（恒为 0）",
        EXPERTS,
        "    usage = reports_repo.team_usage()\n    items = [\n        _expert(expert, usage.get(expert.expert_id, 0))",
        "    usage = {}\n    items = [\n        _expert(expert, usage.get(expert.expert_id, 0))",
        "caught", T_EXPERTS,
    ),
    (
        "分组筛选面改成从过滤后的名单里算",
        EXPERTS,
        "    groups: dict[str, int] = {}\n    for expert in load_experts():",
        "    groups: dict[str, int] = {}\n    for expert in [e for e in load_experts() if (not level or e.level == level) and (not group or e.group == group)]:",
        "caught", T_EXPERTS,
    ),
    # ---- 知识库接口 ----
    (
        "知识库列表把正文也带出去",
        "app/api/routes_evidences.py",
        '        "snippet": row["snippet"],',
        '        "snippet": row["snippet"], "fullText": row["fullText"],',
        "caught", T_EVID,
    ),
    (
        "搜索只匹配标题不匹配摘要",
        EV,
        '        clauses.append("(title LIKE ? OR snippet LIKE ?)")\n        params.extend([f"%{text}%", f"%{text}%"])',
        '        clauses.append("(title LIKE ?)")\n        params.extend([f"%{text}%"])',
        "caught", T_EVID,
    ),
    (
        "品牌逐份相加（跨报告不去重）",
        DASH,
        "        brands.update(brand for brand in row[\"brands\"] if brand)",
        "        brands.update(brand for row2 in reports for brand in row2[\"brands\"] if brand)",
        "defensive", T_DASH,
    ),
    # ---- 任务清理 ----
    (
        "清理漏掉 awaiting_clarify（卡在澄清的任务永远转圈）",
        TASKS,
        '_UNFINISHED_STATUSES = ("pending", "running", "awaiting_clarify")',
        '_UNFINISHED_STATUSES = ("pending", "running")',
        "caught", T_INTERRUPT,
    ),
    (
        "清理把终态也一起判掉",
        TASKS,
        # 用 `1=1 OR` 而不是换成 `WHERE status <> 'done'`：后者会让
        # 占位符个数对不上（3 个参数、2 个 `?`），sqlite3 直接抛异常，
        # 测试是红了，但红的原因是参数个数而不是"终态被改写"——
        # 那就变成一条抓不住目标 bug 的突变。
        '        f"WHERE status IN ({placeholders})",',
        '        f"WHERE 1=1 OR status IN ({placeholders})",',
        "caught", T_INTERRUPT,
    ),
    (
        "清理不写失败原因（用户看到一个空的 failed）",
        TASKS,
        "SET status = 'failed', error = ?, updated_at = ? ",
        "SET status = 'failed', error = error, updated_at = ? ",
        "caught", T_INTERRUPT,
    ),
    # ---- 队伍解析 ----
    (
        "同一个人挂两个角色被算两次（参与度可以超过报告数）",
        REPORTS,
        "    return {\n        str(member)\n        for members in team.values()\n        if isinstance(members, list)\n        for member in members\n    }",
        "    return [\n        str(member)\n        for members in team.values()\n        if isinstance(members, list)\n        for member in members\n    ]",
        "caught", T_TEAM,
    ),
    (
        "队伍名单被写成字符串时按字符拆开",
        REPORTS,
        "        if isinstance(members, list)",
        "        if True",
        "caught", T_TEAM,
    ),
    (
        "conftest 不再把库指到临时目录（测试写进生产库）",
        CONFTEST,
        # ⚠️ 这条突变**会真的写进 `backend/data/xm3.db`**——把里面所有
        # 非终态任务判失败。这正是它要证明的危害，但也意味着：
        # **有任务正在跑的时候别跑这个脚本。**
        # 想确认危害有多大，就往真库插一条 running 的金丝雀再跑。
        '    monkeypatch.setenv("DB_PATH", str(tmp_path / "xm3-default.db"))\n',
        "",
        "caught", T_HEALTH,
    ),
    # ---- 正文引用编号：存进报告，两个渲染器读同一份 ----
    (
        "装配时不再计算正文引用编号",
        ASSEMBLE,
        '    body["citations"] = build_citations(\n'
        '        body["sections"], {ev.evidence_id for ev in ctx.evidences}\n'
        "    )",
        '    body["citations"] = []',
        "caught", T_CITE,
    ),
    (
        "导出时无视报告里存的编号（回到现算）",
        EXPORT,
        'seeded=number_by_id(body.get("citations"))',
        "seeded=None",
        "caught", T_CITE,
    ),
    (
        "编号改按正文的倒序分配",
        CITE,
        "for section in sections:",
        "for section in reversed(sections):",
        "caught", T_CITE,
    ),
    (
        "附录的编号从种子长度起算而不是从最大号起算",
        EXPORT,
        "self._next = max(self._numbers.values(), default=0) + 1",
        "self._next = len(self._numbers) + 1",
        "caught", T_CITE,
    ),
    (
        "不存在的证据也占一个号",
        CITE,
        "if evidence_id in known and evidence_id not in numbers:",
        "if evidence_id not in numbers:",
        "caught", T_CITE,
    ),
    (
        "深化之后不重发正文编号",
        REFINE,
        "    _renumber(data)",
        "    pass",
        "caught", T_REFINE,
    ),
    (
        "识别出转置却不翻回来（只记降级）",
        ANALYZE,
        "        scores, verdict = orient(dimensions, brands, scores)",
        "        _, verdict = orient(dimensions, brands, scores)",
        "caught", T_MATRIX,
    ),
    (
        "矩阵图的形状闸永远放行",
        CHARTS,
        "    if any(len(row) != len(dimensions) for row in scores):",
        "    if False:",
        "caught", T_CHARTS,
    ),
    # ---- 缺省值伪装成结论 / 披露悄悄消失 ----
    (
        "五力缺强度时补 3.0（未判定变成「中等」）",
        ANALYZE,
        'intensity = as_float(pick(item, "intensity", "强度"), default=0.0)',
        'intensity = as_float(pick(item, "intensity", "强度"), default=3.0)',
        # 这条会**同时**打红两条用例：`<= 0` 的分支不再进，
        # 改写记录里也就没有这一条了。两条都是它该抓的。
        "caught", T_GUARDS,
    ),
    (
        "份额越界不记改写记录（悄悄照原值用）",
        ANALYZE,
        '            ctx.coercion.note(\n'
        '                f"市场份额「{brand}」的数值是 {share_value:g}，超出 0–100 的占比范围，"\n'
        '                "疑似填成了用户规模——这一项不出图，表格里按原值显示"\n'
        "            )",
        "            pass",
        "caught", T_GUARDS,
    ),
    (
        "份额闸永远放行（2022% 也照画）",
        CHARTS,
        # 这条**曾经是绿的**：`test_analyze_guards.py` 里那两条"越界不出图"
        # 的用例把份额行写成 `evidenceIds: []`，于是饼图先被 `build_charts()`
        # 出口那道"没有证据链的图不生成"收口拦掉了——断言过了，
        # 但过的原因是另一道闸。把这里的份额闸换成 `if False:` 实测：
        # 整个文件十项全绿。修法见 `_share_rows()`，见问题记录 52。
        '    if any(not 0 < row["share"] <= 100 for row in rows):',
        "    if False:",
        "caught", T_GUARDS,
    ),
    (
        "份额饼图不再写推算口径",
        CHARTS,
        '            note="份额为推算值，口径见各数据点的 basis 字段",\n',
        "",
        "caught", T_GUARDS,
    ),
    # ---- trace 汇总：两个耗时不是"任务跑了多久" / 降级不是失败 ----
    (
        "总占用改回叫 durationMs（会被接到「耗时」上）",
        TASKS_API,
        '        "spanDurationMs": total_duration,',
        '        "durationMs": total_duration,',
        "caught", T_TRACE,
    ),
    (
        "byKind 里改回叫 durationMs",
        TASKS_API,
        '            kind, {"count": 0, "spanDurationMs": 0, "costUsd": 0.0, "totalTokens": 0}',
        '            kind, {"count": 0, "durationMs": 0, "costUsd": 0.0, "totalTokens": 0}',
        # **指到具体那一条用例**，不指整个文件。原因：这个字段名在
        # `setdefault` 的默认字典与下面的自增语句里各出现一次，
        # 只改一处会先炸出一个 `KeyError`——于是"第一个变红的用例"
        # 是碰巧第一个请求这个接口的 `test_父子关系真的被装成树`，
        # 而不是真正在断言这件事的那条。崩溃也算红，但**证人不对**。
        "caught", f"{T_TRACE}::TestDurationAccounting::test_每个kind里也不叫durationMs",
    ),
    (
        "降级并进失败（24 条降级印成 24 次失败）",
        TASKS_API,
        '        "errorCount": sum(1 for span in spans if span.get("status") == "error"),',
        '        "errorCount": sum(1 for span in spans if span.get("status") not in ("", "ok")),',
        "caught", T_TRACE,
    ),
    (
        "时间戳不补时区（naive 与 aware 混在一张表里就 500）",
        TASKS_API,
        "    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp",
        "    return stamp",
        "caught", T_TRACE,
    ),
    (
        "窗口不取绝对值（时钟回拨给出负的并发度）",
        TASKS_API,
        "    return max(int(span_window.total_seconds() * 1000), 0)",
        "    return int(span_window.total_seconds() * 1000)",
        "caught", T_TRACE,
    ),
    (
        "span 树的兜底改成丢弃（父不在就整棵子树消失）",
        TRACES_REPO,
        "        if parent is not None and parent_id != span[\"spanId\"]:\n"
        "            parent[\"children\"].append(node)\n"
        "        else:",
        "        if parent is not None and parent_id != span[\"spanId\"]:\n"
        "            parent[\"children\"].append(node)\n"
        "        elif parent_id:\n"
        "            pass\n"
        "        else:",
        "caught", T_TRACE,
    ),
    # ---- span 的排序键：编号不是时间戳（见问题 35） ----
    # `spans()` 声称"按开始顺序"，而编号与时间戳**取自两个不同的临界区**
    # （编号在锁里发、时间戳在锁外取），并发下会不一致。
    #
    # 这条被抓住，由三条用例一起证成。**注意前两条的守卫力来自一个巧合**：
    # `_now_iso()` 只到毫秒，而用例里两个 span 是同一毫秒开出来的，
    # 于是 `started_at` 并列、稳定排序退回**完成顺序**，用例因此变红。
    # 把 `_now_iso()` 提到微秒精度，前两条就分辨不出来了。
    #
    # 第三条（`test_spans_order_follows_span_id_even_when_timestamps_contradict_it`）
    # 是为此补的：它**直接构造矛盾**（把第一条的 `started_at` 改到晚于第二条），
    # 所以与时间戳精度无关。三条里只要它还在，"编号 = 开始顺序"就有真守卫。
    (
        "spans() 的排序键从 spanId 编号换成 started_at",
        TRACE_PY,
        "            ordered = sorted(self._spans, key=lambda sp: self._sequence(sp.span_id))",
        "            ordered = sorted(self._spans, key=lambda sp: sp.started_at)",
        "caught", T_TRACE_UNIT,
    ),
]


def run(paths: list[str]) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *paths, "-q", "--no-header", "-x"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def _read_flat(path: Path) -> tuple[str, bool]:
    """读成 LF 文本，并回答"这个文件原本是不是全 CRLF"。

    为什么要绕这一下：本仓库里的文件**两种行尾都有**
    （`repo/events.py` 是 LF，`repo/traces.py` 是 CRLF，来自不同时期的编辑）。
    直接 `read_text()` + `write_text()` 会把 CRLF 转成 LF 再转回 CRLF——
    对全 CRLF 的文件是恒等变换，对**行尾混用**的文件则是把 LF 那几行
    悄悄改成 CRLF。表现是突变跑完后文件"没改"但字节变了，
    将来 diff 里会莫名其妙多出几行。
    """
    raw = path.read_bytes().decode("utf-8")
    crlf, lf = raw.count("\r\n"), raw.count("\n") - raw.count("\r\n")
    flat = raw.replace("\r\n", "\n")
    return flat, (crlf > 0 and lf == 0)


def _write_flat(path: Path, text: str, *, crlf: bool) -> None:
    """按原样写回。`crlf=True` 时把 LF 还原成 CRLF。"""
    path.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def main() -> int:
    failures = 0
    for label, rel, old, new, expected, target in MUTATIONS:
        path = ROOT / rel
        source, crlf = _read_flat(path)
        hits = source.count(old)

        print(f"\n{'=' * 72}\n突变：{label}\n  期望 {expected} · {rel} · 命中 {hits} 次"
              f" · 行尾 {'CRLF' if crlf else 'LF/混用'}")
        if hits != 1:
            print(f"  ✗ 命中 {hits} 次（需要恰好 1 次）—— 这个突变没生效或锚点不唯一，结论无效")
            failures += 1
            continue

        _write_flat(path, source.replace(old, new), crlf=crlf)
        try:
            passed, output = run([target])
        finally:
            _write_flat(path, source, crlf=crlf)

        # `run()` 带 `-x`，所以这里只会有一条。标成"第一个"而不是"这条"：
        # 一个崩溃型突变会让**顺序上最先碰到那行代码**的用例变红，
        # 而那不一定是我写来断言这件事的那条。两种情况都算 caught，
        # 但把证人是谁写准，才知道该信任哪一条断言。
        wrecked = [line for line in output.splitlines() if line.startswith("FAILED")]
        detail = f"（最先变红：{wrecked[0].split('::')[-1][:60]}）" if wrecked else ""

        if expected == "caught":
            if passed:
                print("  ✗ 测试仍然全绿 —— 这条测试抓不住这个 bug")
                failures += 1
            else:
                print(f"  ✓ 测试变红 {detail}")
        else:  # defensive
            if passed:
                print("  ✓ 如注释所述：本 schema 下无法证伪，测试照样绿")
            else:
                print(f"  ✗ 居然变红了 {detail} —— 说明注释里『守不住』的说法是错的，要改")
                failures += 1

    total = len(MUTATIONS)
    print(f"\n{'=' * 72}\n{total - failures}/{total} 条突变符合预期")
    if failures:
        print(f"有 {failures} 条不符预期，见上面标 ✗ 的")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
