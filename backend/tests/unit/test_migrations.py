"""迁移与表结构测试。

用裸 `sqlite3.connect` 而不是 `get_conn()`：迁移是唯一不依赖线程局部连接
的模块（它在启动时跑一次），用裸连接测等于顺带证明了这一点。而且这样
测试之间不会互相看到对方的库文件——那类污染在 Windows 上还会因为文件
句柄没释放而变成"删不掉临时文件"的报错。

这里重点验的不是"表建出来了"，而是**三个复合主键的选择**。它们是设计
决策，写错了不会报错，只会静默覆盖数据：
  - `evidences` 用 `(task_id, evidence_id)`：evidence_id 由 URL 摘要生成、
    跨任务稳定，所以同一个 URL 在两个任务里是同一个 id，但它们是
    **不同的证据**（命中维度不同、品牌不同）。单列主键会让第二个任务
    覆盖掉第一个任务的记录。
  - `traces` 用 `(task_id, span_id)`：span 序号按任务从 1 编，
    所以两个任务一定会出现同名 span。
  - `report_feedback` 用代理主键：用 report_id 做主键的话，每次提交
    都覆盖上一次，「人工修正率」永远只能看快照、看不出趋势。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.db.migrations import MIGRATIONS, TARGET_VERSION, current_version, migrate

_TABLES = (
    "tasks",
    "reports",
    "evidences",
    "traces",
    "task_events",
    "report_feedback",
    "subscriptions",
    "expert_stats",
)


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(tmp_path / "migrate.db"))
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


@pytest.fixture
def migrated(conn: sqlite3.Connection) -> sqlite3.Connection:
    migrate(conn)
    return conn


# ============================================================
# 版本
# ============================================================


def test_fresh_database_is_at_version_zero(conn: sqlite3.Connection) -> None:
    """全新库的版本号是 0，不是 1。

    0 与"v1 已应用"必须可区分——否则启动时无法判断该不该建表，
    只能靠 `CREATE TABLE IF NOT EXISTS` 硬试，而那无法处理"加一列"。
    """
    assert current_version(conn) == 0


def test_migrate_brings_the_database_to_the_target_version(migrated: sqlite3.Connection) -> None:
    assert current_version(migrated) == TARGET_VERSION


def test_migrate_creates_every_table(migrated: sqlite3.Connection) -> None:
    names = {
        row["name"]
        for row in migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert set(_TABLES) <= names


def test_migrate_creates_the_indexes_the_queries_rely_on(migrated: sqlite3.Connection) -> None:
    """报告列表按 generated_at 倒序、证据按可信度排序、事件按 seq 取——
    都是列表页的主要读路径，缺索引会随数据量线性变慢。"""
    indexes = {
        row["name"]
        for row in migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert {
        "idx_reports_generated",
        "idx_traces_task",
        "idx_evidences_cred",
        "idx_feedback_report",
        "idx_evidences_source_eid",
        "idx_evidences_brand_eid",
    } <= indexes


def test_知识库筛选面走的是覆盖索引而不是回表(migrated: sqlite3.Connection) -> None:
    """**守的不是"有索引"，是"索引能独立回答这条查询"。**

    v1 里本来就有 `idx_evidences_source(source_type)`，查询计划也确实写着
    `USING INDEX idx_evidences_source` —— 看上去一切正常，实测却是 303 ms。
    原因是索引里没有 `evidence_id`，而查询要 `COUNT(DISTINCT evidence_id)`，
    于是每扫一行都要**回表**取一次。v3 把 `evidence_id` 补进索引之后，
    计划变成 `USING COVERING INDEX`，同样 48,384 行降到 8 ms。

    所以这条测试断言的是查询计划里的 `COVERING` 字样，而不是索引在不在。
    哪天有人觉得"evidence_id 放索引里多余"把它去掉，索引照样存在、
    测试若只查名字照样绿，而性能悄悄退回 38 倍之前——
    那正是这个仓库里反复出现的那类缺陷：**东西都在，只是没接上。**

    计划在空表上也一样（实测 0 / 5 / 200 行三种规模都稳定选中覆盖索引），
    所以这条不依赖夹具数据量。
    """
    queries = {
        "idx_evidences_source_eid": "SELECT source_type, COUNT(DISTINCT evidence_id) AS n "
                                    "FROM evidences GROUP BY source_type ORDER BY n DESC",
        "idx_evidences_brand_eid": "SELECT brand, COUNT(DISTINCT evidence_id) AS n FROM evidences "
                                   "WHERE brand <> '' GROUP BY brand ORDER BY n DESC LIMIT 50",
    }
    for index_name, sql in queries.items():
        plan = " | ".join(row[3] for row in migrated.execute("EXPLAIN QUERY PLAN " + sql))
        assert f"COVERING INDEX {index_name}" in plan, (
            f"筛选面没有走覆盖索引 {index_name}，计划是：{plan}\n"
            "多半是索引里的 evidence_id 被去掉了——补回去，别改这条测试。"
        )


def test_migrate_is_idempotent_and_preserves_data(migrated: sqlite3.Connection) -> None:
    """幂等是"每次启动无条件调用它"的前提。

    这里顺带断言数据还在：迁移若用了 `DROP TABLE` + 重建，幂等性测试
    依然会通过，而数据已经没了。
    """
    migrated.execute(
        "INSERT INTO tasks (task_id, query, created_at, updated_at) VALUES (?,?,?,?)",
        ("TK-1", "对比 Notion 与 Obsidian", "2026-01-01", "2026-01-01"),
    )

    assert migrate(migrated) == []
    assert current_version(migrated) == TARGET_VERSION
    assert migrated.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_migrate_reports_only_the_versions_it_applied(conn: sqlite3.Connection) -> None:
    """返回值是给人看的启动日志，不是"当前版本"。

    第一次返回全部、第二次返回空，这个差别让"迁移到底跑了没有"
    在日志里是一个明确的事实，而不是需要推断的事。
    """
    first = migrate(conn)
    second = migrate(conn)

    assert len(first) == len(MIGRATIONS)
    assert first[0].startswith("v1 ")
    assert second == []


def test_migration_versions_are_unique_and_increasing() -> None:
    """「迁移只增不改」这条规则的可执行版本。

    同一个版本号出现两次，说明有人改了已发布的那条（而不是新增一条），
    于是老库不会重跑、新库拿到的是新 SQL——两台机器的表结构就此分叉，
    而且没有任何报错。
    """
    versions = [m.version for m in MIGRATIONS]

    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert versions[0] == 1


def test_every_migration_declares_its_own_transaction() -> None:
    """每条迁移自己带 BEGIN/COMMIT。

    `executescript()` 在执行前会隐式提交掉挂起的事务，所以靠外层
    `transaction()` 保证原子性是不成立的：半路失败会留下一个改了一半的库。
    把 `BEGIN;` 写进脚本里，`executescript` 就不会再插一脚。
    """
    for migration in MIGRATIONS:
        body = migration.sql.strip().upper()
        assert body.startswith("BEGIN"), migration.description
        assert body.endswith("COMMIT;"), migration.description


# ============================================================
# 复合主键（设计决策，写错了不会报错）
# ============================================================


def test_same_evidence_id_can_exist_in_two_tasks(migrated: sqlite3.Connection) -> None:
    """同一个 URL 被两个任务采到时，是**两条**证据，不是一条被覆盖。

    evidence_id 由 URL 摘要生成、跨任务稳定（这是有意的：同一份材料
    在两个报告里可以互相对照）。但它在两个任务里命中的维度不同、
    归属的品牌不同、交叉印证分也不同。单列主键会让后跑的任务
    静默把先跑的记录改掉——报告里的可信度会变成另一个任务算的。
    """
    sql = (
        "INSERT INTO evidences (evidence_id, task_id, url, captured_at) VALUES (?,?,?,?)"
    )
    migrated.execute(sql, ("EV-a1b2c3d4e5f6", "TK-1", "https://x.test/a", "2026-01-01"))
    migrated.execute(sql, ("EV-a1b2c3d4e5f6", "TK-2", "https://x.test/a", "2026-01-02"))

    rows = migrated.execute(
        "SELECT task_id FROM evidences WHERE evidence_id = ?", ("EV-a1b2c3d4e5f6",)
    ).fetchall()
    assert sorted(row["task_id"] for row in rows) == ["TK-1", "TK-2"]


def test_the_same_evidence_cannot_be_written_twice_in_one_task(
    migrated: sqlite3.Connection,
) -> None:
    """任务内重复采到同一条要能被 `INSERT OR REPLACE` 去重，
    所以主键必须含 task_id 而不只是 url。"""
    sql = "INSERT INTO evidences (evidence_id, task_id, url, captured_at) VALUES (?,?,?,?)"
    migrated.execute(sql, ("EV-a1b2c3d4e5f6", "TK-1", "https://x.test/a", "2026-01-01"))

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(sql, ("EV-a1b2c3d4e5f6", "TK-1", "https://x.test/a", "2026-01-02"))


def test_span_ids_are_scoped_per_task(migrated: sqlite3.Connection) -> None:
    """span 序号按任务从 1 编（决策回放看到的是"这次任务的第 37 步"），
    所以两个任务一定会出现同名 span。单列主键会让后跑的任务
    覆盖掉先跑的埋点——成本表会凭空少一笔。"""
    sql = (
        "INSERT INTO traces (span_id, task_id, kind, name) VALUES (?,?,?,?)"
    )
    migrated.execute(sql, ("SP-00001", "TK-1", "llm", "analyze_claims"))
    migrated.execute(sql, ("SP-00001", "TK-2", "llm", "analyze_claims"))

    assert migrated.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 2


def test_feedback_accumulates_instead_of_overwriting(migrated: sqlite3.Connection) -> None:
    """一份报告可以有多条批注。

    参考实现用 report_id 做主键，第二次提交覆盖第一次——
    「人工修正率」于是永远只有一个快照，看不出"这份报告被改了几轮"，
    而那个数字恰恰是质检闭环有没有起作用的证据。
    """
    sql = (
        "INSERT INTO report_feedback (feedback_id, report_id, section_key, content, created_at)"
        " VALUES (?,?,?,?,?)"
    )
    migrated.execute(sql, ("FB-1", "RP-1", "pricing", "这里的单价口径不对", "2026-01-01"))
    migrated.execute(sql, ("FB-2", "RP-1", "pricing", "补一条企业版报价", "2026-01-02"))

    rows = migrated.execute(
        "SELECT feedback_id FROM report_feedback WHERE report_id = ? ORDER BY created_at", ("RP-1",)
    ).fetchall()
    assert [row["feedback_id"] for row in rows] == ["FB-1", "FB-2"]


def test_event_seq_is_scoped_per_task(migrated: sqlite3.Connection) -> None:
    """`(task_id, seq)` 做主键顺带保证了同一任务不会有两个 seq 相同的事件——
    而 seq 撞号会直接破坏前端的 `seq <= lastSeq` 去重（事件随机丢失）。"""
    sql = "INSERT INTO task_events (task_id, seq, type, created_at) VALUES (?,?,?,?)"
    migrated.execute(sql, ("TK-1", 1, "thought", "2026-01-01"))
    migrated.execute(sql, ("TK-2", 1, "thought", "2026-01-01"))

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(sql, ("TK-1", 1, "thought", "2026-01-02"))


def test_subscriptions_are_unique_per_kind_and_target(migrated: sqlite3.Connection) -> None:
    """唯一索引建在 `(kind, target)` 上而不是 target 上：
    同一个品牌既可以被「每周品牌动态」订阅，也可以被「每周定价变化」订阅。"""
    sql = (
        "INSERT INTO subscriptions (subscription_id, kind, target, created_at)"
        " VALUES (?,?,?,?)"
    )
    migrated.execute(sql, ("SB-1", "brand", "Notion", "2026-01-01"))
    migrated.execute(sql, ("SB-2", "pricing", "Notion", "2026-01-01"))

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(sql, ("SB-3", "brand", "Notion", "2026-01-02"))


def test_evidence_columns_added_by_this_project_exist(migrated: sqlite3.Connection) -> None:
    """`credibility_breakdown` 与 `matched_dimensions` 是两个新增列。

    前者让"为什么这条 82 分"可以说出口（参考实现只返回一个裸整数，
    UI 和报告里都说不出来）；后者是维度覆盖率的真相源——
    覆盖率按证据判定，而不是按"字段有没有被填"。
    """
    columns = {row["name"] for row in migrated.execute("PRAGMA table_info(evidences)")}

    assert {"credibility_breakdown", "matched_dimensions", "full_text"} <= columns
