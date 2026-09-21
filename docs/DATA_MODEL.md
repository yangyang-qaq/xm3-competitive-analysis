# 数据模型

SQLite，单文件。版本号记在 `PRAGMA user_version`，迁移列表在
[`backend/app/db/migrations.py`](../backend/app/db/migrations.py)。

当前 `TARGET_VERSION = 4`：**9 张表、14 个索引、4 条迁移**。

---

## 为什么不用 ORM，也不用 Alembic

两个决定都是"够用就好"，但理由不同。

**不用 ORM**（SQLAlchemy 等）：这个项目只有 9 张表，查询里有两处
`json_each` 和一处两段式改写（见下文 `_library_rows`），这些都不是 ORM
擅长的形状。ORM 在这里换来的是"不用写 SQL"，代价是多一层要学的东西、
和一堆"这行 SQL 到底发出去了什么"的疑问。仓库层
（[`app/db/repo/`](../backend/app/db/repo/)）用六个模块把 SQL 收在一处，
界面同样是类型清晰的函数。

**不用 Alembic**：Alembic 解决的是"多人协作 + 多分支 + 分步回滚"。
这个项目一个开发者、一个 SQLite 文件、没有需要滚动升级的生产环境。
引入它意味着多一个依赖、一个 `alembic.ini`、一个 `versions/` 目录，
换来的能力一条都用不上。

取代方案是 `user_version` + 有序迁移表。规则两条：**迁移只增不改**，
**每条迁移自己带 `BEGIN;`/`COMMIT;`**。第二条值得解释：`executescript()`
在执行前会隐式提交挂起的事务，所以靠外层事务保证原子性的话，半路失败
会留下改了一半的库——而 SQLite 的 DDL 大部分可回滚，白白放弃很可惜。
把 `BEGIN;` 写进脚本里，`executescript` 就不会再插一脚。

对照一下参考实现的启动时 `CREATE TABLE IF NOT EXISTS`：那样无法处理
"加一列"，只能 `ALTER TABLE` 硬试并吞掉异常——而吞掉的异常会掩盖真正的
迁移失败。版本号方案让"当前库是第几版"是一个可查询的事实，不是推测。

---

## 表

### `tasks` —— 一次调研

| 列 | 类型 | NOT NULL | 默认 |
|---|---|---|---|
| `task_id` | TEXT | PK | — |
| `query` | TEXT | NOT NULL | — |
| `mode` | TEXT | NOT NULL | `'deep'` |
| `status` | TEXT | NOT NULL | `'pending'` |
| `stage` | TEXT | NOT NULL | `''` |
| `progress` | REAL | NOT NULL | `0` |
| `need_clarify` | INTEGER | NOT NULL | `0` |
| `clarify_questions` | TEXT | NOT NULL | `'[]'` |
| `clarify_answers` | TEXT | NOT NULL | `'{}'` |
| `subject` | TEXT | NOT NULL | `''` |
| `brands` | TEXT | NOT NULL | `'[]'` |
| `error` | TEXT | NOT NULL | `''` |
| `created_at` | TEXT | NOT NULL | — |
| `updated_at` | TEXT | NOT NULL | — |

索引：`idx_tasks_created(created_at DESC)`、`idx_tasks_status(status)`。

`status` 的取值集合是**契约**，写在
[`contracts/task_states.json`](../contracts/task_states.json)：六个
`pending / running / awaiting_clarify / done / failed / cancelled`，
其中后三个是终态，`awaiting_clarify` 是唯一的"停在半路等人"。

启动时有一条 `interrupt_unfinished()`：把所有非终态任务标成 `failed`，
理由写在 `INTERRUPTED_REASON` 里——进程内的流水线不跨重启存活，
重启后那一行如果还写着 `running`，界面会显示一个永远不动的进度条。

### `reports` —— 一份报告

