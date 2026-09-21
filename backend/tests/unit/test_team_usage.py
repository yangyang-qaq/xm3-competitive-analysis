"""专家参与度：`team_ids()`、`team_usage()` 与 `report_team` 关联表。

"每位专家被派进过多少次调研"是**名册页上唯一的真实战绩**
------------------------------------------------------
名册自带的 `stats` 里每个数都是 0，而那是"还没被量过"
（`source == "seed"`），不是"量出来是 0"。所以那一页能显示的
真实数字只有这个参与度，它必须是对的。

队伍本身写在 `reports.data` 的 `team` 字段里（`dispatch` 写进去的），
但**参与度不再从那里数**——`team_usage()` 读的是 `report_team` 关联表，
由 `save()` 维护、由迁移 v4 回填。原因在
`repo/reports.team_usage` 的 docstring 里写着：从正文数要读全部报告，
233 份时 950 ms，而这个查询在名册页的每一次请求里。

于是这个文件守两件事，而它们**必须同时成立**：
  1. `team_ids()` 仍然是"一份报告里都有谁"的唯一定义（下面那一组用例）；
  2. 关联表里的内容与 `team_ids()` 的结果一致 —— 包括**迁移回填出来的**
     那部分。第 2 条是新的失败面：回填写错的话，`team_ids()` 全绿，
     而老报告的参与度会安静地少几个。

为什么值得单独一个文件：这个数错了不会崩、不会报错，
只会让某个人显示"参与过 7 次"而实际是 9 次——
看起来像数据问题，不像代码问题。
"""
from __future__ import annotations

import sqlite3

from app.core.models import ReportRecord
from app.db.migrations import MIGRATIONS
from app.db.repo import reports as reports_repo


def _save(report_id: str, team: dict | None, **extra) -> str:
    data: dict = dict(extra)
    if team is not None:
        data["team"] = team
    return reports_repo.save(
        ReportRecord(
            report_id=report_id,
            task_id=f"TK-{report_id}",
            query="对比 Notion 与 Obsidian",
            subject="Notion 与 Obsidian",
            data=data,
        )
    )


# ============================================================
# team_ids：从一份报告正文里取人
# ============================================================


def test_三个角色的人都被取到(mock_pipeline_db) -> None:
    """`lead` / `strategists` / `executors` 一个都不能漏，**且要过一遍数据库**。

    抓的 bug：只读 `executors`（"干活的人"听起来才是"参与调研的人"）。
    那样两层决策者永远显示参与 0 次——而他们恰恰是每份报告里
    都在的那三个人。

    断言走 `get()` 读回来而不是直接喂一个内存里的 dict：队伍是
    以 JSON 存在 `reports.data` 这一列里的，中间有一次序列化往返。
    直接测内存字典的话，一个把 `team` 写丢的 codec bug 照样绿。
    """
    _save("RP-1", {
        "lead": ["L3-001"],
        "strategists": ["L2-001", "L2-002"],
        "executors": ["L1-001"],
    })

    record = reports_repo.get("RP-1")
    assert record is not None
    assert "team" in record.data, "team 字段没能在存/取之间活下来"

    assert reports_repo.team_ids(record.data) == {
        "L3-001", "L2-001", "L2-002", "L1-001",
    }


def test_同一个人挂两个角色只算一次(mock_pipeline_db) -> None:
    """他可能既是 lead 又被列进 strategists。

    抓的 bug：按"出现过几次"累加。那样一个人参与 1 份报告会显示 2 次，
    参与数**大于报告总数**——这个数字自相矛盾，但没有任何东西会报错。
    """
    ids = reports_repo.team_ids({"team": {
        "lead": ["L3-001"],
        "strategists": ["L3-001"],
        "executors": [],
    }})

    assert ids == {"L3-001"}, f"同一个人被算了多次：{ids}"


def test_多一个新角色也要算进去(mock_pipeline_db) -> None:
    """**不写死 `lead/strategists/executors` 这三个键。**

    抓的 bug：把键名写死。队伍形状一变（比如加一个"复核人"），
    那个角色的人在所有统计里凭空消失——而且只表现为"这个人参与次数偏少"。
    """
    ids = reports_repo.team_ids({"team": {"reviewers": ["L1-999"]}})

    assert ids == {"L1-999"}, "新增的角色没有被统计"


