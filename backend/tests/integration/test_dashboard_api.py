"""`/api/dashboard`：竞争情报中心的那些数。

这个文件测的不是"接口能不能返回"，是**那几个数是怎么算出来的**
----------------------------------------------------------------
仪表盘最容易变成一块编数字的地方。"效率提升 83×""节省 5 小时"
这种数看着唬人，被问一句"怎么算的"就没了。所以这里的每条断言
都锚在**一个具体的算式**上，而不是"返回了一个正数"。

三对必须分开说的数（混起来就是自己打自己）
--------------------------------------
1. `sources`（266 条去重来源）≠ `mentions`（2426 行）
2. `passed`（过了质量门）≠ `publishable`（完整度够不够发）
3. `reports`（跑出报告的）≠ `tasks`（发起过的）

第 3 条尤其容易糊：分母用错的时候，"平均每篇报告用了 N 条证据"
会小一个数量级，而那个数看起来完全正常。
"""
from __future__ import annotations

from app.core.models import Evidence, ReportRecord, TaskRecord, evidence_id_for
from app.db.repo import evidences as ev_repo
from app.db.repo import reports as reports_repo
from app.db.repo import tasks as tasks_repo


def _ev(url: str) -> Evidence:
    return Evidence(
        evidence_id=evidence_id_for(url), url=url, title=url, snippet="x",
        full_text="x", captured_at="2026-01-01T00:00:00+00:00",
    )


def _report(
    report_id: str,
    *,
    mode: str = "quick",
    claims: int = 3,
    cross_validated: int = 3,
    evidences: int = 10,
    passed: bool = True,
    publishable: bool = False,
    brands: list[str] | None = None,
    write_rows: bool = True,
    **extra_metrics,
) -> None:
    """造一份报告。指标**逐项给**，不写默认的一整套。

    默认值挑的是"不相等"的那种组合（`passed=True` 配
    `publishable=False`、`claims=3` 配 `cross_validated=3`），
    这样一个把所有字段都照抄一遍的实现会立刻露馅。

    `write_rows=True` 时**同时写 `evidences` 条真实的证据行**挂在
    这份报告上。这不是顺手，是必须的：仪表盘的 `mentions` 读的是
    `evidences` 表的行数（与知识库同源），一份"自报用了 10 条"
    却没有 10 行数据的报告在库里是**不可能的**。

    我以前这里不写行，于是夹具处在一种库里不会出现的状态——
    而它在旧实现下也能跑绿，因为旧实现读的是报告自报的那个数。
    要造"自报数与行数不一致"这种分叉时，显式传 `write_rows=False`。
    """
    metrics = {
        "claims": claims,
        "crossValidatedClaims": cross_validated,
        "evidences": evidences,
        "totalCostUsd": 0.01,
        "totalTokens": 1000,
        "durationMs": 60000,
        "independentDomains": 7,
        "platformCount": 4,
        **extra_metrics,
    }
    task_id = f"TK-{report_id}"
    reports_repo.save(
        ReportRecord(
            report_id=report_id,
            task_id=task_id,
            query="对比 Notion 与 Obsidian",
            subject="Notion 与 Obsidian",
            mode=mode,
            brands=brands if brands is not None else ["Notion", "Obsidian"],
            data={"team": {"lead": ["L3-001"]}},
            metrics=metrics,
            quality={"passed": passed, "publishable": publishable},
        )
    )
    if write_rows:
        ev_repo.save_many(
            task_id,
            [_ev(f"https://example.com/{report_id}/e{index}") for index in range(evidences)],
            report_id=report_id,
        )


def _task(task_id: str, status: str = "done") -> None:
    tasks_repo.create(TaskRecord(task_id=task_id, query="x", status=status))


# ============================================================
# 三个口径
# ============================================================


def test_来源数与引用行数是两个数(client) -> None:
    """**第一对。** 造 2 行共享同一个 URL、1 行独有：3 行 → 2 条来源。

    抓的 bug：`coverage.sources` 直接用 `mentions` 那个数（或者反过来）。
    表现是侧边栏写"已沉淀 3 条来源"而知识库页写 2 条——
    同一个系统里两个数打架，用户不知道信哪个。
    """
    # `write_rows=False`：这个用例要的正是"报告自报的条数"与
    # "表里的行数"不一致的那种分叉，所以行由下面手写。
    # 自报 99、实写 3 —— 差得越远，读错口径的实现越藏不住。
    _report("RP-1", evidences=99, write_rows=False)
    ev_repo.save_many("TK-A", [_ev("https://example.com/shared")], report_id="RP-1")
    ev_repo.save_many(
        "TK-B",
        [_ev("https://example.com/shared"), _ev("https://example.com/solo")],
    )

    coverage = client.get("/api/dashboard").json()["coverage"]

    assert coverage["sources"] == 2, "来源数没有去重"
    assert coverage["mentions"] == 3, "引用行数不对"
    assert coverage["sources"] != coverage["mentions"], "两个口径撞在一起了"


