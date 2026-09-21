# HTTP API

**21 条路由**：GET 16、POST 5。没有 PUT / PATCH / DELETE。

后端默认跑在 `127.0.0.1:8020`。前端通过 vite 代理把 `/api` 与 `/health`
转发过来（[`frontend/vite.config.ts`](../frontend/vite.config.ts)），
所以浏览器侧不需要配置后端地址。

字段命名**对外的部分一律 camelCase**（`taskId` / `evidenceCount`），
数据库列是 snake_case，两者在仓库层与路由层之间转换。
这样做是为了和 TypeScript 侧的类型定义逐字对齐——前端不需要维护一张
"哪个接口用哪种命名"的表。

约定：响应里的 `total` 是**筛选后的总数**，不是这一页的条数。

---

## 健康与能力

### `GET /health`

```json
{ "status": "ok", "version": "0.1.0", "llm_provider": "deepseek", "llm_configured": true, ... }
```

**不做任何外部调用。** 语义必须是廉价且永远可用——它是前端"后端起没起"
的探针，`useAsync` 的失败分支靠它。真实连通性探测在
`/api/providers/health`。

返回体来自 `settings.describe()`，**不含任何密钥**，只有一个
`llm_configured` 布尔值表示"key 配没配"。

### `GET /api/providers`

当前生效的 provider、能力矩阵、以及今日成本。

| 键 | 内容 |
|---|---|
| `active` | `{llm, search, fetch}` 三个名字 |
| `fallback` | 降级 provider 名，没配就是 `null` |
| `cassetteMode` | `off` / `record` / `replay` |
| `providers` | 所有已注册 provider 的 `{kind, name, active, configured, error, baseUrl, models, capabilities, pricing}` |
| `cost` | `cost_summary()` 的结果：`calls`、`totalTokens`、`totalCostUsd`、`cacheHitRate`、`byModel`、`byKind` |

`cost` 那条查询失败时会**吞掉异常并返回全 0**，而不是 500。
理由：成本栏是页面上的一个角落，它挂掉不该让整页打不开。日志里有记录。

### `GET /api/providers/health`

真实连通性探测。`?scope=active|all`（默认 `active`）。

- `active` 探的是**正在服役的那个对象**——如果配了 `LLM_FALLBACK`，
  探的就是包了一层的降级链，而不是链上的主 provider。
- `all` 探所有已注册的。

```json
{ "scope": "active", "ok": true, "cassetteMode": "off",
  "probes": [ { "kind": "llm", "name": "deepseek", "probed": true,
                "ok": true, "detail": "...", "latencyMs": 412, "error": "" } ] }
```

两个刻意的设计：

- `ok` 只统计 `probed: true` 的条目。**没配 key 的 provider 不算失败**——
  它是"没启用"，不是"坏了"。
- 单条探测超时 25 秒。超时返回 `ok: false` 且 `error: "Timeout"`，
  而不是让整个请求挂在那里。探测一个已经死掉的 provider 时，
  这个上限决定了页面多久能给出答案。

---

## 任务

### `GET /api/pipeline/stages`

流水线的 DAG 节点，**唯一真相源是
[`app/core/pipeline/stages.py`](../backend/app/core/pipeline/stages.py)**。

```json
{ "stages": [ { "key": "intake", "label": "...", "description": "...",
                "order": 0, "optional": false, "startProgress": 0.0 } ],
  "terminal": "done" }
```

前端不许硬编码阶段列表。文档里的顺序也有测试盯着
（[`tests/unit/test_docs_order.py`](../backend/tests/unit/test_docs_order.py)），
所以"文档与代码不符"在这条线上是结构上不可能发生的。

### `GET /api/modes`

三档模式的参数。当前档位默认 `deep`。

每项含 `key`、`label`、`description`、`maxBrands`、`maxDimensions`、
`maxSearchCalls`、`reworkSearchCalls`、`maxReworkRounds`、`sectionCount`、
`enableSentiment`、`enableStructured`。

`reworkSearchCalls` **单独报，不并进 `maxSearchCalls`**：
返工额度是一个额外的池子，合并显示会让人以为 quick 档比实际能搜得多。

### `POST /api/tasks` → **201**

```json
{ "query": "对比 Notion 与 Obsidian", "mode": "quick", "autoClarify": false }
```

`query` 1–2000 字符，空白串返回 **422**。`mode` 默认 `deep`。

返回 `runner.snapshot()` 加上 `query` 与 `mode`：