def test_队伍字段缺失或形状不对时返回空集而不是抛错(mock_pipeline_db) -> None:
    """历史数据、mock 数据、将来某个 stage 漏写了 `team`——都会走到这里。

    抓的 bug：直接 `data["team"].values()`。缺键会 `KeyError`，
    而调用它的是 `/api/experts` 和 `/api/dashboard` 两个接口，
    表现是**整个仪表盘 500**，只因为一份老报告的正文里没有队伍字段。
    """
    assert reports_repo.team_ids({}) == set()
    assert reports_repo.team_ids(None) == set()
    assert reports_repo.team_ids({"team": None}) == set()
    assert reports_repo.team_ids({"team": []}) == set()
    # 名单被写成了一个字符串而不是列表。不判 `isinstance(..., list)` 的话
    # 这里会按字符拆开，`"L3-001"` 变成 6 个单字 id——48 人之外凭空多出
    # 一堆"专家"，而名册页只会显示成参与度全是 0。
    assert reports_repo.team_ids({"team": {"lead": "L3-001"}}) == set()


# ============================================================
# team_usage：跨报告数
# ============================================================


def test_参与度数的是报告数不是人次(mock_pipeline_db) -> None:
    """两份报告都派了他 → 2。

    抓的 bug：把 `data` 读出来之后按名单长度累加，或者忘了去重。
    这两个都会让参与度大于报告总数。
    """
    _save("RP-1", {"lead": ["L3-001"], "executors": ["L1-001"]})
    _save("RP-2", {"lead": ["L3-001"], "strategists": ["L3-001"], "executors": ["L1-002"]})

    usage = reports_repo.team_usage()

    assert usage["L3-001"] == 2, "同一个人在两份报告里被数成了更多次"
    assert usage["L1-001"] == 1
    assert usage["L1-002"] == 1
    # 人名槽位一共 5 个（RP-1 两个 + RP-2 三个），去重后是 4 个人。
    # **这个 4 就是去重生效的证据**：不去重的话这里是 5，
    # 而 L3-001 会显示"参与 2 次"配一个更大的总人数。
    assert sum(usage.values()) == 4, f"总人数不对（5 说明没去重）：{usage}"


def test_没被派过的人不在表里(mock_pipeline_db) -> None:
    """`.get(id, 0)` 的兜底由调用方做。

    抓的 bug：`team_usage()` 返回的字典里为 48 个人都填 0。
    那样"这个人从来没被派过"和"这个人被派过 0 次"就没法区分了——
    而后者在系统里不成立（派过就是 ≥1），填 0 是在编数据。
    """
    _save("RP-1", {"lead": ["L3-001"]})

    usage = reports_repo.team_usage()

    assert "L1-001" not in usage
    assert usage.get("L1-001", 0) == 0


def test_空库返回空字典(mock_pipeline_db) -> None:
    """全新的库、还没跑过调研。仪表盘与名册页都要能打开。"""
    assert reports_repo.team_usage() == {}


def test_没有队伍字段的报告被跳过而不是让统计整体失败(mock_pipeline_db) -> None:
    """一份报告缺 `team`，不该让另外两份的统计一起丢掉。

    抓的 bug：`team_usage()` 里不兜底，遇到一份老报告就抛异常。
    表现是名册页上**所有人的参与度都变成 0**——
    因为整个接口挂了，前端按"没有数据"渲染。
    """
    _save("RP-1", {"lead": ["L3-001"]})
    _save("RP-2", None)  # 正文里根本没有 team 字段
    _save("RP-3", {"lead": ["L3-001"]})

    usage = reports_repo.team_usage()

    assert usage["L3-001"] == 2, "一份缺字段的报告让其他报告也没被数到"


# ============================================================
# report_team 关联表：与 team_ids() 必须一致
#
# 参与度现在读的是表，不是正文。于是多了一个失败面：
# **表和正文对不上**。对不上不会报错，只会让某个人参与度偏少，
# 看起来像数据问题。下面这一组把两边钉在一起。
# ============================================================