| 列 | 类型 | NOT NULL | 默认 |
|---|---|---|---|
| `report_id` | TEXT | PK | — |
| `task_id` | TEXT | NOT NULL | — |
| `query` | TEXT | NOT NULL | `''` |
| `mode` | TEXT | NOT NULL | `''` |
| `subject` | TEXT | NOT NULL | `''` |
| `brands` | TEXT | NOT NULL | `'[]'` |
| `generated_at` | TEXT | NOT NULL | — |
| `data` | TEXT | NOT NULL | — |
| `metrics` | TEXT | NOT NULL | `'{}'` |
| `quality` | TEXT | NOT NULL | `'{}'` |

索引：`idx_reports_task(task_id)`、`idx_reports_generated(generated_at DESC)`。

**整份报告 JSON 存在 `data` 一列里**，另加需要筛选排序的标量列。
这是本仓库最容易被质疑的一个取舍，代价写在这里：

- 优点：一次访问读一份报告，读放大无所谓。拆成十几张规范化表
  （章节、论点、图表、矩阵、定价…）只会换来 JOIN 风暴，
  而且每加一种结构化块就要加一张表、一条迁移。
- 代价：**无法用 SQL 在报告内部字段上筛选**。想查"所有
  `quality.passed = false` 的报告"只能把 `data` 读出来在 Python 里解析——
  实测平均 172 KB 一份。

这个代价已经真实地付过一次：专家参与度曾经是从 `data.team` 数出来的，
压测时量到 950 ms（233 份报告 / 41 MB 正文）。正确的解法不是加缓存，
而是加一张关联表——这就是迁移 v4，也是为什么现在有
`report_team`。**"用 JSON 一列存"这条路的边界，是量出来的，不是猜的。**

### `evidences` —— 一条证据

| 列 | 类型 | NOT NULL | 默认 |
|---|---|---|---|
| `evidence_id` | TEXT | NOT NULL | — |
| `task_id` | TEXT | NOT NULL | — |
| `report_id` | TEXT | NOT NULL | `''` |
| `url` | TEXT | NOT NULL | — |
| `title` | TEXT | NOT NULL | `''` |
| `snippet` | TEXT | NOT NULL | `''` |
| `full_text` | TEXT | NOT NULL | `''` |
| `brand` | TEXT | NOT NULL | `''` |
| `source_type` | TEXT | NOT NULL | `'unknown'` |
| `site_name` | TEXT | NOT NULL | `''` |
| `published_at` | TEXT | NOT NULL | `''` |
| `captured_at` | TEXT | NOT NULL | `''` |
| `matched_dimensions` | TEXT | NOT NULL | `'[]'` |
| `query` | TEXT | NOT NULL | `''` |
| `provider` | TEXT | NOT NULL | `''` |
| `rank` | INTEGER | NOT NULL | `0` |
| `credibility` | REAL | NOT NULL | `0` |
| `credibility_breakdown` | TEXT | NOT NULL | `'{}'` |
| `degraded` | INTEGER | NOT NULL | `0` |
| `images` | TEXT | NOT NULL | `'[]'` |

**主键是复合的 `(task_id, evidence_id)`**，不是 `evidence_id` 单列。
这一条值得展开：`evidence_id` 由 URL 摘要生成，**跨任务、跨轮次稳定**
（这是有意的，否则同一篇文章在两次调研里是两个东西）。所以同一个 URL
在两个任务里是同一个 `evidence_id`——但它在那两个任务里是**不同的证据**：
命中的维度不同、品牌不同、可信度的交叉印证分不同。单列主键会让第二个
任务把第一个任务的记录**静默覆盖**掉。

索引：`idx_evidences_task`、`idx_evidences_brand`、`idx_evidences_source`、
`idx_evidences_cred(credibility DESC)`，
加上 v3 新增的 `idx_evidences_source_eid(source_type, evidence_id)` 与
`idx_evidences_brand_eid(brand, evidence_id)`。

后两条是 v3 迁移加的，理由是量出来的：知识库筛选面要
`COUNT(DISTINCT evidence_id)`，而 v1 的索引里没有 `evidence_id`，
于是每扫一行都要回表。实测（`loadtest.db`，48,384 行）
`by_source_type 303 ms → 8 ms`、`by_brand 97 ms → 9 ms`。