```json
{ "taskId": "TK-...", "status": "awaiting_clarify", "stage": "intake",
  "stageLabel": "...", "progress": 0.05, "reportId": "", "error": "",
  "nodes": { "intake": "done", "orchestrator": "pending" },
  "lastSeq": 12, "elapsedMs": 1830,
  "needClarify": true, "awaitingClarify": true,
  "clarifyQuestions": [ { "question": "...", "options": [...], "recommended": "..." } ],
  "subject": "...", "brands": ["Notion", "Obsidian"],
  "query": "...", "mode": "quick" }
```

**这个接口会等到 intake 阶段跑完才返回**，因为澄清问题必须在那之前
生成出来——否则前端拿到的 `clarifyQuestions` 永远是空的。

两个反直觉但有意的地方：

- intake 失败时返回的**仍然是 201**，body 里 `status: "failed"`、
  `error` 里写明原因。任务确实被创建了，它的失败是一个状态而不是一个
  请求错误。用 4xx/5xx 表达它会让前端把"任务建好了但第一阶段失败"
  和"请求根本没发出去"混成一件事。
- 建任务**会真的开始跑流水线**（intake 那一段）。这不是一个幂等接口。

### `GET /api/tasks`

`?limit=1..200`（默认 50）、`?offset>=0`、`?status=`。

返回 `{total, limit, offset, items[]}`，每项是 `TaskRecord.to_dict()`：
`taskId`、`query`、`mode`、`status`、`stage`、`progress`、`needClarify`、
`clarifyQuestions`、`clarifyAnswers`、`subject`、`brands`、`createdAt`、
`updatedAt`、`error`。

### `GET /api/tasks/{task_id}`

任务的当前快照。**只读**——打开一个页面不等于执行一次任务。

**这个接口有两种响应形状**，这是有意的：

| 情形 | 形状 |
|---|---|
| runner 在内存里，或任务已终态 | `TaskRunner.snapshot()`，含 `elapsedMs` |
| 进程重启过、任务非终态 | `load_task()` 回退，含 `evidenceCount`，`nodes` 恒为 `{}` |

后者是从数据库行重建的，进程内的阶段状态已经没了，所以 `nodes` 只能是
空对象。两种形状的差异写在
[`contracts/task_states.json`](../contracts/task_states.json) 的
`snapshot.optional` 里（`elapsedMs`、`evidenceCount` 是可选键），
两侧各有一条契约测试盯着。

不存在返回 **404**。

### `POST /api/tasks/{task_id}/clarify`

```json
{ "answers": { "q1": "答案", "q2": "选项A、选项B" } }
```

返回 `runner.snapshot()`。多选答案由前端用 `、` 拼成一个字符串——
`clarify_answers` 存的是 `{问题: 字符串}`，不是数组。

- **404** 任务不存在
- **409** 任务存在但状态不是 `awaiting_clarify`

409 的 detail 会把当前状态印出来（`任务当前状态是 running，不在等澄清`），
因为"点提交没反应"和"这个任务已经跑起来了"是两件需要不同处理的事。

进程重启后 runner 不在内存里时，这里会**重新挂载 runner 并重跑一次
intake**，然后才应用答案。

### `GET /api/tasks/{task_id}/stream` —— SSE

事件流。**纯读**，永远不启动流水线。

| 参数 | 说明 |
|---|---|
| `?from_seq=0` | 从第几条之后开始；0 表示全量 |
| `Last-Event-ID` 头 | **优先于 `?from_seq=`** |

帧格式（[`app/core/observability/events.py`](../backend/app/core/observability/events.py)）：

```
id: 37
event: thought
data: {"seq":37,"taskId":"TK-...","type":"thought","createdAt":"...","thought":{...}}

```

`id:` 是事件按任务的 `seq`（从 1 开始），`event:` 是类型，`data:` 是
摊平后的信封。浏览器原生 `EventSource` 会自动把最后收到的 `id` 放进
重连时的 `Last-Event-ID` 头——**整条断线续传链路不需要任何客户端代码**。

**11 种事件类型**：`node_update`、`progress`、`thought`、`message`、
`evidence`、`trace`、`chart`、`image`、`report_ready`、`done`、`error`。
字段级契约在
[`contracts/sse_events.json`](../contracts/sse_events.json)，后端跑一遍
真实 Mock 流水线断言推出去的字节与它一致，前端断言类型定义没有多写
少写一个字段。