def _table_usage() -> dict[str, int]:
    """直接读关联表，不经过 `team_usage()`——否则就是拿实现验实现。"""
    from app.db.connection import get_conn

    return {
        row["expert_id"]: int(row["n"])
        for row in get_conn().execute(
            "SELECT expert_id, COUNT(*) AS n FROM report_team GROUP BY expert_id"
        ).fetchall()
    }


def test_关联表与team_ids数出来的一致(mock_pipeline_db) -> None:
    """写入路径的核心断言：表里的内容 == 从正文数出来的内容。

    拿 `team_ids()` 当参照物而不是当被测对象：它是"一份报告里都有谁"的
    唯一定义，表只是它的一个物化。两者不同就说明同步漏了。
    """
    shapes = [
        ("RP-1", {"lead": ["L3-001"], "strategists": ["L2-001"], "executors": ["L1-001"]}),
        ("RP-2", {"reviewers": ["L1-007"]}),                     # 没登记过的新角色
        ("RP-3", {"lead": ["L3-001"], "strategists": ["L3-001"]}),  # 一人挂两角
        ("RP-4", None),                                          # 没有 team 字段
        ("RP-5", {"lead": []}),                                  # 空名单
    ]
    for report_id, team in shapes:
        _save(report_id, team)

    want: dict[str, int] = {}
    for report_id, _ in shapes:
        record = reports_repo.get(report_id)
        assert record is not None
        for expert_id in reports_repo.team_ids(record.data):
            want[expert_id] = want.get(expert_id, 0) + 1

    assert _table_usage() == want
    assert reports_repo.team_usage() == want


def test_重写同一份报告时旧的队伍不会留在表里(mock_pipeline_db) -> None:
    """返工之后队伍会重派，而 `save()` 是整体替换。

    抓的 bug：同步只 `INSERT OR IGNORE` 不先删。那样第一版的成员会留在
    表里，表现是**某个人的参与度永远降不下来**——而且它比"漏加"更难发现，
    因为数字看着是合理的，只是偏大。

    这条例子里 L1-001 从第一版退出了，所以他能区分两种实现。
    """
    _save("RP-1", {"lead": ["L3-001"], "executors": ["L1-001"]})
    _save("RP-1", {"lead": ["L3-001"], "executors": ["L1-002"]})

    usage = reports_repo.team_usage()

    assert usage == {"L3-001": 1, "L1-002": 1}, f"第一版的队伍没被清掉：{usage}"
    assert "L1-001" not in usage


def test_同一份报告重复保存不会让参与度翻倍(mock_pipeline_db) -> None:
    """`INSERT OR REPLACE` 打在 reports 上，关联表得自己保证不重复。

    抓的 bug：同步时用了 `INSERT`。主键是 `(report_id, expert_id)`，
    所以第二次会抛 `IntegrityError`——而它在 `save()` 里，
    意味着**重跑一次任务就写不进报告**，整个流水线在最后一步炸掉。
    """
    _save("RP-1", {"lead": ["L3-001"]})
    _save("RP-1", {"lead": ["L3-001"]})
    _save("RP-1", {"lead": ["L3-001"]})

    assert reports_repo.team_usage() == {"L3-001": 1}


