# 决策记录（DECISIONS）

这份文档记的是**做过的取舍**，不是功能清单。每一条回答三个问题：
做了什么决定、**凭什么**、代价是什么。

判定标准：一条决策值得写进来，当且仅当**存在一个合理的替代方案**，
而且当时选它的理由不是"更省事"。凡是"只有一种写法"的事不记。

每条都带**可复核的证据**——一条 SQL、一个文件位置、一次实测。面试里被追问
"你怎么知道"时，能当场跑出来给人看的才算数。

> 记号：**[修正]** = 有意与参考实现（Verda）不同的地方；
> **[沿用]** = 参考实现这么做是对的，照做；**[自有]** = 参考实现里没有的东西。
> 这个分工是为了回答"哪些是你自己想的"——把继承来的说成原创是求职里最贵的失误。

---

## 一、接口与命名

### D1. trace 汇总里不叫 `durationMs`，拆成三个名字

**[修正]** 参考实现的汇总里有一个 `durationMs`，值是**所有 span 耗时相加**。

实测 `TK-1d1ff1f7090b`：相加 **788.6 秒**，墙钟 **183.2 秒**，比值 **4.30**。
那 788 秒从没在墙上发生过——采集阶段是并发扇出的（118 search + 100 fetch 并行）。

**决定的不是数字，是名字。** 数字没错，错的是它挂在一个会被读成
"这次任务花了多久"的名字下面。任务耗时只有一个出处：`done.metrics.durationMs`。

| 字段 | 含义 |
|---|---|
| `spanDurationMs` | 所有 span 耗时**相加**（总占用） |
| `spanWindowMs` | 最早开始 → 最晚结束的墙钟窗口，**也不是任务耗时** |
| `concurrency` | 前两者相除。≈1 串行链，4.30 是同时开了四路 |

**代价**：多两个字段，前端要理解三个数而不是一个。接受这个代价是因为
**"变慢了"和"变串行了"是两种病**，只看端到端耗时它们长得一样。

证据：`backend/app/api/routes_tasks.py`（`_summarize_spans`、`_span_window_ms`）、
`backend/tests/integration/test_task_trace_api.py::TestDurationAccounting`
（`_summarize_spans` 的单测与常量一并复核）。

### D2. 失败与降级分开数，不合成一个 `failedCount`

**[修正]** 参考实现的定义是 `status not in ("", "ok")`，把 `error` 和 `degraded`
合成一个桶。而 span 的 `status` 只有三个取值。

实测全库 1036 行：`ok` 997、`degraded` 39、`error` **0**。

于是那份 249 条 span 的报告会印出"**24 次失败**"，真相是
"24 个页面抓到了但正文太薄"——那是网页采集的**正常形态**，不是故障。
读者会由此认为这个系统不稳。而这一切不报错、不崩，只是误导。

`Tracer.metrics()` 一直是分开数的，所以这其实是**同一个库里两套口径**。

**代价**：前端要显示两个数，且"降级"这个词得解释一次。值得。

证据：`errorCount` / `degradedCount` 两个字段；
`test_task_trace_api.py::TestStatusCounting`。

### D3. span 树接口保留**树**的形状，即使当前数据是一片森林

**[修正]** 实测：真库 1036 行里 `parent_id != ''` 的 **0 行**；
全仓库**没有一处 `with span(...)` 嵌在另一处里面**（静态扫过，0 处）；
mock 流水线一次跑出 49 条 span，`parentId` 全是空串。

所以 `span_tree()` 在当前数据上是个**恒等函数**，`children` 永远是 `[]`。

**替代方案**：既然现在全是根，不如把接口改成扁平数组，简单直白。

**没这么做**，理由：扁平数组是从数据里**删信息**——将来真出现嵌套
（并发子任务、返工子流程都是自然的嵌套来源）时，前端得重写。
而保留树形状的代价只是多一层 `children: []`。

**同时**必须承认代价：这段代码**当前没有被真实数据走到**。
它的正确性完全靠构造数据的测试守着——见 `问题记录.md` 33.4，第一版
测试是假绿的（"真数据跑通"不等于"这段代码被走到了"）。