响应头：`Cache-Control: no-cache`、`Connection: keep-alive`、
`X-Accel-Buffering: no`。最后一条是 nginx 部署必需的——不加的话事件
会在最后一次性到达，看起来像"流没生效"。

**心跳**：15 秒没有事件时发一个注释帧 `: keep-alive\n\n`。
注释帧不会被 `EventSource` 当事件派发，所以前端看不到它，
但它能让中间的代理不把连接掐掉。

两条不同的服务路径：

1. **runner 在内存里** → 从 `EventJournal` 订阅，带心跳，
   新事件实时推送，一直挂着直到任务结束。
2. **runner 不在（进程重启过）** → 从 `task_events` 表回放历史，
   **没有心跳**，历史推完就关流。

第 2 条是有意的：回放路径是"把已经发生过的事情讲一遍"，
它有个明确的终点，不需要保持连接。

---

## 报告

### `GET /api/reports`

`?limit=1..200`（默认 20）、`?offset>=0`、`?mode=`。
返回 `{items[], total, limit, offset}`。

每项是**摘要**：`reportId`、`taskId`、`query`、`subject`、`brands`、
`mode`、`generatedAt`、`metrics`、`quality{passed,publishable,blockers}`、
`evidenceCount`、`degradedCount`。

**刻意不含 `data`**：quick 档一份报告的 `data` 有几 MB，列表页要 20 份。
这不是优化，是"列表页能不能打开"的问题。

### `GET /api/reports/{report_id}`

```json
{ "reportId": "...", "taskId": "...", "data": { ... },
  "feedback": [ { "feedbackId": "...", "sectionKey": "...", "kind": "annotation",
                  "content": "...", "author": "local", "createdAt": "..." } ],
  "feedbackCount": 3 }
```

`data` 是整份报告正文（章节、图表、矩阵、定价、画像、质量报告、
审计发现、术语表、图集、队伍…）。字段定义见
[`frontend/src/types/report.ts`](../frontend/src/types/report.ts)。

404 时 detail 是 `报告 {id} 不存在`。

> **没有** `GET /api/reports/{id}/trace`。轨迹是**按任务**的
> （`/api/tasks/{id}/trace`），加一个按报告的别名只会让两份数据有
> 两个入口。这个决定记在 `contracts/sse_events.json` 里。

### `POST /api/reports/{report_id}/feedback`

```json
{ "content": "这一节的定价数据是去年的", "sectionKey": "pricing", "kind": "annotation" }
```

`content` 1–4000 字符。`sectionKey` 空串表示针对整份报告。
`kind` 的约定取值：`annotation` / `refine` / `rating`。

返回 `{feedbackId, reportId, count, correctionRate}`。

**只增不改**，没有 UPDATE 路径。`correctionRate` 是"有过批注的报告数
÷ 报告总数"，每次提交都返回最新值，所以前端不需要再拉一次仪表盘。

### `POST /api/reports/{report_id}/refine`

"按批注深化本节"。

```json
{ "annotation": "缺少 2025 年的数据", "sectionKey": "pricing", "search": true }
```

`search: true` 表示允许为这次重写补采证据。

返回：`{reportId, section, sectionKey, addedEvidences, phantomCitations,
degraded, refinement, completeness, quality, feedbackCount, correctionRate}`。

- **404** 报告不存在，或 `sectionKey` 不在报告里（detail 是 `报告里没有章节 X`）
- **422** 报告没有章节（数据问题，不是请求问题）

**这个接口会重写整份报告**（一次 UPDATE，不是改一个字段），并在
`report_feedback` 里记一条 `kind="refine"`。前端的做法是拿返回值里的
`section` 就地替换那一节，而不是重新拉整份报告。

`phantomCitations` 是这次重写里被丢弃的、模型编造出来的引用编号个数——
它不是"出错"，是幻觉抑制在工作的计数。

### `GET /api/reports/{report_id}/export`

`?format=md|markdown|json`（默认 `md`）、`?include_full_text=false`。

返回 `text/plain; charset=utf-8`，**不是** `text/markdown`——
后者会让浏览器直接下载文件，而演示时更希望它显示出来。
文件名走 RFC 6266 的 `filename*=UTF-8''...`，中文标题不会乱码。

---

## 证据（知识库）

### `GET /api/evidences`

| 参数 | 说明 |
|---|---|
| `?brand=` | 按品牌 |
| `?sourceType=` | 按来源类型 |
| `?minCred=` | 可信度下限，0–100 |
| `?domain=` | 按域名 |
| `?q=` | 标题/摘要模糊匹配 |
| `?limit=1..200`（默认 40）、`?offset=` | |