def test_来源数与知识库接口一致(client) -> None:
    """**两个端点必须说同一个数。**

    抓的 bug：某个端点绕开共用函数、自己算一遍。这正是这一轮修掉的
    那个 bug 的形状——`coverage.mentions` 读的是"各报告自报之和"，
    而知识库读的是表里的行数。

    它现在守的是**接线**，不是 SQL：行数只有一个实现（`repo.count_rows()`，
    两个端点都调它），所以"两份 SQL 各自分叉"已经不可能发生了。
    剩下的失败方式是有人把某个端点改回去自己算——这条仍然抓得住，
    而且只有这条抓得住。
    """
    # 报告**自报 99 条**，实际只写进去 2 行。这个分叉是这个用例的全部要点：
    # 两者相等的话（比如自报 2、实写 2），"读表"和"读报告自报之和"
    # 两种实现给出同样的数，这条测试就分不出对错了——
    # 实测过：那种夹具下把实现改回"自报之和"，这条测试照样绿。
    _report("RP-1", evidences=99, write_rows=False)
    ev_repo.save_many(
        "TK-RP-1",
        [_ev("https://example.com/shared"), _ev("https://example.com/solo")],
        report_id="RP-1",
    )

    dashboard = client.get("/api/dashboard").json()["coverage"]
    facets = client.get("/api/evidences/facets").json()

    assert dashboard["sources"] == facets["total"] == 2
    assert dashboard["mentions"] == facets["mentions"] == 2, (
        "仪表盘的 mentions 与知识库的行数不是同一个口径——"
        "它读的应该是 evidences 表的行数（2），不是报告自报的那个数（99）"
    )


def test_通过质量门与达到可发布是两个判断(client) -> None:
    """**第二对。** 造一份过了门但没达到可发布的报告——这是最常见的状态，
    实测这台机器上 12 份报告全是这样（passed=12, publishable=0）。

    抓的 bug：把 `publishable` 也读成 `quality.get("passed")`。
    那样"达到可发布"永远等于"通过质量门"，两个数永远一样，
    而它们说的是两件事（证据够不够 vs 整篇有没有缺块）。
    """
    _report("RP-1", passed=True, publishable=False)
    _report("RP-2", passed=True, publishable=True)
    _report("RP-3", passed=False, publishable=False)

    runs = client.get("/api/dashboard").json()["runs"]

    assert runs["passed"] == 2
    assert runs["publishable"] == 1, "可发布被算成了通过质量门"
    assert runs["passRate"] == round(2 / 3, 4)


def test_分母是报告数不是任务数(client) -> None:
    """**第三对。** 3 份报告、7 个任务。

    抓的 bug：`avgEvidencesPerReport` 除以 `tasks`。这里 3 份报告
    各 10 条证据（合计 30），除以 3 是 10，除以 7 是 4.3——
    后者看起来完全正常，只是每个数都小了 60%。
    """
    for index in range(3):
        _report(f"RP-{index}", evidences=10)
    for index in range(7):
        _task(f"TK-{index}")

    payload = client.get("/api/dashboard").json()

    assert payload["runs"]["reports"] == 3
    assert payload["runs"]["tasks"] == 7, "任务数没算对"
    assert payload["coverage"]["avgEvidencesPerReport"] == 10.0, "分母用成了任务数"


def test_质量门通过率的分母是报告数(client) -> None:
    _report("RP-1", passed=True)
    _report("RP-2", passed=False)
    for index in range(5):
        _task(f"TK-{index}")

    assert client.get("/api/dashboard").json()["runs"]["passRate"] == 0.5


# ============================================================
# 交叉验证率：加权，不是平均
# ============================================================


def test_交叉验证率是加权平均不是逐份平均(client) -> None:
    """**这个文件里最要紧的一条。**

    两份报告的论点数量**刻意差 6 倍**（3 条 vs 18 条），
    而它们的交叉验证率一头一尾（1.0 vs 0.0）。

    - 加权：3 / 21 = 0.1429  ← 正确答案，它回答的是"所有结论里
      有多少条被交叉验证过"
    - 逐份平均：(1.0 + 0.0) / 2 = 0.5  ← **错的**，它让一份只有 3 条
      结论的报告和一份 18 条的权重相同

    抓的 bug：`sum(rates) / len(rates)`。这是最容易顺手写出来的版本，
    而且它给出的 0.5 比正确答案的 0.14 好看三倍——一个偏乐观的错数
    最不容易被人怀疑。
    """
    _report("RP-1", claims=3, cross_validated=3)
    _report("RP-2", claims=18, cross_validated=0)

    coverage = client.get("/api/dashboard").json()["coverage"]

    assert coverage["crossValidatedClaims"] == 3
    assert coverage["claims"] == 21
    assert coverage["crossValidationRate"] == round(3 / 21, 4), "交叉验证率不是加权的"
    assert coverage["crossValidationRate"] != 0.5, "算成了逐份报告求平均"