**"有索引 ≠ 有覆盖索引"**——同一批实验里，另一次"加索引"让一条要整行
（含 `full_text`）的查询从 357 ms 变成 **648 ms**，因为每行都要回表随机读。
两次动作一样，一次快 38 倍、一次慢 1.8 倍，区别只在查询要不要回表。
细节见 [问题记录.md 第 44 条](../问题记录.md)。

`full_text` 是**一等列**，不是挂在对象上的外部属性。参考实现把正文挂在
对象外，`asdict()` 会丢掉它——任何一次经数据库的往返都会丢正文。

### `traces` —— 一次调用

| 列 | 类型 | NOT NULL | 默认 |
|---|---|---|---|
| `span_id` | TEXT | NOT NULL | — |
| `task_id` | TEXT | NOT NULL | — |
| `parent_id` | TEXT | NOT NULL | `''` |
| `kind` | TEXT | NOT NULL | `''` |
| `name` | TEXT | NOT NULL | `''` |
| `purpose` | TEXT | NOT NULL | `''` |
| `provider` | TEXT | NOT NULL | `''` |
| `model` | TEXT | NOT NULL | `''` |
| `started_at` | TEXT | NOT NULL | `''` |
| `ended_at` | TEXT | NOT NULL | `''` |
| `duration_ms` | INTEGER | NOT NULL | `0` |
| `prompt_tokens` | INTEGER | NOT NULL | `0` |
| `completion_tokens` | INTEGER | NOT NULL | `0` |
| `cost_usd` | REAL | NOT NULL | `0` |
| `status` | TEXT | NOT NULL | `'ok'` |
| `error` | TEXT | NOT NULL | `''` |
| `detail` | TEXT | NOT NULL | `'{}'` |
| `cached_prompt_tokens` | INTEGER | NOT NULL | `0` | ← v2 加的 |

主键同样是复合的 `(task_id, span_id)`。理由与证据那条同源但更直接：
span 序号**按任务**从 1 开始编（决策回放的滑杆看到的是"这次任务的第 37 步"），
所以两个任务一定会出现同名 span。单列主键会让后跑的任务覆盖先跑的埋点。

索引：`idx_traces_task(task_id, started_at)`。

`cached_prompt_tokens` 是 v2 加的唯一一列。理由：成本表里只有 `cost_usd`
一个数时，"这份报告很便宜"和"定价表配错了"在界面上长得一模一样。
命中价与未命中价差 50 倍，把命中数一起存下来，成本才是一个可以复核的数。
默认 `0` 是唯一诚实的取值——v1 时期存下的 span 当时没有这个信息，
"不知道命中多少"按未命中计，成本偏高而不是偏低。

### `task_events` —— 事件日志（断线续传的持久层）

| 列 | 类型 | NOT NULL | 默认 |
|---|---|---|---|
| `task_id` | TEXT | NOT NULL | — |
| `seq` | INTEGER | NOT NULL | — |
| `type` | TEXT | NOT NULL | — |
| `data` | TEXT | NOT NULL | `'{}'` |
| `created_at` | TEXT | NOT NULL | — |

主键 `(task_id, seq)`，无二级索引。`seq` 是**按任务**的，所以主键顺带
保证了"同一任务不会有两个 seq 相同的事件"。

这条表是 SSE 断线续传的全部基础：客户端重连时带 `Last-Event-ID`，
服务端从 `seq > 该值` 开始补发。没有它，"跑到一半刷新页面"就丢状态。

### `report_feedback` —— 批注与反馈

| 列 | 类型 | NOT NULL | 默认 |
|---|---|---|---|
| `feedback_id` | TEXT | PK | — |
| `report_id` | TEXT | NOT NULL | — |
| `section_key` | TEXT | NOT NULL | `''` |
| `kind` | TEXT | NOT NULL | `'annotation'` |
| `content` | TEXT | NOT NULL | `''` |
| `author` | TEXT | NOT NULL | `''` |
| `created_at` | TEXT | NOT NULL | — |

索引：`idx_feedback_report(report_id, created_at)`。id 形如
`FB-<12 位 uuid hex>`。

