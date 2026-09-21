"""有序迁移。

为什么不用 Alembic
------------------
Alembic 解决的是"多人协作 + 多分支 + 分步回滚"的问题。这个项目只有
一个开发者、一个 SQLite 文件、没有生产环境需要滚动升级。引入 Alembic
意味着多一个依赖、一个 `alembic.ini`、一个 `versions/` 目录，
换来的能力一条都用不上。

这里用 `PRAGMA user_version` 记版本号，加一张有序的迁移表。
规则只有两条：**迁移只增不改**，**每条迁移自己带 BEGIN/COMMIT**。

第二条值得解释：`executescript()` 在执行前会隐式提交掉挂起的事务，
所以如果靠外层的 `transaction()` 保证原子性，半路失败会留下一个
改了一半的库——而 SQLite 的 DDL 大部分是可以回滚的，白白放弃这个能力
很可惜。把 `BEGIN;` 写进脚本里，`executescript` 就不会再插一脚。

参考实现是启动时手工 `CREATE TABLE IF NOT EXISTS`。那样无法处理
"加一列"，只能靠 `ALTER TABLE` 硬试并吞掉异常——而吞掉的异常
会掩盖真正的迁移失败。版本号方案让"当前库是第几版"是一个可查询的事实，
而不是推测出来的。
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    sql: str


# ============================================================
# v1：初始表结构
# ============================================================

_V1 = """
BEGIN;