def test_回填出来的参与度与从正文数出来的一样(tmp_path) -> None:
    """**迁移 v4 的回填**。上面几条走的是 `save()`，这条走的是 SQL。

    回填是纯 SQL（`json_each` 拆 JSON），与 `team_ids()` 是两份独立实现。
    回填错了的后果很隐蔽：新写的报告参与度对，**已经存在的报告全是 0**，
    页面上看不出任何异常。

    所以这里造一个**停在 v3 的库**、直接塞进报告行（绕开 `save()`，
    即绕开新的写入路径），再升到 v4，然后拿 `team_ids()` 的结果当参照物。

    **这条用例的第一版有一半是空的，必须记下来。** 语料里前五行我按
    `_save()` 的入参形状写成了 `{"lead": [...]}`——但 `_save()` 那个形状是
    **被包进 `{"team": ...}` 之后才落库的**。直接塞库时少了那一层，
    `$.team` 根本不存在，于是回填什么都没插，而参照物那侧按同样的
    错数据也算不出人来：**两边都是空字典，断言成立。**
    九行语料里五行在验一个空集，而它看起来是绿的。

    一条"两边都空"的等价性断言是没有意义的，所以末尾加了反空洞下限：
    先钉住这份语料**确实**产出若干个人和若干个具体数字。
    """
    import json

    conn = sqlite3.connect(str(tmp_path / "v3.db"))
    conn.row_factory = sqlite3.Row
    for migration in MIGRATIONS:
        if migration.version > 3:
            break
        conn.executescript(migration.sql)
        conn.execute(f"PRAGMA user_version={migration.version}")

    # 键一律是落库后的正文形状：队伍挂在 `team` 底下。
    corpus = [
        ("RP-1", {"team": {"lead": ["L3-001"], "strategists": ["L2-001", "L2-002"],
                           "executors": ["L1-001", "L1-002"]}}),
        ("RP-2", {"team": {"lead": ["L3-001"], "executors": ["L1-001"]}}),
        ("RP-3", {"team": {"reviewers": ["L1-009"]}}),        # 没登记过的角色
        ("RP-4", {"team": {"lead": ["L3-001"], "strategists": ["L3-001"]}}),  # 一人两角
        ("RP-5", {"team": {"lead": []}}),
        ("RP-6", {}),                                          # 正文里没有 team 键
        ("RP-7", {"team": "L3-001"}),                          # 名单写成了字符串
        ("RP-8", {"team": ["L3-001"]}),                        # 队伍写成了列表
        ("RP-9", {"team": {"lead": [7, 9]}}),                  # 成员写成了数字
        # RP-10 是**唯一**能验证 `je1.type = 'array'` 那一行的形状：
        # 一个非数组的成员，它的值本身又是一段合法 JSON。
        # 少了它，那条过滤写成 `WHERE 1=1` 测试照样绿——
        # 因为其他非数组成员（"L3-001"、["L3-001"]）都被第二层的
        # `json_valid` 兜底挡掉了，根本走不到类型判断。
        ("RP-10", {"team": {"lead": ["L3-001"], "budget": 5, "note": "临时凑的"}}),
    ]
    for report_id, data in corpus:
        conn.execute(
            "INSERT INTO reports (report_id, task_id, generated_at, data) VALUES (?,?,?,?)",
            (report_id, "TK-1", "2026-01-01", json.dumps(data, ensure_ascii=False)),
        )
    conn.commit()

    v4 = next(m for m in MIGRATIONS if m.version == 4)
    conn.executescript(v4.sql)

    got = {
        row["expert_id"]: int(row["n"])
        for row in conn.execute(
            "SELECT expert_id, COUNT(*) AS n FROM report_team GROUP BY expert_id"
        ).fetchall()
    }

    want: dict[str, int] = {}
    for report_id, _ in corpus:
        raw = conn.execute(
            "SELECT data FROM reports WHERE report_id = ?", (report_id,)
        ).fetchone()["data"]
        for expert_id in reports_repo.team_ids(json.loads(raw)):
            want[expert_id] = want.get(expert_id, 0) + 1
    conn.close()

    # 反空洞下限。**没有这三行，上面那句断言可以在两边都空的时候通过**，
    # 而"两边都空"正是这条用例第一版的样子。
    assert want, "语料没有产出任何参与关系，这条等价性断言等于没测"
    assert want == {
        "L3-001": 4,   # RP-1 / RP-2 / RP-4（一人挂两角，只算一次）/ RP-10
        "L2-001": 1, "L2-002": 1,
        "L1-001": 2,   # RP-1 / RP-2
        "L1-002": 1,
        "L1-009": 1,
        "7": 1, "9": 1,  # 数字成员也要被算上
    }, f"语料本身的预期就不对：{want}"

    assert got == want, f"回填与 team_ids 数出来的不一致：回填 {got}，参照 {want}"


