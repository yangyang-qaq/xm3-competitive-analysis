"""`/api/experts`：48 人名册 + 参与度。

这个文件守的核心是**一件事**：名册页上那个数字必须是量出来的
----------------------------------------------------------------
名册自带的 `stats` 里每个数都是 0，而 `source == "seed"` 标记着
"还没被量过"（后端 `Expert` 的注释就是这么写的）。
把 0 当实测值印出来，用户看到的是一整页"参与 0 次调研"的专家——
看起来像系统坏了，或者像这 48 个人从来没干过活。

所以每个专家身上唯一真实的数字是 `participation`（被派进过多少份报告），
它从报告正文的 `team` 字段数出来。这个文件钉住它**真的来自报告**、
以及**筛选不会让它错位**。

另外守住"页头那个 48 不随筛选变化"——用 `total` 当页头的话，
用户筛了"战略层"之后页头会变成"9 位专家"，
而这一页要说的恰恰是"这个公会有 48 个人"。
"""
from __future__ import annotations

from app.core.models import ReportRecord
from app.data.loader import load_experts
from app.db.repo import reports as reports_repo

ROSTER = 48


def _save_team(report_id: str, team: dict) -> None:
    reports_repo.save(
        ReportRecord(
            report_id=report_id, task_id=f"TK-{report_id}",
            query="对比 Notion 与 Obsidian", subject="Notion 与 Obsidian",
            data={"team": team},
        )
    )


# ============================================================
# 名册
# ============================================================


def test_名册是四十八人(client) -> None:
    payload = client.get("/api/experts").json()

    assert payload["rosterSize"] == ROSTER
    assert payload["total"] == ROSTER
    assert len(payload["items"]) == ROSTER


def test_页头的四十八不随筛选变化(client) -> None:
    """**这条守的是页头那句话说的事。**

    抓的 bug：`rosterSize` 用 `len(items)` 算。那样筛了"战略层"之后
    页头会写"9 位专家"——而这一页要讲的是这个公会有多少人。
    `total` 该跟着筛选变，`rosterSize` 不该。
    """
    filtered = client.get("/api/experts?level=L2").json()

    assert filtered["total"] == 9, "战略层应该是 9 个人"
    assert filtered["rosterSize"] == ROSTER, "页头的人数被筛选带偏了"


def test_三层人数是三九三十六(client) -> None:
    """3 / 9 / 36 是名册的构成，由 `test_experts.py` 从另一个角度守着。
    这里守的是**接口把它算对了并给出来了**。"""
    by_level = {bucket["level"]: bucket["count"] for bucket in client.get("/api/experts").json()["byLevel"]}

    assert by_level == {"L3": 3, "L2": 9, "L1": 36}


def test_层级按决策到执行排序(client) -> None:
    """L3 → L2 → L1，**不是 L1 → L2 → L3**。

    抓的 bug：直接对 level 字符串排序。字典序下 L1 在最前，
    而界面上的含义是"决策 → 战略 → 执行"——排反了会让决策层
    排在执行层后面，读起来是"最底层的人在最上面"。
    这个顺序是业务含义，不是名册里恰好那个次序。
    """
    levels = [bucket["level"] for bucket in client.get("/api/experts").json()["byLevel"]]

    assert levels == ["L3", "L2", "L1"]


def test_名册里每个人的条目字段齐全(client) -> None:
    """48 张卡片都要能渲染。

    抓的 bug：某个专家缺 `skills` 或 `avatarColor`，前端那一格空白。
    空白不报错，所以字段名在这里逐个钉住。
    """
    item = client.get("/api/experts").json()["items"][0]

    assert {
        "expertId", "level", "levelLabel", "group", "name", "roleTitle", "oneLiner",
        "skills", "knowledgeBase", "knowledgeTags", "badgeColor", "avatarColor",
        "domainIcon", "stats", "participation",
    } <= set(item), f"字段与前端约定不一致：{sorted(item)}"
    assert item["expertId"] and item["name"] and item["roleTitle"]


def test_淘汰默认的种子统计不会被当成实测值(client) -> None:
    """`stats.source` 必须还是 `seed`，前端据此决定不显示那几个 0。

    抓的 bug：某天有人把 `expert_stats` 表接上（它现在是空的），
    却没把 `source` 改掉——前端会继续隐藏真实数据，
    或者反过来把种子里的 0 当实测值印出来。
    """
    for item in client.get("/api/experts").json()["items"]:
        assert item["stats"].get("source") == "seed", (
            f"{item['expertId']} 的 stats.source 变了，前端的显示逻辑要跟着改"
        )