-- ---------- 任务 ----------
CREATE TABLE tasks (
    task_id           TEXT PRIMARY KEY,
    query             TEXT NOT NULL,
    mode              TEXT NOT NULL DEFAULT 'deep',
    status            TEXT NOT NULL DEFAULT 'pending',
    stage             TEXT NOT NULL DEFAULT '',
    progress          REAL NOT NULL DEFAULT 0,
    need_clarify      INTEGER NOT NULL DEFAULT 0,
    clarify_questions TEXT NOT NULL DEFAULT '[]',
    clarify_answers   TEXT NOT NULL DEFAULT '{}',
    subject           TEXT NOT NULL DEFAULT '',
    brands            TEXT NOT NULL DEFAULT '[]',
    error             TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX idx_tasks_status  ON tasks(status);

-- ---------- 报告 ----------
-- 整份报告 JSON 存 data 一列，另加需要筛选排序的标量列。
-- 一次访问只读一份报告，读放大无所谓；拆成十几张规范化表
-- 只会换来一堆 JOIN。代价（无法用 SQL 直接按报告内部字段筛选）
-- 写进 技术栈.md 的"已知代价"。
CREATE TABLE reports (
    report_id    TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    query        TEXT NOT NULL DEFAULT '',
    mode         TEXT NOT NULL DEFAULT '',
    subject      TEXT NOT NULL DEFAULT '',
    brands       TEXT NOT NULL DEFAULT '[]',
    generated_at TEXT NOT NULL,
    data         TEXT NOT NULL,
    metrics      TEXT NOT NULL DEFAULT '{}',
    quality      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_reports_task      ON reports(task_id);
CREATE INDEX idx_reports_generated ON reports(generated_at DESC);

-- ---------- 证据 ----------
-- full_text 是一等列，不是外部挂在对象上的属性：
-- 参考实现挂在对象外的正文，任何一次 DB 往返就丢了。
-- credibility_breakdown / matched_dimensions 是本项目新增的两列，
-- 分别支撑"可解释评分"与"维度覆盖的真相源"。
-- 主键是 (task_id, evidence_id) **复合主键**，不是 evidence_id 单列。
-- 因为 evidence_id 由 URL 摘要生成（跨任务、跨轮次稳定，这是有意的），
-- 所以同一个 URL 在两个任务里是同一个 evidence_id。但它在那两个任务里
-- 是**不同的证据**：命中的维度不同、品牌不同、可信度的交叉印证分不同。
-- 单列主键会让第二个任务把第一个任务的记录覆盖掉，而覆盖是静默的。
CREATE TABLE evidences (
    evidence_id           TEXT NOT NULL,
    task_id               TEXT NOT NULL,
    report_id             TEXT NOT NULL DEFAULT '',
    url                   TEXT NOT NULL,
    title                 TEXT NOT NULL DEFAULT '',
    snippet               TEXT NOT NULL DEFAULT '',
    full_text             TEXT NOT NULL DEFAULT '',
    brand                 TEXT NOT NULL DEFAULT '',
    source_type           TEXT NOT NULL DEFAULT 'unknown',
    site_name             TEXT NOT NULL DEFAULT '',
    published_at          TEXT NOT NULL DEFAULT '',
    captured_at           TEXT NOT NULL DEFAULT '',
    matched_dimensions    TEXT NOT NULL DEFAULT '[]',
    query                 TEXT NOT NULL DEFAULT '',
    provider              TEXT NOT NULL DEFAULT '',
    rank                  INTEGER NOT NULL DEFAULT 0,
    credibility           REAL NOT NULL DEFAULT 0,
    credibility_breakdown TEXT NOT NULL DEFAULT '{}',
    degraded              INTEGER NOT NULL DEFAULT 0,
    images                TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (task_id, evidence_id)
);
CREATE INDEX idx_evidences_task     ON evidences(task_id);
CREATE INDEX idx_evidences_brand    ON evidences(brand);
CREATE INDEX idx_evidences_source   ON evidences(source_type);
CREATE INDEX idx_evidences_cred     ON evidences(credibility DESC);

-- ---------- 埋点 ----------
-- 主键是 (task_id, span_id)：span 序号按任务从 1 开始编（这样
-- 决策回放的滑杆看到的是"这次任务的第 37 步"），所以两个任务
-- 一定会出现同名 span。单列主键会让后跑的任务覆盖掉先跑的埋点。
CREATE TABLE traces (
    span_id           TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    parent_id         TEXT NOT NULL DEFAULT '',
    kind              TEXT NOT NULL DEFAULT '',
    name              TEXT NOT NULL DEFAULT '',
    purpose           TEXT NOT NULL DEFAULT '',
    provider          TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL DEFAULT '',
    started_at        TEXT NOT NULL DEFAULT '',
    ended_at          TEXT NOT NULL DEFAULT '',
    duration_ms       INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'ok',
    error             TEXT NOT NULL DEFAULT '',
    detail            TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (task_id, span_id)
);
CREATE INDEX idx_traces_task ON traces(task_id, started_at);

-- ---------- 事件日志 ----------
-- 断线续传的持久层。seq 是**按任务**的，(task_id, seq) 做主键
-- 顺带保证了"同一任务不会有两个 seq 相同的事件"。
CREATE TABLE task_events (
    task_id    TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    type       TEXT NOT NULL,
    data       TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);

-- ---------- 报告批注与反馈 ----------
-- 代理主键 feedback_id，不是 report_id。
-- 参考实现用 report_id 做主键，每次提交覆盖上一次，
-- 「人工修正率」永远只能看快照、看不出趋势。
CREATE TABLE report_feedback (
    feedback_id TEXT PRIMARY KEY,
    report_id   TEXT NOT NULL,
    section_key TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'annotation',
    content     TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_feedback_report ON report_feedback(report_id, created_at);

-- ---------- 订阅 ----------
-- 唯一索引建在 (kind, target) 上而不是 target 上：
-- 同一个品牌既可以被"每周品牌动态"订阅，也可以被"每周定价变化"订阅。
CREATE TABLE subscriptions (
    subscription_id TEXT PRIMARY KEY,
    kind            TEXT NOT NULL DEFAULT 'brand',
    target          TEXT NOT NULL,
    mode            TEXT NOT NULL DEFAULT 'deep',
    interval_hours  INTEGER NOT NULL DEFAULT 168,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_run_at     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_subscriptions_target ON subscriptions(kind, target);

-- ---------- 专家统计 ----------
CREATE TABLE expert_stats (
    expert_id       TEXT PRIMARY KEY,
    tasks           INTEGER NOT NULL DEFAULT 0,
    claims          INTEGER NOT NULL DEFAULT 0,
    evidences       INTEGER NOT NULL DEFAULT 0,
    reworks         INTEGER NOT NULL DEFAULT 0,
    avg_credibility REAL NOT NULL DEFAULT 0,
    last_active_at  TEXT NOT NULL DEFAULT ''
);

COMMIT;
"""


# ============================================================
# v2：traces 记缓存命中的输入 token
# ============================================================

# 为什么要加这一列：成本表里只有 `cost_usd` 一个数时，"这份报告很便宜"
# 和"定价表配错了"在界面上长得一模一样。命中价与未命中价差 50 倍，
# 把命中数一起存下来，成本才是一个可以复核的数而不是一个孤零零的金额。
#
# 默认 0：v1 的库里已经存下来的那些 span 当时没有这个信息，
# 补 0 是唯一诚实的取值——"不知道命中多少"按未命中计，成本偏高而不是偏低。
_V2 = """
BEGIN;

ALTER TABLE traces ADD COLUMN cached_prompt_tokens INTEGER NOT NULL DEFAULT 0;

COMMIT;
"""


# ============================================================
# v3：知识库筛选面的覆盖索引
# ============================================================
#
# v1 就有 `idx_evidences_source(source_type)` 与 `idx_evidences_brand(brand)`，
# 但筛选面那两条查询**靠它们快不起来**——查询里除了分组列还要
# `COUNT(DISTINCT evidence_id)`，而索引里没有 `evidence_id`，
# 所以每扫一行都要回表取一次。
#
# **有索引 ≠ 有覆盖索引。** 这一点查询计划说得很直白：
#
#     加索引前  SCAN evidences USING INDEX idx_evidences_source | USE TEMP B-TREE ...
#     加索引后  SCAN evidences USING COVERING INDEX idx_evidences_source_eid | ...
#
# 两行都是"走了索引"，差别只在**回不回表**，而时间差 38 倍。
# 所以"走了索引"不是一条性能结论，"索引里有查询要的全部列"才是。
#
# 实测（data/loadtest.db，48,384 行）：
#
#     by_source_type   303 ms  →   8 ms
#     by_brand          97 ms  →   9 ms
#     total             20 ms  →   9 ms
#
# 顺带确认了**列表没被拖慢**：`_library_rows` 第一步 151 ms → 151 ms，
# 查询计划也仍是 `SCAN evidences`（它还要 `task_id` 和 `credibility`，
# 这两条索引里没有）。加了索引不等于所有查询都改走索引。
#
# 与之相对的另一次实验（见 `repo/evidences.py` 的 `_library_rows`）：
# 那次查询要的是**整行**（含 `full_text`），加索引之后每行都要回表随机读，
# 反而从 357 ms 变成 648 ms。**同一个"加索引"的动作，
# 一次快 40 倍、一次慢 1.8 倍，区别只在查询要不要回表。**
# 所以这两条索引是量出来的，不是照着"筛选列都该建索引"的规矩加的。
#
# 代价：两次额外的 B 树维护。证据是**批量**写入（`save_many` 一个 executemany），
# 不是逐条插入，摊到每条上的代价很小；而筛选面是知识库页首屏，
# 每次打开都要付。这个交换划得来。
_V3 = """
BEGIN;

CREATE INDEX idx_evidences_source_eid ON evidences(source_type, evidence_id);
CREATE INDEX idx_evidences_brand_eid  ON evidences(brand, evidence_id);

COMMIT;
"""


# ============================================================
# v4：report_team 关联表
# ============================================================
#
# 问题不是"以后会慢"，是**压测当场量出来的慢**。
#
# `team_usage()` 一直是从 `reports.data` 的 `team` 字段数出来的，
# 它自己的 docstring 也早就写明了这条路的边界：
#
#     "一次访问一份报告"这个前提在这里**不成立**：这个函数一次要读全部报告。
#     现在 12 份、平均几百 KB，实测几毫秒，可以接受。规模上去之后
#     正确的做法不是加缓存，而是加一张 `report_team(report_id, expert_id)`
#     关联表，让这个查询变成一次 `GROUP BY`。
#
# 压测把这句话兑现了。实测（data/loadtest.db，233 份报告 / 41 MB 正文）：
#
#     team_usage()      950 ms   ← /api/experts 每次请求都调它
#     load_experts()    0.3 ms   ← 同一接口里的另一半，可以忽略
#
# 也就是说名册页每打开一次要读 41 MB 正文再解析，只为数出 48 个人
# 各被派过几次。20 个并发用户打上去，一个单进程 uvicorn 直接塌掉
# （压测读数从 892 req 掉到 425 req，聚合 p95 从 2.2 s 涨到 7.4 s，
# 而 `/api/experts` 是其中塌得最狠的那个）。
#
# 所以这次是照着自己写下的结论做，不是临时想到的优化。
#
# 回填
# ----
# 只建表不回填，会让**已经存在的报告**参与度全部变成 0——
# 界面照样能打开，数字只是安静的错了。这跟"写了却没接上"是同一类缺陷，
# 所以建表和回填必须在同一条迁移里。
#
# 回填用 `json_each` 直接在 SQL 里把队伍拆开。
#
# **这条 SQL 的第一版是错的，被回填等价性测试当场抓住。**
# 值得原样记下来，因为踩的是 SQLite JSON 函数一个不明显的性质：
#
#     `json_each(X)` 与单参数的 `json_type(X)` 都会把 X **当成一份
#     JSON 文档去解析**。X 是一个普通字符串时，它们不返回"不是 JSON"，
#     而是直接抛 `malformed JSON`。
#
# 而队伍里到处都是普通字符串：`"team": {"lead": "L3-001"}` 时
# `je1.value` 就是 `L3-001`，成员本身也是 `L3-001`。
# 于是第一版的两处 `json_type(...)` 与第二层的 `json_each(je1.value)`
# 全是地雷，迁移直接失败——**而这个迁移是启动时跑的**，
# 也就是说一份形状不对的老报告能让整个应用起不来。
#
# 正确写法有三条，缺一不可：
#
#   1. `WHERE json_valid(data)` 放在**子查询**里。放外层 WHERE 不保证
#      先于 JOIN 求值，而 `json_each()` 的参数在 JOIN 时就算掉了。
#   2. 第二层的参数用 CASE 兜住：参数本身永远得是合法 JSON。
#   3. 判类型一律用 `json_each` 虚拟表自带的 **`.type` 列**，
#      不用 `json_type()`。`.type` 是列，取它不会重新解析，
#      给什么都不会抛。
#
# `je1.type = 'array'` 对应 `team_ids()` 里那句 `isinstance(members, list)`，
# 语义上必须留着：CASE 那个兜底只保证不崩，不保证只收数组。
#
# 与 `team_ids()` 的**已知差异（只有一处）**：成员不是字符串时
# （比如 `["L1-001", null]`），Python 的 `str(member)` 会得到一个
# `"None"` 这样的一格，回填则直接丢掉它。丢掉是对的——那不是一个专家 id，
# 它出现在参与度统计里只说明上游写坏了。这条差异由
# `tests/unit/test_team_usage.py` 里一条专门的用例钉住。
_V4 = """
BEGIN;

CREATE TABLE report_team (
    report_id TEXT NOT NULL,
    expert_id TEXT NOT NULL,
    PRIMARY KEY (report_id, expert_id)
);
CREATE INDEX idx_report_team_expert ON report_team(expert_id);

INSERT OR IGNORE INTO report_team (report_id, expert_id)
SELECT r.report_id, CAST(je2.value AS TEXT)
  FROM (SELECT report_id, data FROM reports WHERE json_valid(data)) AS r
  JOIN json_each(r.data, '$.team') AS je1
  JOIN json_each(CASE WHEN json_valid(je1.value) THEN je1.value ELSE '[]' END) AS je2
 WHERE je1.type = 'array'
   AND je2.type IN ('text', 'integer', 'real');

COMMIT;
"""


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "初始表结构：任务/报告/证据/埋点/事件/批注/订阅/专家统计", _V1),
    Migration(2, "traces 增加 cached_prompt_tokens：缓存命中的输入 token 数", _V2),
    Migration(3, "知识库筛选面的覆盖索引：让 COUNT(DISTINCT evidence_id) 不必回表", _V3),
    Migration(4, "report_team 关联表：专家参与度不再靠读全部报告正文数出来", _V4),
)

#: 代码期望的库版本。启动时断言实际版本不小于它——
#: 小于说明有人忘了提交迁移文件，那需要在开发期就炸掉。
TARGET_VERSION = MIGRATIONS[-1].version


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def migrate(conn: sqlite3.Connection, *, verbose: bool = False) -> list[str]:
    """把库升到最新版本，返回执行过的迁移描述。

    幂等：已应用过的迁移会被跳过，所以可以在每次启动时无条件调用。
    """
    applied: list[str] = []
    version = current_version(conn)
    for migration in sorted(MIGRATIONS, key=lambda m: m.version):
        if migration.version <= version:
            continue
        log.info("应用迁移 v%d：%s", migration.version, migration.description)
        conn.executescript(migration.sql)
        # user_version 不支持参数占位符，只能拼字符串。
        # 这里的值是代码里的整数常量，不来自外部输入。
        conn.execute(f"PRAGMA user_version={int(migration.version)}")
        applied.append(f"v{migration.version} {migration.description}")
        version = migration.version
        if verbose:
            print(f"  已应用迁移 v{migration.version}：{migration.description}")
    return applied