**代理主键 `feedback_id`，不是 `report_id`。** 参考实现用 `report_id`
做主键，每次提交覆盖上一次，于是"人工修正率"永远只能看快照、
看不出趋势。有了代理主键，反馈是**只增不改**的：`add_feedback()`
只做 INSERT，没有 UPDATE 路径。

`correction_rate()` 的口径是"有过批注的报告数 ÷ 报告总数"——
不是"批注条数 ÷ 报告数"，否则一份报告上写十条批注会把修正率抬高十倍。

### `report_team` —— 专家参与度（v4）

| 列 | 类型 | NOT NULL |
|---|---|---|
| `report_id` | TEXT | NOT NULL |
| `expert_id` | TEXT | NOT NULL |

主键 `(report_id, expert_id)`，索引 `idx_report_team_expert(expert_id)`。
**没有 `report_id` 单列索引**，因为按报告查的路径只有 `_sync_team` 的
先删后插，它走的是主键前缀。

这张表是**派生数据**：它的内容完全由 `reports.data` 里的 `team` 字段决定。
之所以要把它物化出来，是因为 `team_usage()` 原先每次请求都要读全部报告
正文再解析，只为数出 48 个人各被派过几次——压测实测 950 ms。

派生数据天然有一致性风险，所以有两条纪律：

1. **唯一的写入口是 `reports_repo.save()`。** 它调 `_sync_team()`，
   而 `_sync_team` 是**先删后插**，不是只插。只插的话，一份报告被重新
   保存（"深化本节"会重写整份报告）时，上一版队伍里那个被换掉的人
   会永远留在表里，参与度只增不减。
2. **`team_ids()` 仍然是队伍成员定义的唯一实现**，`report_team` 必须与它
   一致。这条一致性由测试钉住，包括 v4 的回填结果。

v4 的回填 SQL 里踩过一个坑，值得记在这里：单参数的 `json_type(X)` 和
`json_each(X)` 都会把 X **当成一份 JSON 文档去解析**，X 是普通字符串时
不返回"不是 JSON"而是直接抛 `malformed JSON`。而队伍里到处都是普通
字符串。**这个迁移是启动时跑的**，也就是说一份形状不对的老报告能让
整个应用起不来。最终写法三条：`json_valid(data)` 放进子查询、
第二层参数用 CASE 兜住、判类型一律用 `json_each` 的 `.type` 列。
细节见 [问题记录.md 第 45 条](../问题记录.md)。

### `subscriptions` —— 订阅

| 列 | 类型 | NOT NULL | 默认 |
|---|---|---|---|
| `subscription_id` | TEXT | PK | — |
| `kind` | TEXT | NOT NULL | `'brand'` |
| `target` | TEXT | NOT NULL | — |
| `mode` | TEXT | NOT NULL | `'deep'` |
| `interval_hours` | INTEGER | NOT NULL | `168` |
| `enabled` | INTEGER | NOT NULL | `1` |
| `last_run_at` | TEXT | NOT NULL | `''` |
| `created_at` | TEXT | NOT NULL | — |

唯一索引 `idx_subscriptions_target(kind, target)`——建在
`(kind, target)` 而不是 `target` 上：同一个品牌既可以被"每周品牌动态"
订阅，也可以被"每周定价变化"订阅。

> **这张表目前没有写入方**（实测 0 行），`/api/subscriptions` 也没有实现。
> 见下文"已知缺口"。

### `expert_stats` —— 专家统计

| 列 | 类型 | NOT NULL | 默认 |
|---|---|---|---|
| `expert_id` | TEXT | PK | — |
| `tasks` | INTEGER | NOT NULL | `0` |
| `claims` | INTEGER | NOT NULL | `0` |
| `evidences` | INTEGER | NOT NULL | `0` |
| `reworks` | INTEGER | NOT NULL | `0` |
| `avg_credibility` | REAL | NOT NULL | `0` |
| `last_active_at` | TEXT | NOT NULL | `''` |

无索引。