def test_回填遇到非字符串成员时丢掉它而不是编一个None出来(tmp_path) -> None:
    """**已知且有意保留的一处差异**，钉住它是为了让它是"决定"而不是"意外"。

    `team_ids()` 对任何成员做 `str()`，所以 `["L1-001", None]` 会得到
    一个叫 `"None"` 的"专家"。回填的 SQL 只收 text/integer/real，
    把它丢掉。丢掉是对的：`None` 不是一个专家 id，它出现在参与度统计里
    只说明上游写坏了——而 `"None"` 会以"某个人参与过 1 次"的形式
    混进名册页，谁也看不出那是垃圾。

    数字成员两边一致（Python 的 `str(7)` 与 SQL 的 `CAST(7 AS TEXT)`
    都是 `"7"`），所以这条的差异只在 null / 对象 / 数组上。
    """
    import json

    conn = sqlite3.connect(str(tmp_path / "v3.db"))
    conn.row_factory = sqlite3.Row
    for migration in MIGRATIONS:
        if migration.version > 3:
            break
        conn.executescript(migration.sql)
        conn.execute(f"PRAGMA user_version={migration.version}")

    data = {"team": {"lead": ["L3-001", None]}}
    conn.execute(
        "INSERT INTO reports (report_id, task_id, generated_at, data) VALUES (?,?,?,?)",
        ("RP-1", "TK-1", "2026-01-01", json.dumps(data)),
    )
    conn.commit()
    conn.executescript(next(m for m in MIGRATIONS if m.version == 4).sql)

    expert_ids = {
        row["expert_id"] for row in conn.execute("SELECT expert_id FROM report_team").fetchall()
    }
    conn.close()

    assert expert_ids == {"L3-001"}, f"空成员被当成了一个人：{expert_ids}"
    # 这一行是这条例子的重点：参照物**确实**会产出 "None"，
    # 所以上面那个断言不是"两边本来就一样"，而是一次有意的取舍。
    assert reports_repo.team_ids(data) == {"L3-001", "None"}


# ============================================================
# reports_of_expert：名册详情页的那次查询
# ============================================================


def test_只返回这个人参与过的报告(mock_pipeline_db) -> None:
    _save("RP-1", {"lead": ["L3-001"], "executors": ["L1-001"]})
    _save("RP-2", {"lead": ["L3-002"], "executors": ["L1-001"]})
    _save("RP-3", {"lead": ["L3-003"]})

    assert {item["reportId"] for item in reports_repo.reports_of_expert("L1-001")} == {
        "RP-1",
        "RP-2",
    }
    assert [item["reportId"] for item in reports_repo.reports_of_expert("L3-003")] == ["RP-3"]
    assert reports_repo.reports_of_expert("L1-999") == []


def test_名册详情不读报告正文(mock_pipeline_db) -> None:
    """这条查询的**全部意义**就是不读 `data`（平均 172 KB 一份）。

    抓的 bug：名册详情页退回 `list_recent(200)` 再自己过滤——那次实测
    为了回答"他参与过哪几次"，读了 233 份报告的正文。

    断言"返回里没有 data 键"是在守这个意图：哪天有人图省事把 `SELECT *`
    写回来，正文就会重新出现在这条路径上，而**功能上一切正常**。
    """
    _save("RP-1", {"lead": ["L3-001"], "body": "x" * 5000})

    item = reports_repo.reports_of_expert("L3-001")[0]

    assert "data" not in item
    assert set(item) == {"reportId", "taskId", "query", "subject", "generatedAt"}


def test_专家详情接口复用同一次查询(mock_pipeline_db) -> None:
    """接口层与 repo 层必须是同一条路。

    `test_名册详情不读报告正文` 守得住 repo，守不住"接口又自己写了一遍"。
    这条从接口读回去，确认它返回的报告列表确实来自关联表。
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    _save("RP-1", {"lead": ["L3-001"]})
    _save("RP-2", {"lead": ["L3-002"]})

    with TestClient(create_app()) as client:
        payload = client.get("/api/experts/L3-001").json()

    assert [item["reportId"] for item in payload["reports"]] == ["RP-1"]
    assert payload["participation"] == 1