def test_交叉验证率的分子分母都对(client) -> None:
    """两个数都要给出来——只给比率的话没法复核。

    与上一条的区别是这条**只验分子分母**，上一条验的是算法。
    分开写是因为它们会各自坏掉：一个改了除法的实现，
    分子分母往往还是对的。
    """
    _report("RP-1", claims=4, cross_validated=1)
    _report("RP-2", claims=6, cross_validated=5)

    coverage = client.get("/api/dashboard").json()["coverage"]

    assert coverage["claims"] == 10
    assert coverage["crossValidatedClaims"] == 6
    assert coverage["crossValidationRate"] == 0.6


def test_没有报告时不除零(client) -> None:
    """新装的系统第一次打开竞争情报中心。

    抓的 bug：`mentions / report_count` 直接除。0 份报告时是
    `ZeroDivisionError`——表现是**首页能开、竞争情报中心 500**，
    而这一页恰是用户最先点开看的那几个之一。
    """
    payload = client.get("/api/dashboard").json()

    assert payload["runs"]["reports"] == 0
    assert payload["coverage"]["avgEvidencesPerReport"] == 0.0
    assert payload["coverage"]["crossValidationRate"] == 0.0
    assert payload["runs"]["passRate"] == 0.0
    assert payload["runs"]["avgDurationMs"] == 0
    assert payload["recent"] == []


# ============================================================
# 分档位与最近
# ============================================================


def test_按档位拆出来的份数加起来等于总数(client) -> None:
    """**这条守的是"拆完之后没有漏掉任何一份"。**

    抓的 bug：`by_mode` 用 `if/elif` 只认三个已知档位，
    遇到一个没登记过的档位（或者空字符串）就整份丢掉。
    表现是各档位份数之和小于报告总数，而**没有一个地方会报错**——
    表格看起来只是"少了一点"。
    """
    _report("RP-1", mode="quick")
    _report("RP-2", mode="quick")
    _report("RP-3", mode="deep")
    _report("RP-4", mode="expert")

    payload = client.get("/api/dashboard").json()

    assert payload["runs"]["reports"] == 4
    assert sum(bucket["reports"] for bucket in payload["byMode"]) == 4
    assert {bucket["mode"]: bucket["reports"] for bucket in payload["byMode"]} == {
        "quick": 2, "deep": 1, "expert": 1,
    }


def test_按档位的平均证据数用的是该档位自己的份数(client) -> None:
    """quick 两份共 40 条 → 平均 20；deep 一份 5 条 → 平均 5。

    抓的 bug：分母用报告总数。那样 quick 会显示 40/3 ≈ 13.3，
    两个档位的数字都被拉向彼此。
    """
    _report("RP-1", mode="quick", evidences=20)
    _report("RP-2", mode="quick", evidences=20)
    _report("RP-3", mode="deep", evidences=5)

    by_mode = {b["mode"]: b for b in client.get("/api/dashboard").json()["byMode"]}

    assert by_mode["quick"]["avgEvidences"] == 20.0
    assert by_mode["deep"]["avgEvidences"] == 5.0


def test_最近只给十条而且不带正文(client) -> None:
    """`recent` 给的是卡片，正文每份几百 KB。

    抓的 bug：`recent` 直接序列化整个 `ReportRecord`——`data` 里有
    216 条证据的全文，10 份就是几十 MB，而这一栏只显示一行标题。
    """
    for index in range(12):
        _report(f"RP-{index:02d}")

    recent = client.get("/api/dashboard").json()["recent"]

    assert len(recent) == 10, "条数不对"
    for item in recent:
        assert "data" not in item and "sections" not in item, "正文被带出来了"
        # `evidences` 在这里是**一个计数**，不是证据清单——名字一样但
        # 是两种东西，所以顺手钉住类型：它哪天变成列表，这一栏就会
        # 悄悄变成几十 MB。
        assert isinstance(item["evidences"], int) and not isinstance(item["evidences"], bool)
        assert {
            "reportId", "taskId", "query", "subject", "mode", "generatedAt",
            "evidences", "claims", "passed", "publishable",
        } <= set(item)


def test_品牌是去重后数出来的(client) -> None:
    """两份报告都覆盖 Notion：品牌数是 2，不是 4。

    抓的 bug：把每份报告的 `brands` 数相加。表现是"覆盖 4 个竞品"
    而实际上只有 2 个——这个数直接决定用户以为自己铺了多宽。
    """
    _report("RP-1", brands=["Notion", "Obsidian"])
    _report("RP-2", brands=["Notion", "Obsidian"])

    coverage = client.get("/api/dashboard").json()["coverage"]

    assert coverage["brands"] == 2
    assert coverage["brandNames"] == ["Notion", "Obsidian"]


def test_成本是把每份报告的加起来(client) -> None:
    """`totalCostUsd` 是各份报告 `metrics.totalCostUsd` 之和。

    抓的 bug：读成 `tasks` 或 `traces` 表里的成本。那两条路各自
    只在特定阶段有数，会漏掉一部分调用——而成本报小了没人会发现。
    """
    _report("RP-1", totalCostUsd=0.0123)
    _report("RP-2", totalCostUsd=0.0456)

    runs = client.get("/api/dashboard").json()["runs"]

    assert runs["totalCostUsd"] == round(0.0123 + 0.0456, 6)
    assert runs["totalTokens"] == 2000