证据：`backend/tests/integration/test_task_trace_api.py::TestSpanTree`
里三条构造嵌套的用例（根→子→孙、父不在库里的孤儿、自己当自己的父亲）。

### D4. 不建 `GET /api/reports/{id}/trace`

**[自有]** 参考实现有报告级的 trace 路由。xm3 **有意不做**。

理由：报告详情 `GET /api/reports/{id}` 已经返回 `taskId`，
而 trace 的唯一键是 `taskId`。再开一条报告级的路由，就是**同一个资源两条取法**——
两条路会各自演化，然后某一天给出不一样的结果（比如一条带了报告级过滤、
另一条没带）。这个不对称在代码里看不出来，只会表现为"从报告页点进去
和从任务页点进去看到的 span 数不一样"。

前端因此用 `/trace/:taskId`，可选带 `?report=<reportId>` 用来做返回链接——
**参数只影响导航，不影响取数**。

**代价**：从报告页进 trace 要多一跳（先拿 `taskId`）。这一跳本来就在，
因为报告详情已经加载完了。

证据：`backend/app/api/routes_reports.py` 只有
`""` / `/{report_id}` / `/{report_id}/feedback` / `/{report_id}/refine` / `/{report_id}/export`
五个路由，无 trace。

---

## 二、决策回放怎么驱动

阶段 8 的回放页要在动手前先定"滑杆走什么"。三个候选：
证据的捕获时间、span 的开始时间、span 的结束时间。

### D5. 滑杆走 **span**，不走证据捕获时间

**[自有]** 关键实测：`evidences.captured_at` **是按采集轮次盖的章，不是逐条记的**。

| 任务 | 不同取值 | 分布 |
|---|---|---|
| `TK-1d1ff1f7090b` | **2 个** | 14:48:42 ×84、14:49:42 ×24 |
| `TK-aea39ac70af5` | **2 个** | 21:09:55 ×188、21:11:38 ×24 |

一个任务里这个字段只有 2 个不同值。而 249 条 span 的时间戳是毫秒级的
（14:47:26.874 → 14:50:30.096）。

**用捕获时间当时间轴会怎样**：滑杆走过 45% 的行程面板都是空的，
然后一次性弹出 84 条。那不是回放，是三段跳。

而且盖章发生在**轮次的开头**，不是结尾：

```
[116] search  14:48:41.850  Nuclino 评价
[117] fetch   14:48:42.466  https://www.informat.cn/qa/169310   ← 第一条 fetch 开始
       证据 capturedAt = 14:48:42                            ← 早了 0.47 秒
```

盖章时第一条 fetch **还没开始**。所以它连"这一轮抓到的证据到手的时刻"都不是，
它标的是"这一轮开始的时刻"，且整轮共用。

**代价**：回放不能声称"这条证据是第 137 步拿到的"——数据里没有这个答案。
页面必须说清楚它给的是"跟着当前 span 高亮/过滤"，否则用户会以为滑杆坏了。

证据：`frontend/src/lib/replay.ts` 顶部注释；`问题记录.md` 问题 34.1。

### D6. span ↔ 证据按**域名**连，不按引用、不按时间

**[自有]** 先看数据里有没有答案：`span.detail` 里**没有任何 evidence id**。

| span kind | `detail` 里有什么 |
|---|---|
| `search` | `{sites, hits}` |
| `fetch` | `{domain, chars, boilerplate, degraded}` |
| `llm` | `{tier}` |

所以"这一条 span 产出的是哪几条证据"**在数据里不存在**。

**替代方案**：按时间窗口近似（span 结束时刻前后 N 秒内的证据）。

**没这么做**：捕获时间是按轮次盖章的（D5），时间窗口会退化成
"整轮的证据都算给这一轮的每一条 fetch"，也就是没有信息量。

**实际用的是域名**：`fetch` span 的 `detail.domain` 与证据 URL 的主机名实测
**100/100 命中**（该任务 100 条 fetch span、63 个主机、108 条证据）。