**一行 = 一个去重后的来源**，不是一条证据记录。同一篇文章被 9 次调研
引用过就是一行，`taskCount: 9`。

每项：`evidenceId`、`url`、`title`、`snippet`、`brand`、`sourceType`、
`siteName`、`publishedAt`、`capturedAt`、`credibility`、`degraded`、
`taskCount`。**不含 `fullText`**——列表页不需要正文，而正文是这一行里
最大的字段。

`total` 是去重后的来源数（`COUNT(DISTINCT evidence_id)`）。

> **没有** `GET /api/evidences/{id}` 详情路由，因为 `evidences` 表没有
> 全局唯一的 id——主键是 `(task_id, evidence_id)`，同一个
> `evidence_id` 可以在多个任务里各有一行，且各行的维度、品牌、
> 交叉印证分都不同。"这条证据"单独拿出来是一个不完整的概念。

### `GET /api/evidences/facets`

```json
{ "total": 556, "mentions": 3178,
  "bySourceType": [ { "value": "news", "count": 120 } ],
  "byBrand": [ { "value": "Notion", "count": 84 } ] }
```

`total` 是去重后的来源数，`mentions` 是**行数**（累计被引用次数）。
两个口径都要有：知识库页头写的是"`total` 条来源（去重后）· 累计被引用
`mentions` 次"。

分面与列表**必须同一个口径**，否则会出现"选项里写着 24 条、
点进去只有 3 条"——那种不一致看起来像 bug，实际是两处各算各的。
所以 `library_facets()` 和 `count_library()` 走的是同一套过滤条件。

---

## 专家

### `GET /api/experts`

`?level=L1|L2|L3`、`?group=`。返回 `{items[], total, rosterSize, byLevel[], byGroup[]}`。

- `rosterSize` 是**名册总人数**（48），不受过滤影响；`total` 是筛选后的。
  页头写"48 位专家"用的是前者——用 `total` 的话筛选之后页头会变成
  "12 位专家"。
- 排序：层级 → 分组 → 参与度降序 → id 升序。**同分时按 id**，
  否则每次刷新名册的顺序都在跳。
- 过滤在服务端做，不在前端：48 人一次全发也就几十 KB，但前端自己筛
  意味着筛选逻辑有两份，而多一份就会漂。
- `participation` 来自 `report_team` 关联表，是**真实数出来的**。

名册来自 `experts.json`（代码资产，由 `gen_experts.py` 生成），
**不从数据库来**。放进库里就要面对"库里的名册和仓库里的名册不一致"，
而这个不一致没有任何东西会报错。

### `GET /api/experts/{expert_id}`

```json
{ "expertId": "L3-001", "...": "...", "participation": 4,
  "reports": [ { "reportId": "...", "taskId": "...", "query": "...",
                 "subject": "...", "generatedAt": "..." } ] }
```

带上了这个人参与过的报告——名册页最常问的下一句是"他都参与过哪几次"。

`reports` 里**一个报告正文字段都没有**：走的是 `reports_of_expert()`
（`report_team` 与 `reports` 的连接），不是"拉 200 份报告再自己过滤"。
后者为了回答这个问题要读 200 × 172 KB 的正文，而这里一个字段都不需要。

`reports` 最多 200 条。超过时前端会显示一行提示，而不是让人以为
"就这么多"。

404 时 detail 是 `名册里没有专家 {id}`。

---

## 仪表盘

### `GET /api/dashboard`

四组数字：`coverage`、`runs`、`byMode[]`、`correction`，加 `recent[]`
（最近 10 份）。

几个口径上的选择：

- `sources` 是**去重后的来源数**（`count_library()`），`mentions` 是
  **行数**（`count_rows()`）。这两个数在同一个页面上，必须各自说明口径，
  否则"覆盖 556 个来源"和"3178 条证据"看起来像矛盾。
- `crossValidationRate` 是**加权比率**（`Σ 交叉验证过的论点 ÷ Σ 论点`），
  不是"每份报告的交叉验证率的平均"。后者会让只写了一句话的报告
  和写了 30 条论点的报告权重相同。
- `passed` 与 `publishable` 是两个不同的门槛，页面上分开显示。
  `passed > publishable` 时页面会解释一句为什么——这个差值是正常的，
  不是 bug。

仪表盘的每个数字在界面上都带一个 `hint`（公式说明）。一个没有出处的
数字不如不显示。