> **这张表也没有写入方**（实测 0 行）。`/api/experts` 刻意不读它——
> 读一张永远为空的表只会给接口添一个恒为 `None` 的分支。参与度走的是
> `report_team`（真实数出来的），名册自带的初始值走 `stats.source == "seed"`。
> 接口形状已经留好了 `source` 字段来承载"这个数是量出来的还是配置里的"。

---

## 迁移历史

| 版本 | 描述 | 动作 |
|---|---|---|
| 1 | 初始表结构：任务/报告/证据/埋点/事件/批注/订阅/专家统计 | 8 张表 + 11 个索引 |
| 2 | traces 增加 cached_prompt_tokens：缓存命中的输入 token 数 | +1 列 |
| 3 | 知识库筛选面的覆盖索引：让 COUNT(DISTINCT evidence_id) 不必回表 | +2 索引 |
| 4 | report_team 关联表：专家参与度不再靠读全部报告正文数出来 | +1 表 +1 索引 + 回填 |

v3 和 v4 都是**性能问题驱动**的，而且两个数字都是压测当场量出来的，
不是"以后可能会慢"的推测。

---

## 两个反常规的写法

### 一处两段式查询（`_library_rows`）

知识库列表要"每个来源一行、带上它被几个任务引用过"，一条 SQL 写出来是
一个 `GROUP BY` 之后再取整行。实测一条 SQL 版本 338/535 ms，
拆成"先聚合出 id、再取行"两段之后 187/331 ms。

原因和 v3 那个索引实验是同一件事：一条 SQL 版本里，聚合阶段之后还要
为每一行回表读 `full_text`，而 SQLite 选择了对它不利的连接顺序。
拆成两段之后，第二段是一个明确的 `IN (...)` 主键查找。

这里**没有加索引**——加索引那次实验（357 → 648 ms）恰好证明了在这条
查询上加索引会更慢。

### `save()` 是报告的唯写入口

`reports_repo.save(report)` 从 `ReportRecord` 自身取 `task_id`，
不接受调用方传参；缺 `task_id` 直接抛
`ValueError("报告必须有 task_id——它只能来自 ReportRecord 自身")`。

参考实现的 `save_report(rep, task_id="")` 默认参数是空串，配
`INSERT OR REPLACE`，于是调用方漏传时会把报告的关联**静默抹掉**。
把关联做到结构里取，这类缺陷就是不可能发生的，而不是"记得传"。

`save()` 还顺带维护 `report_team`（见上）。**把派生表的维护放在唯一
写入口里，而不是放在各个调用点**，是这条设计能成立的关键。

---

## 已知缺口（诚实清单）

写在这里而不是藏起来，因为面试里被问到"这个表怎么用的"时，
正确答案比含糊其辞好。

| 缺口 | 现状 | 如果要补 |
|---|---|---|
| `subscriptions` 无写入方 | 表已建，0 行，无 `/api/subscriptions` 路由 | 补路由 + 一个定时触发（进程内 APScheduler 或外部 cron） |
| `expert_stats` 无写入方 | 表已建，0 行，接口刻意不读 | 在 `save()` 里顺带聚合写入，像 `report_team` 那样 |
| `evidences.facets()` 无调用方 | 行口径的分面；接口用的是去重口径的 `library_facets()` | 删掉，或明确它的用途（"每个来源被引用几次"这个口径已经被 `library_facets` 覆盖） |
| `Settings.llm_max_retries` 未被读取 | 定义在 `config.py`，全仓库无第二处引用 | 删掉，或接进 `RetryPolicy`（现在重试次数由 `retry.py` 决定） |
| `traces.py` docstring 提到 `/api/dashboard/cost` | 该路由不存在，只有 `/api/providers` 读 `cost_summary` | 改注释 |
| `.env.example` 缺三个配置项 | `CORS_EXTRA_ORIGINS` / `FORCE_MOCK_PROVIDER` / `DB_PATH` 未列 | 补上（`.env.example` 是配置项的文档） |

前四条是同一类：**表/字段建好了，但没人写它**。这类缺口不会报错，
只会让读到 schema 的人以为某个功能存在。这也是为什么这张清单值得有。