**代价**：域名匹配是**近似**——同一主机的不同页面分不开，
一条 span 会连上该主机下的所有证据。这个代价写进页面文案，
不假装它是引用级联动。**引用级联动落在报告那一侧**（`citations` 有真正的 id 对应），
不在这条时间线上。

证据：`frontend/src/lib/replay.ts` 的 `spanHosts` / `evidenceHost`；
`spanHosts` 有一条测试专门断言 **`search` 的 `sites` 不算数**
（那是请求时指定的站点过滤，不是"抓到了哪些站"）。

### D7. `at` 取 `endedAt`，且排完序重新发号

**[自有]** 滑杆停在某一步时，读者要看到的是"这一步**已经发生**"——
而一个正在跑的 span 还没有结果。所以 `at` 用结束时刻。

时间戳解析不出来的记 0 而**不是丢掉这一条**：丢一条会让步数和
`summary.spanCount` 对不上，而页面上看不出来。

这会带来一个后果：坏数据把 `at` 压到起点，于是 `at` 不再单调。
所以排完序要**重新发号**（`index` 从 0 连续），否则 `index` 与位置不一致，
滑杆的取值会指向错的 span。

**代价**：`ReplayStep.index` 在构建过程中被改写一次，
函数不再是纯 `map`。用一条"`at` 单调不减"的测试钉住。

证据：`replay.ts::buildTimeline`；测试
`frontend/src/lib/replay.test.ts` 的 `at 单调不减` 与
`时间戳坏掉的行**不丢**，步数等于 span 数`。

---

## 三、沿用参考实现的架构（**[沿用]**）

这些是读懂 Verda 之后**判断它做对了**、照着做的。写进来是为了把
"我继承了什么"和"我改了什么"分开——面试里被问"哪些是你自己想的"时，
这个分界线要能当场指出来。

| 决策 | 为什么它是对的 | xm3 的落点 |
|---|---|---|
| **三档模式**（quick/deep/expert）配置化 | 成本与深度的取舍应该是一个参数，不是三份代码 | `core/modes.py` |
| **三层专家分工**（3 决策 / 9 战略 / 36 执行） | 名册是数据不是代码，可生成、可测 | `data/experts_seed.yaml` + `gen_experts.py` |
| **SSE 而非 WebSocket** | 事件是单向的，且标准 SSE 语义让浏览器**原生**重连就能工作 | `routes_tasks.py::stream` |
| **四条铁律**（引用强制、交叉验证、返工、降级可见） | 把"不许编"变成结构约束，而不是提示词里的一句请求 | `core/models.py`、`audit.py` |
| **contextvars 无侵入埋点** | 打点不污染业务签名；异步扇出时自动跟着走 | `core/observability/trace.py` |
| **整份报告 JSON 存一列** | 一次访问读一份报告，读放大无所谓；拆成 12 张表只换来 JOIN 风暴 | `db/migrations.py` |

**已知代价**（写在这里而不是藏起来）：

- 整份 JSON 存一列 → **无法用 SQL 按报告内容筛选**（比如"找出所有
  提到某个品牌的报告"只能用 `metrics` 里冗余出来的标量列）。
  这个代价在数据量到十万份报告时会开始疼。
- contextvars 埋点 → span 只在**同一个上下文**里可见，
  所以 `asyncio.to_thread` 扇出时要显式传递；实测当前**没有嵌套 span**
  （见 D3），这个限制还没被真实压到。

---

## 四、与参考实现有意不同的地方（**[修正]**）

每一条都是"读懂它之后判断这里会出事"。详细的现象与定位过程在
`问题记录.md`，这里只留决策本身。

### D8. `TaskRunner` + `ensure_runner()` 防重复订阅重跑

参考实现的 `/api/tasks/{id}/stream` 在请求处理器里**直接调** `run_pipeline`，
没有注册表。而 `EventSource` 会**自动重连**，开第二个标签页也会再连一次——
两种情况都会对同一个 `task_id` **再跑一遍流水线**，重复写证据、trace、专家统计。