# ============================================================
# 参与度
# ============================================================


def test_参与度来自报告正文(client) -> None:
    """**本文件的重点。** 名册里被派进过报告的人，参与度就是那份报告数。

    抓的 bug：从 `expert_stats` 表读（那张表 0 行、没有任何代码写它）
    ——表现是**所有人的参与度恒为 0**，而页面上看起来"这套系统还没用过"。
    这条测试让参与度必须真的从 `reports.data` 数出来。
    """
    target = load_experts()[0].expert_id
    _save_team("RP-1", {"lead": [target], "executors": ["L1-001"]})
    _save_team("RP-2", {"lead": [target]})

    items = {item["expertId"]: item for item in client.get("/api/experts").json()["items"]}

    assert items[target]["participation"] == 2, "参与度不是从报告里数出来的"
    assert items["L1-001"]["participation"] == 1
    assert items[load_experts()[-1].expert_id]["participation"] == 0


def test_没跑过调研时全员为零(client) -> None:
    """空库上每个人都是 0——这是对的，因为确实一次都没跑过。

    这条与上一条**成对**：只有上一条的话，一个"给所有人填 1"
    的实现也能过；只有这一条的话，一个恒为 0 的实现也能过。
    """
    items = client.get("/api/experts").json()["items"]

    assert all(item["participation"] == 0 for item in items)


# ============================================================
# 筛选
# ============================================================


def test_按层级筛选(client) -> None:
    payload = client.get("/api/experts?level=L3").json()

    assert payload["total"] == 3
    assert {item["level"] for item in payload["items"]} == {"L3"}


def test_按分组筛选(client) -> None:
    """分组名从名册里取一个真实的，而不是编一个——编的话筛出来是空的，
    而"空"和"这个分组不存在"分不出来。"""
    group = load_experts()[0].group

    payload = client.get(f"/api/experts?group={group}").json()

    assert payload["total"] >= 1
    assert {item["group"] for item in payload["items"]} == {group}


def test_两个筛选可以叠加(client) -> None:
    expert = load_experts()[0]

    payload = client.get(f"/api/experts?level={expert.level}&group={expert.group}").json()

    assert all(
        item["level"] == expert.level and item["group"] == expert.group
        for item in payload["items"]
    )


def test_筛不到就是空列表不是报错(client) -> None:
    assert client.get("/api/experts?level=L9").json()["items"] == []
    assert client.get("/api/experts?group=不存在的组").json()["items"] == []


def test_分组筛选面覆盖全名册(client) -> None:
    """`byGroup` 的计数之和 = 48。

    抓的 bug：`byGroup` 从**过滤后**的名单里算。那样点了"战略层"之后
    分组选项里就只剩战略层那几个人，用户再也点不回全部分组。
    """
    payload = client.get("/api/experts?level=L2").json()

    assert sum(bucket["count"] for bucket in payload["byGroup"]) == ROSTER


# ============================================================
# 详情
# ============================================================


def test_详情给出参与过的报告(client) -> None:
    """名册页点开一张卡片，最常问的下一句是"他都参与过哪几次"。"""
    target = load_experts()[0].expert_id
    _save_team("RP-1", {"lead": [target]})
    _save_team("RP-2", {"executors": ["L1-001"]})

    payload = client.get(f"/api/experts/{target}").json()

    assert payload["participation"] == 1
    assert len(payload["reports"]) == 1, "把没参与过的报告也算进去了"
    assert payload["reports"][0]["reportId"] == "RP-1"
    assert {"reportId", "taskId", "query", "subject", "generatedAt"} <= set(payload["reports"][0])


def test_返回的报告确实有他(client) -> None:
    """**这条是上面那条的反面**：参与列表的判据必须是"他在 team 里"，
    而不是"这份报告存在"。

    抓的 bug：详情页图省事，把最近 200 份报告全列出来。
    表现是每个人的"参与记录"都长得一模一样，而且都等于全部报告。
    """
    target = load_experts()[0].expert_id
    _save_team("RP-1", {"lead": [target]})
    _save_team("RP-2", {"lead": ["L3-999"]})

    payload = client.get(f"/api/experts/{target}").json()

    assert [item["reportId"] for item in payload["reports"]] == ["RP-1"]


def test_不存在的专家是404(client) -> None:
    response = client.get("/api/experts/L9-999")

    assert response.status_code == 404
    assert "L9-999" in response.json()["detail"], "错误信息要说清是哪个 id"
