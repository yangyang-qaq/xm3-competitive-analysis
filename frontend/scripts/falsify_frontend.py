"""反证前端的守卫：把缺陷放回去，看门禁是否真的红。

一条抓不住 bug 的测试比没有测试更坏——它给出的是"这里有人看着"的错觉。
所以每条守卫都要反证一次：**把漏洞放回去，门禁必须失败**。

这里的"门禁"是两件事，缺一不可：

  1. `tsc -b --noEmit`——`npm run build` 会跑 `tsc -b`，所以一个字段名
     写错的样例会让**构建**失败，而 `vitest` 里那些纯运行时断言拦不住它。
     之前有五条突变里两条只有 tsc 抓得住，所以这里必须两个都跑。
  2. `vitest run`——运行时的键集合比对。

每条突变在 `witness_kind` 里说明**该由谁抓住**：
  `test` —— 必须有一条指定名字的用例失败
  `tsc`  —— 必须编译不过（类型契约的那几条走这里）

跑完逐字节比对还原。`问题记录.md` 问题 10 记过"注释改了、代码没改，
比不改更坏"——还原不彻底会留下一个看起来验证过的现场，所以这一步是自断言的。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

# 用 `node <script>` 而不是 `npx xxx`：Windows 上 npx / tsc 是 `.cmd`，
# 而 `subprocess` 不带 shell 时对 `.cmd` 的支持取决于 CreateProcess 的实现细节。
# `node` 一定是可执行的，`node_modules/.bin` 里的东西全都只是 node 的包装。
NODE = shutil.which("node") or "node"
FRONTEND = pathlib.Path(__file__).resolve().parents[1]
REPO = FRONTEND.parent
VITEST = FRONTEND / "node_modules" / "vitest" / "vitest.mjs"
TSC = FRONTEND / "node_modules" / "typescript" / "bin" / "tsc"


def label(path: pathlib.Path) -> str:
    """相对**仓库根**的路径。有一处突变在 `contracts/` 下，
    而它在 frontend 之外——`relative_to(FRONTEND)` 会在那里直接抛异常。"""
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)

STORE = "src/store/taskStore.ts"
EVENTS = "src/store/taskEvents.ts"
SSE = "src/lib/sse.ts"
HOOK = "src/hooks/useTaskStream.ts"
CLARIFY = "src/pages/ClarifyPage.tsx"
TRACE = "src/components/trace/TracePanel.tsx"
CITE = "src/lib/reportCitation.ts"
DOMAIN = "src/types/domain.ts"
CONTRACT = "../contracts/task_states.json"
CONTRACT_TEST = "src/types/task.contract.test.ts"
REPLAY = "src/lib/replay.ts"
GRAPH = "src/lib/graph.ts"

#: (名字, 文件, 原文, 替换成, witness_kind, witness)
Mut = tuple[str, str, str, str, str, str]

MUTATIONS: list[Mut] = [
    # ---- taskEvents.ts：去重、排序、上限 ----
    (
        "F1 批内不再去重（续传补发与实时推送重叠时，同一条思维出现两遍）",
        EVENTS,
        "    if (event.seq <= watermark) continue\n",
        "    if (false) continue\n",
        "test",
        "同一批里",
    ),
    (
        "F2 去掉按 seq 排序（乱序到达的那几条被当成过期帧永久丢掉）",
        EVENTS,
        "  const fresh = events\n"
        "    .filter((event) => event.seq > watermark)\n"
        "    .sort((a, b) => a.seq - b.seq)\n",
        "  const fresh = events.filter((event) => event.seq > watermark)\n",
        "test",
        "不会被当成过期帧丢掉",
    ),
    (
        "F3 环形上限保留最旧的（工作台永远只看到一场调研的开头）",
        EVENTS,
        "  return list.length > cap ? list.slice(list.length - cap) : list",
        "  return list.length > cap ? list.slice(0, cap) : list",
        "test",
        "思维流截到上限",
    ),
    (
        "F4 没有新事件时也返回新对象（每个空帧白重渲染一次）",
        EVENTS,
        "  if (fresh.length === 0) return { state, lastSeq: watermark }",
        "  if (false) return { state, lastSeq: watermark }",
        "test",
        "没有新事件时返回",
    ),
    (
        "F5 兜底快照的空 nodes 直接覆盖（刷新页面后 DAG 变回一片灰）",
        EVENTS,
        "    nodes:\n"
        "      snapshot.nodes && Object.keys(snapshot.nodes).length > 0\n"
        "        ? snapshot.nodes\n"
        "        : state.nodes,\n",
        "    nodes: snapshot.nodes ?? state.nodes,\n",
        "test",
        "兜底快照的空 nodes",
    ),
    (
        "F6 快照里空字符串的 reportId 覆盖掉已有的（报告链接突然消失）",
        EVENTS,
        "    reportId: snapshot.reportId || state.reportId,",
        "    reportId: snapshot.reportId ?? state.reportId,",
        "test",
        "快照里空字符串的 reportId",
    ),
    (
        "F7 图表不再按 chartId 去重（返工后图集里出现两张同样的图）",
        EVENTS,
        "      state.charts = [...state.charts.filter((c) => c.chartId !== event.chart.chartId),\n"
        "        event.chart]",
        "      state.charts = [...state.charts, event.chart]",
        "test",
        "同一个 chartId",
    ),
    # ---- sse.ts：分发、水位、出错 ----
    (
        "F8 一个监听器都不挂（连接 200、控制台干净、页面全空）",
        SSE,
        "  for (const type of EVENT_TYPES) {",
        "  for (const type of ([] as string[])) {",
        "test",
        "具名帧能收到",
    ),
    (
        "F9 onerror 里主动 close（网络抖一下就永久停更）",
        SSE,
        "  source.onerror = () => {\n    if (closed) return\n",
        "  source.onerror = () => {\n    source.close()\n    if (closed) return\n",
        "test",
        "浏览器自己在退避重连",
    ),
    (
        "F10 水位改成分发之后再推进（分发抛异常就卡死在同一帧）",
        SSE,
        "      watermark = Math.max(watermark, parsed.seq)\n      handlers.onEvent(parsed)\n",
        "      handlers.onEvent(parsed)\n      watermark = Math.max(watermark, parsed.seq)\n",
        "test",
        "先推进再分发",
    ),
    (
        "F11 fromSeq 为 0 时也带查询参数（后端 422，工作台永远空白）",
        SSE,
        "  const query = fromSeq > 0 ? `?from_seq=${fromSeq}` : ''",
        "  const query = `?from_seq=${fromSeq}`",
        "test",
        "fromSeq 为 0 或缺失时不带查询参数",
    ),
    (
        "F12 负数水位不夹到 0（初始水位变成 -5）",
        SSE,
        "  const fromSeq = Math.max(0, options.fromSeq ?? 0)",
        "  const fromSeq = options.fromSeq ?? 0",
        "test",
        "负数水位被夹到 0",
    ),
    # ---- taskStore.ts：合帧、水位、连接生命周期 ----
    (
        "F13 reset 不清缓冲（重置之后上一轮的帧还会渲染进来）",
        STORE,
        # 锚点全在代码上。第一版锚的是 `pending = []\n scheduled = false`，
        # 而这两行之间本来夹着一段注释——改注释就让它失效了。
        #
        # 第二版（现在这条）改成了连 `clearScheduled()` 一起删。原因是
        # `scheduled = false` 后来被抽成了 `clearScheduled()`，而它**顺带
        # 取消了那个已排好的 rAF 回调**——只删 `pending = []` 的话，
        # 回调被取消、`flush()` 再也跑不到，缓冲里那条帧**没有任何人看得见**，
        # 于是用例照样是绿的（实测：单独删 `pending = []` 抓不住）。
        # 语义上"不清缓冲"本来也该是两行一起不动。
        "    pending = []\n    clearScheduled()\n    set({",
        "    set({",
        "test",
        "reset 丢掉缓冲里还没落地的帧",
    ),
    (
        "F14 重连水位只看 fromSeq（每次重连白传几百帧已经看过的事件）",
        STORE,
        "    const resumeFrom = Math.max(fromSeq ?? 0, get().lastSeq)",
        "    const resumeFrom = fromSeq ?? 0",
        "test",
        "重连时取 fromSeq 与手上水位的较大者",
    ),
    (
        "F15 hydrate 直接把水位赋过去（后到的旧快照把它拉回去）",
        STORE,
        "      lastSeq: Math.max(state.lastSeq, snapshot.lastSeq ?? 0),",
        "      lastSeq: snapshot.lastSeq ?? 0,",
        "test",
        "hydrate 只增水位",
    ),
    (
        "F16 disconnect 把 fatal 抹成 idle（界面说空闲，其实永远不会再来了）",
        STORE,
        "      connection: isSettled(state.connection) ? state.connection : 'idle',",
        "      connection: 'idle',",
        "test",
        "不会把 fatal 抹成 idle",
    ),
    (
        "F17 disconnect 把缓冲里的帧一起 flush（断开反而多渲染一次）",
        STORE,
        # 与 F13 同一个道理：`clearScheduled()` 会连回调一起取消，
        # 所以"把帧 flush 出去"这件事要成立，两行都得留着不动。
        "    pending = []\n    clearScheduled()\n    set((state) => ({\n"
        "      connection: isSettled(state.connection) ? state.connection : 'idle',",
        "    set((state) => ({\n"
        "      connection: isSettled(state.connection) ? state.connection : 'idle',",
        "test",
        "丢掉缓冲里还没落地的帧",
    ),
    # ---- 类型契约：这几条只有 tsc 抓得住 ----
    (
        "F18 TaskStatus 丢掉一个成员（cancelled 在列表里渲染成空标签）",
        DOMAIN,
        "export type TaskStatus =\n"
        "  | 'pending'\n"
        "  | 'running'\n"
        "  | 'awaiting_clarify'\n"
        "  | 'done'\n"
        "  | 'failed'\n"
        "  | 'cancelled'",
        "export type TaskStatus =\n"
        "  | 'pending'\n"
        "  | 'running'\n"
        "  | 'awaiting_clarify'\n"
        "  | 'done'\n"
        "  | 'failed'",
        "tsc",
        "",
    ),
    (
        "F19 任务行的字段名写错（clarifyAnswers -> answers，读到的永远是 undefined）",
        CONTRACT_TEST,
        "  clarifyAnswers: {},",
        "  answers: {},",
        "tsc",
        "",
    ),
    (
        "F20 契约文件里去掉一个必需状态",
        CONTRACT,
        '    "required": ["pending", "running", "awaiting_clarify", "done", "failed", "cancelled"],',
        '    "required": ["pending", "running", "done", "failed", "cancelled"],',
        "test",
        "联合类型的成员与契约严格相等",
    ),
    (
        "F21 把 awaiting_clarify 放进终态集合（用户答完澄清，流水线再也不动）",
        CONTRACT,
        '      "values": ["done", "failed", "cancelled"]',
        '      "values": ["done", "failed", "cancelled", "awaiting_clarify"]',
        "test",
        "终态集合与契约一致",
    ),
    (
        "F22 兜底快照也带上 elapsedMs（两个生产者的差异被抹平）",
        CONTRACT_TEST,
        "  evidenceCount: 12,\n} satisfies TaskSnapshot",
        "  evidenceCount: 12,\n  elapsedMs: 1,\n} satisfies TaskSnapshot",
        "test",
        "两个生产者的差异与契约一致",
    ),
    # ---- useTaskStream.ts：先快照、后连流、切任务不继承 ----
    (
        "F23 切任务时不 reset store（带着上一个任务的水位去连，新任务一条历史都不补）",
        HOOK,
        "        store.reset()\n        store.hydrate(snapshot)",
        "        store.hydrate(snapshot)",
        "test",
        "水位不继承上一个任务的",
    ),
    (
        "F24 拿到快照却不喂给 store 就接流（水位从 0 开始，刷新后重传整个 journal）",
        HOOK,
        "        store.reset()\n        store.hydrate(snapshot)\n",
        "        store.reset()\n",
        "test",
        "续传水位来自快照",
    ),
    (
        "F25 connect 挪到 hydrate 前面（水位还是 0，且界面先渲染出一片空）",
        HOOK,
        "        store.hydrate(snapshot)\n",
        "        store.connect(taskId)\n        store.hydrate(snapshot)\n",
        "test",
        "流打开的那一刻",
    ),
    (
        "F26 丢掉 cancelled 守卫（慢的那条旧快照回来，把当前任务盖掉并另开一条流）",
        HOOK,
        "        if (cancelled) return\n        const store = useTaskStore.getState()",
        "        const store = useTaskStore.getState()",
        "test",
        "姗姗来迟",
    ),
    (
        "F27 加载状态不再记'这份结果属于哪个任务'（切任务时闪一帧上一个任务的界面）",
        HOOK,
        "    load: !taskId ? 'notFound' : fresh ? result.load : 'loading',",
        "    load: !taskId ? 'notFound' : result.load,",
        "test",
        "没有任何一次渲染拿新 id 配旧的 ready",
    ),
    (
        "F28 空的 taskId 不再单独判（页面停在'正在读取任务…'永远不往下走）",
        HOOK,
        "    load: !taskId ? 'notFound' : fresh ? result.load : 'loading',",
        "    load: fresh ? result.load : 'loading',",
        "test",
        "空的 taskId 直接判 notFound",
    ),
    (
        "F29 404 混进普通错误（一个被删掉的任务显示'网络错误'，让人去查网络）",
        HOOK,
        "        if (err instanceof ApiError && err.status === 404) {\n"
        "          setResult({ forTask: taskId, load: 'notFound', error: '' })\n"
        "          return\n"
        "        }\n",
        "",
        "test",
        "404 是 notFound",
    ),
    (
        "F30 重试时不立刻回到加载中（点完'重试'还看得见上一次的错误信息）",
        HOOK,
        "      setResult((current) => ({ ...current, load: 'loading', error: '' }))\n"
        "      setNonce((value) => value + 1)",
        "      setNonce((value) => value + 1)",
        "test",
        "重试：立刻回到",
    ),
    (
        "F31 卸载时不断开流（离开工作台后事件还在往 store 里灌）",
        HOOK,
        "      cancelled = true\n      useTaskStore.getState().disconnect()",
        "      cancelled = true",
        "test",
        "卸载时断开流",
    ),
    # ---- ClarifyPage.tsx：澄清页的三条硬要求 ----
    (
        "F32 选项为空时不再退化成自由文本（模型没给选项，任务就卡死在这一页）",
        CLARIFY,
        "  const freeText = question.options.length === 0 || question.kind === 'text'",
        "  const freeText = question.kind === 'text'",
        "test",
        "一个选项都没有的问题退化成自由文本",
    ),
    (
        "F33 用 needClarify（事实）代替 awaitingClarify（状态）判断去留"
        "（一个早就答过的任务每次打开都被拉回澄清页）",
        CLARIFY,
        "    if (load === 'ready' && !awaiting && needClarify) {",
        "    if (load === 'ready' && needClarify) {",
        "test",
        "在等回答：停住",
    ),
    (
        "F34 干脆不看 needClarify（从不提问的任务被弹去工作台，白屏闪一下）",
        CLARIFY,
        "    if (load === 'ready' && !awaiting && needClarify) {",
        "    if (load === 'ready' && !awaiting) {",
        "test",
        "从来没需要过澄清",
    ),
    (
        "F35 多选答案改用逗号拼接（约定换了，后端拿到一整串认不出的东西）",
        CLARIFY,
        "                  onChange(next.join('、'))",
        "                  onChange(next.join(','))",
        "test",
        "顿号连起来",
    ),
    (
        "F36 单选再点一次不再取消（用户反悔了也没法回到空答案）",
        CLARIFY,
        "                    onChange(picked ? '' : option)",
        "                    onChange(option)",
        "test",
        "回到空答案",
    ),
    (
        "F37 空答案也往上报（『用户答了，答的是空』与『没碰过』混成一种）",
        CLARIFY,
        "    const payload = Object.fromEntries(\n"
        "      Object.entries(answers).filter(([, value]) => value.trim() !== ''),\n"
        "    )",
        "    const payload = answers",
        "test",
        "自由文本只填了空白",
    ),
    # ---- TracePanel.tsx：三个数字，三种算法 ----
    #
    # 这一片全部是**算错了不会有人发现**的那一类：界面照常渲染、
    # 数字照常好看，只是含义变了。"求和"写成"取最后一个"，得到的仍然
    # 是一个合理的数——所以每一条都要靠具体的数去抓，而不是靠"渲染出来了"。
    (
        "F38 累计成本取最后一条 span 而不是求和（标签还写着『累计』）",
        TRACE,
        "    cost += span.costUsd\n",
        "    cost = span.costUsd\n",
        "test",
        "成本与 token 是全部 span 的和",
    ),
    (
        "F39 总 token 取最后一条 span 而不是求和",
        TRACE,
        "    tokens += span.totalTokens\n",
        "    tokens = span.totalTokens\n",
        "test",
        "成本与 token 是全部 span 的和",
    ),
    (
        "F40 命中缓存取最后一条 span 而不是求和",
        TRACE,
        "    cached += span.cachedPromptTokens\n",
        "    cached = span.cachedPromptTokens\n",
        "test",
        "命中缓存非 0 时显示真实数字",
    ),
    (
        "F41 最慢的一次改成最后一条（span 按开始时间落，不按耗时）",
        TRACE,
        "    if (slowest === null || span.durationMs > slowest.durationMs) slowest = span\n",
        "    slowest = span\n",
        "test",
        "慢的那条在中间时也能找出来",
    ),
    (
        "F42 命中为 0 时显示 0 而不是破折号（『没命中』冒充『没测量』）",
        TRACE,
        "          value={cached > 0 ? formatInt(cached) : '—'}",
        "          value={formatInt(cached)}",
        "test",
        "命中缓存为 0 时显示破折号",
    ),
    (
        "F43 空数组不再提前返回（渲染一堆 0，像是『跑过了但什么都没花』）",
        TRACE,
        "  if (spans.length === 0) {\n"
        '    return <p className="py-4 text-center text-xs text-fg-faint">还没有调用记录。</p>\n'
        "  }\n",
        "  // 突变：空数组也往下走\n",
        "test",
        "没有 span 时说清楚",
    ),
    (
        "F44 purpose 为空时不退回 name（最慢的那条显示成空白行）",
        TRACE,
        "            {slowest.purpose || slowest.name}",
        "            {slowest.purpose}",
        "test",
        "退回",
    ),
    (
        "F45 分组计数不累加（三个 fetch 显示成 ×1）",
        TRACE,
        "    bucket.count += 1\n",
        "    bucket.count = 1\n",
        "test",
        "同一类型不重复出行",
    ),
    (
        "F46 分组耗时取最后一条而不是求和",
        TRACE,
        "    bucket.durationMs += span.durationMs\n",
        "    bucket.durationMs = span.durationMs\n",
        "test",
        "每种类型一行",
    ),
    # ---- 引用编号：读存下来的那份，不是每次现算 ----
    (
        "F47 报告页不读存下来的引用编号（每次按正文现算一遍）",
        CITE,
        "  for (const item of body.citations ?? []) {",
        "  for (const item of []) {",
        # 这一条是**唯一**能分开"读存的"与"现算"的突变：
        # 真实数据里两者恰好给出同一份编号，所以别的用例对它全是绿的。
        # 见 `ReportPage.test.tsx` 那组用例的注释。
        "test",
        "存下来的编号变了",
    ),
    # ---- lib/replay.ts：回放的步骤顺序（见问题 35） ----
    #
    # 这三条的**测试数据都是构造的**，这一点必须说清楚：真库里 1036 行
    # 没有一行有 `parent_id`，而且 15 个任务的"按编号"与"按时间"两种排法
    # 结果完全一致。也就是说，**拿真数据喂这一层，这些缺陷一条都抓不住**——
    # 正是问题 33.4 那个教训在另一个文件上的重演。
    (
        "R1 flattenSpans 的排序键换回 startedAt 优先（与后端 spans() 分家）",
        REPLAY,
        "    const bySequence = spanSequence(left.span.spanId) - spanSequence(right.span.spanId)\n"
        "    if (bySequence !== 0) return bySequence\n"
        "\n"
        "    const a = parseTime(left.span.startedAt)\n"
        "    const b = parseTime(right.span.startedAt)\n"
        "    if (a !== null && b !== null && a !== b) return a - b\n"
        "    if (a === null && b !== null) return -1\n"
        "    if (a !== null && b === null) return 1\n"
        "    return 0",
        "    const a = parseTime(left.span.startedAt)\n"
        "    const b = parseTime(right.span.startedAt)\n"
        "    if (a !== null && b !== null && a !== b) return a - b\n"
        "    if (a === null && b !== null) return -1\n"
        "    if (a !== null && b === null) return 1\n"
        "    return spanSequence(left.span.spanId) - spanSequence(right.span.spanId)",
        "test",
        "编号与开始时间矛盾时",
    ),
    (
        "R2 buildTimeline 再按 at 排一遍（第三种顺序，且 cumulativeMs 不再是前缀和）",
        REPLAY,
        "  const endMs = steps.reduce((max, step) => Math.max(max, step.at), startMs)",
        "  steps.sort((left, right) => left.at - right.at || left.index - right.index)\n"
        "  const endMs = steps.reduce((max, step) => Math.max(max, step.at), startMs)",
        "test",
        "步骤顺序就是编号顺序",
    ),
    (
        "R3 spanHosts 把 search 的 sites 也算成碰到的主机",
        REPLAY,
        "  const domain = span.detail?.['domain']\n"
        "  if (typeof domain !== 'string' || !domain) return []\n"
        "  return [domain.toLowerCase().replace(/^www\\./, '')]",
        "  const domain = span.detail?.['domain']\n"
        "  const sites = span.detail?.['sites']\n"
        "  if (typeof domain !== 'string' || !domain) {\n"
        "    return Array.isArray(sites) ? sites.map((item) => String(item).toLowerCase()) : []\n"
        "  }\n"
        "  return [domain.toLowerCase().replace(/^www\\./, '')]",
        "test",
        "search span 的 sites 不算数",
    ),
    # ---- lib/graph.ts：知识图谱（见问题 37） ----
    #
    # 这一层的缺陷全都由**真实报告**先发现，单元测试一条都没抓到——
    # 夹具里每条证据都只命中一个维度，而真报告里不是。
    # 所以 G1/G5 的测试数据是**故意造出来**的那种形态，注释里写明了这一点。
    (
        "G1 品牌权重改按命中次数记（点的大小与「几条证据」脱钩，且差额恒为 0）",
        GRAPH,
        "    brandWeight.set(match.brand, (brandWeight.get(match.brand) ?? 0) + 1)",
        "    brandWeight.set(\n"
        "      match.brand,\n"
        "      (brandWeight.get(match.brand) ?? 0) + match.dimensions.length,\n"
        "    )",
        "test",
        "品牌节点的 weight 是证据条数",
    ),
    (
        "G2 没有证据的维度不再标 uncovered（图上抹掉一个结论）",
        GRAPH,
        "      uncovered: (dimensionWeight.get(dim) ?? 0) === 0,",
        "      uncovered: false,",
        "test",
        "一条证据都没有的维度仍然是个节点",
    ),
    (
        "G3 取证据时不再走「进图」判据（图上 45 条，点开 72 行）",
        GRAPH,
        "    const match = plannedMatches(evidence, planned)\n"
        "    if (!match) return false\n"
        "    if (filter.brand !== undefined && match.brand !== filter.brand) return false\n"
        "    if (filter.dimension !== undefined && !match.dimensions.includes(filter.dimension)) return false\n"
        "    return true",
        "    if (filter.brand !== undefined && evidence.brand !== filter.brand) return false\n"
        "    if (filter.dimension !== undefined) {\n"
        "      if (!(evidence.matchedDimensions ?? []).includes(filter.dimension)) return false\n"
        "    }\n"
        "    return true",
        "test",
        "图上说有几条",
    ),
    (
        "G4 「维度名对不上」与「没标注维度」不再分开（两种丢法合并成一个数）",
        GRAPH,
        "      if ((evidence.matchedDimensions ?? []).length > 0) droppedUnknownDimension += 1",
        "      droppedUnknownDimension += 1",
        "test",
        "进不了的证据按原因分开数",
    ),
    (
        "G5 布局不再深拷贝边数组（forceLink 就地改写调用方的 GraphLink）",
        GRAPH,
        "  const simulationLinks = graph.links.map((item) => ({ ...item }))",
        "  const simulationLinks = graph.links",
        "test",
        "布局不改动传进来的图",
    ),
    (
        "G6 初始位置改用 Math.random（两次布局不再一致）",
        GRAPH,
        "    const angle = (2 * Math.PI * index) / Math.max(1, graph.nodes.length)\n"
        "    const radius = Math.min(width, height) * 0.34\n"
        "    return { ...node, x: cx + radius * Math.cos(angle), y: cy + radius * Math.sin(angle) }",
        "    return {\n"
        "      ...node,\n"
        "      x: Math.random() * width,\n"
        "      y: Math.random() * height,\n"
        "    }",
        "test",
        "两次布局的坐标完全相同",
    ),
]


def _run(cmd: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def run_gate() -> dict:
    """跑两个门禁。返回 {"tsc_ok", "vitest_ok", "passed", "failed", "names", ...}。

    **`vitest_ok` 和失败数不是一回事，两个都要看。** 收集阶段崩掉时
    （比如某个 import 解析不了）vitest 会给出 `success: false`，而
    `numFailedTests` 是 **0**——一条用例都没跑，自然一条都没失败。
    那正是这个脚本自己的快照曾经干过的事：备份文件被 vitest 收走、
    解析不了相对 import，于是 `success:false / failed:0` 并存。

    只看失败数的话，这种"仪器坏了"会被读成"全过"（基线时），
    或者被读成"这条突变没抓住"（判证人时——而证人根本没机会跑）。
    两种都是在说谎。
    """
    tsc = _run([NODE, str(TSC), "-b", "--noEmit"], FRONTEND)

    with tempfile.TemporaryDirectory() as tmp:
        report = pathlib.Path(tmp) / "vitest.json"
        vitest = _run(
            [NODE, str(VITEST), "run", "--reporter=json", f"--outputFile={report}"],
            FRONTEND,
        )
        if report.exists():
            raw = json.loads(report.read_text(encoding="utf-8"))
        else:
            # 配置层面就崩了（import 错误之类）。当成全红处理，
            # 但把数字留空，免得看起来像"跑过了而且全过"。
            raw = {"numPassedTests": 0, "numFailedTests": 0, "testResults": []}

    names = [
        result.get("fullName") or result.get("title") or ""
        for file in raw.get("testResults", [])
        for result in file.get("assertionResults", [])
        if result.get("status") == "failed"
    ]
    # 收集失败会挂在**文件**上（`testResults[].status == "failed"` 且
    # `assertionResults` 为空），这些文件名要在报错里露出来，
    # 否则"vitest 自己崩了"和"某个用例红了"在输出里长得一样。
    broke = [
        file.get("name", "?")
        for file in raw.get("testResults", [])
        if file.get("status") == "failed" and not file.get("assertionResults")
    ]
    return {
        "tsc_ok": tsc.returncode == 0,
        "tsc_out": (tsc.stdout or "") + (tsc.stderr or ""),
        # `vitest_ok` 的意思是"这次运行的结果**可信**"，不是"测试全过了"。
        #
        # 第一版写的是 `returncode == 0 and success is not False`，错了：
        # 一条突变被抓住时，正是**要**让 vitest 退非零、`success: false`。
        # 于是每一条"抓住"后面都跟着一句"门禁自己崩了"——46 条里 46 条
        # 全在喊狼来了，而这个脚本的价值全在"报警要能信"上。
        # 判据写错的方向很典型：我把"仪器坏了"和"仪器给出了我不想听的读数"
        # 当成了同一件事。
        #
        # 真正说明仪器坏了的是**收集级失败**：某个测试文件 `status == "failed"`
        # 却一条用例都没产生（`assertionResults` 为空）。那意味着测试根本没跑，
        # 而不是跑了并给出结论。退出码 0/1 都是正常收尾（1 = 有用例红了）。
        "vitest_ok": (
            vitest.returncode in (0, 1)
            and raw.get("numTotalTests", 0) > 0
            and not broke
        ),
        "broke": broke,
        "passed": raw.get("numPassedTests", 0),
        "failed": raw.get("numFailedTests", 0),
        "names": names,
    }


# ------------------------------------------------------------------
# 基线快照：让"现场干不干净"变成一次**比对**，而不是一次猜测
#
# 在此之前这里用的是锚点启发式：把每条突变的"替换后文本"拿去文件里找，
# 找到就认为它还留着。它出过一次**误诊**（问题记录 22）：被强杀的现场里
# 真正留着的是 F25（插入式：把 `store.connect` 插到 `store.hydrate` 前面），
# 而它报的是 F23 和 F24——因为 F25 插进去的那一行**切断了 F23/F24 的锚点**，
# 于是那两条的"原文不在了、替换后的文本还在"同时成立。按它的报告去还原，
# 会把真正的残留留下、再往文件里塞一句多余的 `store.reset()`。
#
# 启发式在这里没有出路：**突变之间会互相遮挡**。所以改成开跑前把每个
# 目标文件的原始字节存一份，之后一切判断都退化成哈希比对——
# 精确、不猜，而且能**修**（`--restore`）。
# ------------------------------------------------------------------

# 快照放在**仓库根**，不放 `frontend/` 里。两个理由，各自独立：
#
#   1. `vitest` 没有配 `include`，用的是默认 glob `**/*.{test,spec}.?(c|m)[jt]s?(x)`，
#      而默认 exclude 里**没有**我们自己的目录名。于是放在 frontend 下的备份
#      会被当成测试收走——其中一份的名字正好以 `.test.ts` 结尾
#      （`task.contract.test.ts` 的副本），它会真的被跑一遍。那等于
#      这个脚本往自己用来判断的仪器里塞东西：基线数字虚高，
#      而且真有一份红的话，红的还是个副本。
#      跑在 frontend 之外的目录就不在任何 glob 的射程内。
#   2. `tsc` 的 `include` 是 `["src"]`，同样够不着。
#
# 备份文件名统一加 `.bak`：即使以后有人把 glob 放宽，`*.test.ts.bak`
# 也匹配不上 `?(c|m)[jt]s?(x)` 结尾。位置和名字是两道独立的锁，
# 任何一道单独失效都还能挡住。
BASELINE = REPO / ".falsify-baseline"
# 开跑时落下的标记，正常结束时删掉。它存在 = 上一轮**被强杀**，
# 那时文件可能停在任何一个突变状态，而"被强杀过"这件事本身就是
# 唯一可靠的线索——比事后去文件里猜哪条突变还留着要结实得多。
RUNNING = BASELINE / "RUNNING"


def _resolve(rel: str) -> pathlib.Path:
    """清单里的路径 → 真实路径。

    **清单里的键是 `label()` 给的，也就是相对仓库根的**（有一处突变在
    `contracts/` 下，它在 frontend 之外）。所以基准目录是 `REPO` 而不是
    `FRONTEND`——第一版写成了 `FRONTEND / rel`，于是每个文件都被解析到
    `frontend/frontend/...` 去，查出来一律「文件不存在」，
    `--restore` 也一个都还原不了。一致性靠这一个函数收口。
    """
    return (REPO / rel).resolve()


def _backup(rel: str) -> pathlib.Path:
    """备份路径：保留目录结构（人工比对时一眼看得出对应关系），加 `.bak` 后缀。"""
    return BASELINE / "files" / (rel + ".bak")


def first_difference(a: str, b: str) -> str:
    """两段文本第一个不同的地方，给人看。

    只说"这个文件的哈希变了"能告诉人**有**残留，但没法判断
    "这到底是被强杀留下的，还是我自己刚改的"。给出行号和两侧原文，
    这个问题一眼就能答——而 `--restore` 会覆盖文件，用户得先能判断。
    """
    a_lines, b_lines = a.splitlines(), b.splitlines()
    for i, (left, right) in enumerate(zip(a_lines, b_lines), start=1):
        if left != right:
            return f"第 {i} 行：基线 {left.strip()[:48]!r} ≠ 当前 {right.strip()[:48]!r}"
    if len(a_lines) != len(b_lines):
        extra = "当前多了" if len(b_lines) > len(a_lines) else "当前少了"
        return f"前 {min(len(a_lines), len(b_lines))} 行相同，{extra} {abs(len(b_lines) - len(a_lines))} 行"
    return "内容不同（只差结尾换行符）"


def snapshot_baseline(targets: list[pathlib.Path]) -> dict[pathlib.Path, str]:
    """把目标文件的原始内容存到 `BASELINE`，返回 {路径: 原文}。"""
    (BASELINE / "files").mkdir(parents=True, exist_ok=True)
    originals: dict[pathlib.Path, str] = {}
    manifest: dict[str, dict[str, str]] = {}
    for path in targets:
        text = path.read_text(encoding="utf-8")
        originals[path] = text
        rel = label(path)
        backup = _backup(rel)
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(text, encoding="utf-8")
        manifest[rel] = {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
    (BASELINE / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return originals


def load_manifest() -> dict[str, dict[str, str]] | None:
    """上一次开跑前留下的清单。没有就返回 None。"""
    path = BASELINE / "manifest.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def dirty_against_baseline() -> list[str]:
    """当前树里哪些文件与快照**逐字节不同**，附带第一处差异。

    这是**精确**判断：只回答"字节一样吗"。相比之下，之前那版拿
    "哪条突变的替换后文本还在文件里"去猜的做法会张冠李戴——
    突变之间会互相遮挡（见上面的注释）。
    """
    manifest = load_manifest()
    if manifest is None:
        return []
    dirty: list[str] = []
    for rel in sorted(manifest):
        path = _resolve(rel)
        backup = _backup(rel)
        if not path.exists():
            dirty.append(f"  {rel}：文件不存在")
            continue
        current = path.read_text(encoding="utf-8")
        if hashlib.sha256(current.encode("utf-8")).hexdigest() == manifest[rel]["sha256"]:
            continue
        before = backup.read_text(encoding="utf-8") if backup.exists() else ""
        dirty.append(f"  {rel}：{first_difference(before, current)}")
    return dirty


def restore_from_baseline() -> list[str]:
    """把快照写回文件。返回还原过的路径。"""
    manifest = load_manifest()
    if manifest is None:
        return []
    restored: list[str] = []
    for rel in sorted(manifest):
        path = _resolve(rel)
        backup = _backup(rel)
        if not backup.exists():
            continue
        text = backup.read_text(encoding="utf-8")
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            restored.append(rel)
    return restored


def _status() -> int:
    """`--status`：只报告现场，不改任何东西。"""
    manifest = load_manifest()
    if manifest is None:
        print("还没有快照（这个脚本没跑过）。")
        return 0
    print(f"快照里有 {len(manifest)} 个文件，最后开跑前记录于 manifest.json。")
    if RUNNING.exists():
        print("RUNNING 标记还在：上一轮**没有正常结束**，差异更可能是它的残留。")
    dirty = dirty_against_baseline()
    if not dirty:
        print("现场与快照逐字节一致。")
        return 0
    print("现场与快照不同：")
    for line in dirty:
        print(line)
    print("\n上面这些差异可能是被强杀留下的残留，也可能是你自己后来的改动。")
    print("要恢复成快照：--restore（**会覆盖**，先看清楚上面每一行）。")
    print("要接受现状、把快照更新成现在这样：--resnapshot。")
    return 1


def _restore() -> int:
    """`--restore`：把快照写回，并把残留标记清掉。"""
    if load_manifest() is None:
        print("没有快照可还原。")
        return 1
    dirty = dirty_against_baseline()
    if not dirty:
        print("现场本来就与快照一致，没有可还原的。")
    else:
        print("以下差异将被**快照的内容覆盖**：")
        for line in dirty:
            print(line)
    restored = restore_from_baseline()
    RUNNING.unlink(missing_ok=True)
    if restored:
        print("\n已还原：")
        for rel in restored:
            print(f"  {rel}")
    # 再查一遍：还原之后必须逐字节一致，否则这次的还原本身就该被怀疑。
    still = dirty_against_baseline()
    print(f"还原后与快照一致：{not still}")
    return 1 if still else 0


def main() -> int:
    if "--status" in sys.argv:
        return _status()
    if "--restore" in sys.argv:
        return _restore()

    targets = sorted({(FRONTEND / path).resolve() for _, path, _, _, _, _ in MUTATIONS})

    # 这个脚本会改源码。`finally` 能盖住异常，盖不住进程被强杀。
    print("会被改动的文件（结束时逐字节还原）：")
    for path in targets:
        print(f"  {label(path)}")
    print("=" * 78)

    # 开跑前先确认现场干净。判据只有一条：**与快照逐字节相同吗**。
    #
    # 这一版之前这里靠"哪条突变的替换后文本还在文件里"去猜，猜错过一次
    # （把 F25 的残留认成了 F23/F24，见问题记录 22）。根子在于突变之间
    # 会互相遮挡：插入式的那条切断了另外两条的锚点，于是那两条全都"命中"。
    # 比对字节没有这个毛病——它不解释现场，只描述现场。
    #
    # `RUNNING` 标记不参与判断，只用来**解释**：它让报错能说清
    # "这是上一轮被强杀留下的"，而不是一句干巴巴的"文件变了"。
    # 两种残留都要拦住——被强杀的残留和手工试出来的残留，危害一样。
    resnapshot = "--resnapshot" in sys.argv
    manifest = load_manifest()
    if manifest is None:
        print("还没有快照。这一次会把当前内容记成原始状态，之后的判断都以它为准。")
        print("（所以第一次跑之前，先确认工作区是自己想要的样子。）")
    else:
        dirty = dirty_against_baseline()
        if dirty and not resnapshot:
            if RUNNING.exists():
                print("上一轮**没有正常结束**（RUNNING 标记还在），文件可能停在突变状态：")
            else:
                print("现场与快照不一致，文件可能停在上一次突变状态：")
            for line in dirty:
                print(line)
            print("\n先看清楚，再二选一：")
            print("  还原成快照里的样子：   python scripts/falsify_frontend.py --restore")
            print("  接受现状并重新建快照： python scripts/falsify_frontend.py --resnapshot")
            print("（不还原就这么跑下去，快照会被污染成「已经坏掉」的版本，")
            print("  之后所有「已还原：True」都会是假的。）")
            return 1
        if resnapshot:
            print("--resnapshot：接受当前文件内容作为新的原始状态。")
            RUNNING.unlink(missing_ok=True)

    print("基线（未改动）")
    base = run_gate()
    print(f"  tsc {'绿' if base['tsc_ok'] else '红'}，{base['passed']} passed, {base['failed']} failed")
    if not base["tsc_ok"]:
        print("  tsc 基线就是红的——先修好再谈反证。")
        print("  " + base["tsc_out"].strip()[:600].replace("\n", "\n  "))
        return 1
    if not base["vitest_ok"]:
        # 仪器坏了就不能用它量东西：这时"0 failed"只说明一条都没跑。
        print("  vitest 自己没跑成功（收集阶段就崩了），所以下面的数字不算数：")
        for name in base["broke"][:5]:
            print(f"    {name}")
        print("  先修好测试再来反证。")
        return 1
    if base["failed"] or base["passed"] == 0:
        print("  vitest 基线不是全绿（或一条都没跑）——先修好再谈反证。")
        for name in base["names"][:10]:
            print(f"    {name}")
        return 1

    # 快照建立在这一刻：门禁刚证明过这棵树是自洽的，所以它不会把
    # 一株已经坏掉的树记成"原始状态"。只在**开跑前**写一次，
    # 跑到一半被强杀时，这份快照就是还原的依据。
    originals = snapshot_baseline(targets)
    RUNNING.write_text("这一轮还没结束。看到它就说明上一轮被强杀了。\n", encoding="utf-8")

    # 锚点预检：开跑之前把"锚点找不到"全部问出来。
    #
    # 放在这里而不是更早，是因为**判据要与突变循环用的同一份文本**——
    # 那用的是 `originals`（快照），不是磁盘上的现场。现场脏的时候两者不同，
    # 拿现场做预检会报出一批假失败。
    #
    # 为什么值得单独做一遍：一轮要跑一个多钟头，而锚点失效是**唯一一种
    # 开跑前就能知道**的失败——它只取决于源码文本，与测试跑得怎么样
    # 毫无关系。不预检的话，一条被重构带走的锚点要等一小时之后才在汇总里
    # 露面，而那一轮里它**什么都没验证**（47 条里藏了 3 条那次就是这样）。
    dead_anchors = [
        (name, rel, original.count(old))
        for name, rel, old, _, _, _ in MUTATIONS
        if (original := originals.get((FRONTEND / rel).resolve(), "")).count(old) != 1
    ]
    if dead_anchors:
        print("\n锚点预检没过——这几条突变现在**什么都验证不了**：")
        for name, rel, count in dead_anchors:
            why = "锚点在代码里找不到了（被重构带走了）" if count == 0 else f"锚点命中 {count} 次"
            print(f"  [{why}] {rel}\n      <- {name}")
        print("修法是把锚点改成现在的代码，不是删掉这条突变。")
        RUNNING.unlink(missing_ok=True)
        return 1

    verdicts: list[str] = []
    broken: list[str] = []
    dead: list[str] = []
    #: 测试**没抓住**的突变。三种失败里唯一一种"工具没问题、守卫有问题"的，
    #: 所以它必须让退出码变红（见下面 `if missed:` 与 `return`）。
    missed: list[str] = []
    try:
        for name, rel, old, new, kind, witness in MUTATIONS:
            path = (FRONTEND / rel).resolve()
            original = originals[path]
            count = original.count(old)
            if count != 1:
                # 0 与「>1」要采取的行动**完全不同**，所以话要分开说：
                # 0 是"这段代码被重构过，突变已经停在旧写法上"（去改锚点），
                # >1 是"锚点太宽，替换不定向"（去把锚点写窄）。
                # 混成一句"出现了 N 次"，0 次会被读成"出现过、只是多次"。
                why = (
                    "锚点在代码里找不到了——这段代码被改过，"
                    "这条突变**什么都没验证**"
                    if count == 0
                    else f"锚点在 {rel} 里出现了 {count} 次，无法定向替换"
                )
                print(f"\n{name}\n  !! {why}")
                verdicts.append(f"  锚点失效  {name}")
                dead.append(name)
                continue

            path.write_text(original.replace(old, new), encoding="utf-8")
            gate = run_gate()

            if kind == "tsc":
                caught = not gate["tsc_ok"]
                detail = "tsc 报错" if caught else "tsc 竟然通过了"
            else:
                caught = any(witness in candidate for candidate in gate["names"])
                detail = f"证人 {witness!r}"
                if not gate["tsc_ok"]:
                    detail += "（tsc 也红了——确认这是预期的副作用，不是锚点太宽）"
                if not gate["vitest_ok"]:
                    # 这一条**必须**单独说。否则"收集崩了"会被读成
                    # "这条突变没被抓住"，而真相是证人没有机会出场——
                    # 两者要采取的行动完全不同（一个去补测试，一个去修现场）。
                    why = (
                        "收集失败：" + "、".join(pathlib.Path(n).name for n in gate["broke"][:3])
                        if gate["broke"]
                        else f"一条用例都没跑出来（passed={gate['passed']}）"
                    )
                    detail += (
                        f"！！ vitest 自身没成功（{why}）——"
                        "这不是证人失败，是门禁自己崩了，别当成'漏掉'去补测试。"
                    )

            mark = "抓住" if caught else "**漏掉**"
            tsc_note = "tsc 绿" if gate["tsc_ok"] else "tsc 红"
            print(f"\n{name}\n  {rel}  [{tsc_note}]")
            print(f"  {gate['passed']} passed, {gate['failed']} failed -> {mark}（{detail}）")
            if gate["names"]:
                shown = "、".join(gate["names"][:5])
                more = f" 等 {len(gate['names'])} 条" if len(gate["names"]) > 5 else ""
                print(f"  失败的用例：{shown}{more}")
            tag = ""
            if kind != "tsc" and not gate["vitest_ok"]:
                # 仪器崩了**不算漏掉**——证人都没机会出场，补测试是白补。
                tag = "  ← 门禁自己崩了，这条结论不算数"
                broken.append(name)
            elif not caught:
                # ============ 这一支以前什么都不做 ============
                # 它只影响打印出来的那一行汇总（`**漏掉**`），
                # 而退出码是 `not dirty and not broken and not dead`——
                # **"测试没抓住"是最该红的一种失败，却不在那个判断里。**
                # 于是一条守卫真的没了的时候，脚本退出 0：跑 CI 的人看到的是
                # "反证通过"。这是这个工具存在以来最该被抓住的一次漏。
                # `kind == "tsc"` 时仪器就是 tsc 自己，`caught` 已经是它的结论。
                missed.append(name)
            verdicts.append(f"  {mark:8s} {name}{tag}")

            path.write_text(original, encoding="utf-8")
    finally:
        for path, original in originals.items():
            path.write_text(original, encoding="utf-8")
        # 还原做完才删标记：反过来的话，这两步之间被强杀就会留下
        # 一株变异的树和一个"我跑完了"的标记。
        RUNNING.unlink(missing_ok=True)

    print("\n" + "=" * 78)
    print("汇总")
    for line in verdicts:
        print(line)

    dirty = [
        label(path)
        for path, original in originals.items()
        if path.read_text(encoding="utf-8") != original
    ]
    print(f"\n已还原：{not dirty}" + (f"（没还原的：{dirty}）" if dirty else ""))
    if missed:
        # 三种失败里**只有这一种是在说"守卫没了"**：锚点失效是工具的问题，
        # 门禁崩了是现场的问题，而这个是"这段代码被改坏了，测试却全绿"。
        # 所以它单列一段，并且和另外两种一样让退出码变红。
        print(f"有 {len(missed)} 条突变**测试没抓住**——这是真的漏，不是仪器的问题：")
        for name in missed:
            print(f"  {name}")
        print("  两种修法，选哪种都要写下来：补一条能抓住它的用例；")
        print("  或者删掉这条突变，并在注释里写明它为什么证伪不了。")
    if broken:
        # 仪器崩过就不能报"全绿"：这几条的判定本身不成立。
        # 注意**不**把它们算进"漏掉"——漏掉的意思是"测试没抓住"，
        # 而这里测试根本没跑起来。
        print(f"有 {len(broken)} 条的判定不成立（vitest 收集失败，不是证人失败）：")
        for name in broken:
            print(f"  {name}")
    if dead:
        # 锚点失效是三种失败里**最安静**的一种，所以它必须让退出码变红。
        # 它既不是"测试没抓住"也不是"仪器崩了"：突变被改写的那段代码
        # 早已重构，这条断言**已经不再对应任何一行代码**，而它在汇总里
        # 长得和"抓住"一样靠前，只有这一行字不同。
        #
        # 这个洞是真发生过的：`clearScheduled()` 与 `isSettled()` 两次重构
        # 一次带走了三条锚点（F13/F16/F17），而那一轮**退出码是 0**，
        # 47 条里实际只有 44 条在验证。修法是把锚点改成现在的代码，
        # **不是删掉这条突变**——删掉等于把"这里没人守"变成"这里看不出没人守"。
        print(f"有 {len(dead)} 条的锚点失效——这几条**什么都没验证**：")
        for name in dead:
            print(f"  {name}")
        print("  把锚点改成现在的代码；不要删掉这些突变。")
    # 四种情况全都要红。`missed` 是最后补进来的那一项——
    # 在它之前，"测试没抓住"是**唯一一种退出码为 0 的失败**，
    # 而它恰好是四种里最严重的一种（问题记录 53）。
    return 0 if not dirty and not broken and not dead and not missed else 1


if __name__ == "__main__":
    sys.exit(main())