xm3 用 `ensure_runner()` 拿一个 per-task 的单例，第二次订阅**接上**已有的 runner。

证据：`core/pipeline/runner.py::TaskRunner`（98 行）、`ensure_runner`（596 行）。

### D9. 事件 seq **按任务**，不是全局计数器

参考实现用一个模块级 `_SEQ`。两个并发任务的序号会交错，
而**决策回放的滑杆恰恰依赖序号排序**——交错的序号会让滑杆跳。

xm3 每股任务流独立发号，`seq` 从 1 起、在该任务内连续。

证据：`core/observability/trace.py` 顶部注释写明了这条取舍。

### D10. `task_events` 表 + journal 补发 `Last-Event-ID`

参考实现没有 `id:` 帧、不处理 `Last-Event-ID`、没有事件日志——
跑到一半刷新页面状态全丢。

xm3：每帧带 `id: <seq>`；服务端按 `Last-Event-ID` 补发。
**优先级是 `Last-Event-ID` 头高于 `?from_seq=`**，因为自动重连时 URL 还是
原来那个（可能带着 `from_seq=0`），若查询参数优先则续传会从头再来。

证据：`routes_tasks.py::stream`（416 行 `Last-Event-ID` 头）、`_resume_from`（459 行）。

### D11. `report_feedback` 用代理主键 `feedback_id`

参考实现用 `report_id` 做主键，每次提交**覆盖**上一次——
于是「人工修正率」永远只能看快照、看不出趋势。而这个指标的意义恰恰是趋势。

证据：`db/migrations.py::report_feedback`（`feedback_id TEXT PRIMARY KEY`）。

### D12. `save(report)` 从报告对象内部取 `task_id`，不接受参数

参考实现是 `save_report(rep, task_id="")`，用 `INSERT OR REPLACE`——
默认参数一旦漏传，**关联被静默抹掉**。

xm3 的 `save(report: ReportRecord)` 只收一个参数，且 `task_id` 为空时
**直接抛 `ValueError`**。让它在结构上不可能发生。

证据：`db/repo/reports.py::save`（44 行）、`raise ValueError`（53 行）。

### D13. 相关性过滤移出适配器，放进 `core/evidence/relevance.py`

参考实现把它藏在搜索适配器里。**换搜索源时它就会静默消失**——
而"静默消失"正是适配层本来要防的事。

证据：`core/evidence/relevance.py` 的位置本身。

### D14. 类型化 `ProviderError`，不靠字符串匹配状态码

参考实现用 `"429" in str(err)` 判断限流。差别不在能否工作，
而在于「我们处理了 429」和「我们有一层重试抽象」是两件事。

证据：`providers/errors.py` 里 9 个类型（`AuthFailed` / `QuotaExhausted` /
`RateLimited(retry_after)` / `Transient` / `MalformedResponse` / …）。

### D15. 不要那四个没有任何引用的依赖

**[修正]** 实测参考实现的 `requirements.txt` 里钉了四个版本、
而代码中引用 **0 处**：`langgraph==0.2.60`、`langchain-core==0.3.28`、
`matplotlib==3.9.4`、`networkx==3.2.1`。

xm3 的运行依赖 **11 个**，每一个在 `pyproject.toml` 里都有注释说明为什么在。
知识图谱用前端 d3，所以后端不需要 `networkx`；图表用 ECharts，不需要 `matplotlib`。

**代价**：真需要图谱算法（中心性、社区发现）时要自己写或再加依赖。
当前图谱是 d3-force 布局，不需要。

证据：`backend/pyproject.toml` 的 `dependencies` 列表。

---

## 五、待定（阶段 8–10 会补）

这份文档随阶段推进追加。以下决策**还没做**，先放在这里避免以后忘了记：

- [ ] 知识图谱的节点/边口径（品牌↔维度↔证据↔专家，哪个方向是主视图）
- [ ] `eval/` 的判定阈值定在哪，以及 `--gate` 失败时是阻断还是告警
- [ ] Playwright 冒烟的范围（只截图，还是断言渲染）
- [ ] Locust 的通过标准（p95 首事件时延定多少算过）
