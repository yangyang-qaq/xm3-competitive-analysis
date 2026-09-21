"""`/api/reports`：列表、详情、导出、批注。

这个文件主要守一条**结构性的**决定：**列表不返回报告正文**。

这条决定的后果不是"慢一点"，而是"用久了会崩"：一份 quick 报告正文里有
216 条证据，每条带 `fullText`（实测整份 JSON 有几 MB）。列表页显示 50 份，
照搬正文就是几百 MB 的响应——而这个数字随使用时间自然增长，
不是某一天写错的代码。所以断言必须落在"`data` 根本不在响应里"，
而不是"响应小了一点"。
"""
from __future__ import annotations

import json
from urllib.parse import unquote

from app.db.repo import reports as reports_repo

VAGUE_QUERY = "帮我看看那个笔记软件"


# `client` 与 `report_id` 两个夹具在 `tests/conftest.py`：
# 深化那一组测试也要用它们。

# ============================================================
# 列表
# ============================================================


def test_列表不含报告正文(client, report_id) -> None:
    """**本文件的重点。** `data` 是整份报告正文（几 MB），
    列表接口的调用者不需要它。"""
    payload = client.get("/api/reports").json()

    assert payload["total"] >= 1
    item = payload["items"][0]
    assert "data" not in item, "列表把整份报告正文带出来了"
    assert "sections" not in item and "evidences" not in item
    assert item["reportId"] == report_id


def test_列表的卡片字段够渲染(client, report_id) -> None:
    """文库页要显示的每一个字段都在。

    缺字段的表现是界面上空一块，而**空一块不会让任何测试变红**——
    所以字段名要在这里逐个钉住。
    """
    item = client.get("/api/reports").json()["items"][0]

    assert {
        "reportId", "taskId", "query", "subject", "brands",
        "mode", "generatedAt", "metrics", "quality",
        "evidenceCount", "degradedCount",
    } <= set(item)
    assert item["taskId"], "报告与任务的关联丢了，点不进对应的任务"
    assert item["evidenceCount"] > 0
    assert item["quality"]["passed"] in (True, False)


def test_列表按时间倒序(client, report_id) -> None:
    payload = client.get("/api/reports").json()
    times = [item["generatedAt"] for item in payload["items"]]
    assert times == sorted(times, reverse=True)


def test_列表可以按档位过滤(client, report_id) -> None:
    assert client.get("/api/reports?mode=quick").json()["total"] >= 1
    assert client.get("/api/reports?mode=expert").json()["total"] == 0


def test_列表有上限(client) -> None:
    """`?limit=100000` 不该被接受——那是一个免费的拒绝服务入口。"""
    assert client.get("/api/reports?limit=100000").status_code == 422
    assert client.get("/api/reports?limit=0").status_code == 422


def test_空库返回空列表而不是报错(client) -> None:
    payload = client.get("/api/reports").json()
    assert payload == {"items": [], "total": 0, "limit": 20, "offset": 0}


# ============================================================
# 详情
# ============================================================


def test_详情带完整正文(client, report_id) -> None:
    payload = client.get(f"/api/reports/{report_id}").json()

    assert payload["reportId"] == report_id
    assert payload["data"]["sections"], "详情必须带正文"
    assert payload["data"]["evidences"]


def test_详情把批注一起给(client, report_id) -> None:
    """报告页一打开就要渲染批注，分两次拿的话中间那一帧是"没有批注"，
    用户刚写完的批注会先消失再出现。"""
    client.post(f"/api/reports/{report_id}/feedback", json={"content": "第一节太笼统"})

    payload = client.get(f"/api/reports/{report_id}").json()

    assert payload["feedbackCount"] == 1
    assert payload["feedback"][0]["content"] == "第一节太笼统"
    assert "feedback" in payload, "批注要跟详情一起给，不能让前端再发一个请求"


def test_不存在的报告是404(client) -> None:
    assert client.get("/api/reports/RP-nope").status_code == 404
    assert client.post("/api/reports/RP-nope/feedback", json={"content": "x"}).status_code == 404
    assert client.get("/api/reports/RP-nope/export").status_code == 404


# ============================================================
# 批注
# ============================================================


def test_批注永远新增不覆盖(client, report_id) -> None:
    """「人工修正率」的分子是这张表的**行数**。覆盖式写入的话，
    改三次同一个章节只留下一行，修正率永远是 0 或 1。"""
    for index in range(3):
        client.post(
            f"/api/reports/{report_id}/feedback",
            json={"content": f"第 {index + 1} 次批注", "sectionKey": "executive_summary"},
        )

    payload = client.get(f"/api/reports/{report_id}").json()
    assert payload["feedbackCount"] == 3
    assert len({item["feedbackId"] for item in payload["feedback"]}) == 3


def test_批注能挂到某一节也能挂到整份(client, report_id) -> None:
    client.post(
        f"/api/reports/{report_id}/feedback",
        json={"content": "这一节要重写", "sectionKey": "pricing"},
    )
    client.post(f"/api/reports/{report_id}/feedback", json={"content": "整体不错"})

    items = client.get(f"/api/reports/{report_id}").json()["feedback"]
    by_content = {item["content"]: item for item in items}

    assert by_content["这一节要重写"]["sectionKey"] == "pricing"
    assert by_content["整体不错"]["sectionKey"] == ""


def test_批注字段是驼峰(client, report_id) -> None:
    """**这条守的是"接口层有没有把数据库的行直接透传出去"。**

    透传的话前端读 `feedbackId` 拿到 `undefined`，然后什么都不显示——
    不报错、不告警，界面上只是少了一块。这个接口的其他字段
    （`reportId` / `sectionKey` / `generatedAt`）都是驼峰，
    只有批注是蛇形的话，不一致本身就是 bug 的来源。
    """
    client.post(f"/api/reports/{report_id}/feedback", json={"content": "x"})

    item = client.get(f"/api/reports/{report_id}").json()["feedback"][0]

    assert {
        "feedbackId", "reportId", "sectionKey", "kind", "content", "author", "createdAt",
    } == set(item), f"批注字段与接口约定不一致：{sorted(item)}"


def test_空批注被拒(client, report_id) -> None:
    """一条空批注会污染修正率：分子加一，"被改过"这件事却没发生。"""
    assert client.post(f"/api/reports/{report_id}/feedback", json={"content": ""}).status_code == 422


def test_批注响应里带回修正率(client, report_id) -> None:
    """写完批注要能立刻看到它对修正率的影响，否则前端得再查一次。"""
    payload = client.post(f"/api/reports/{report_id}/feedback", json={"content": "x"}).json()

    assert payload["count"] == 1
    rate = payload["correctionRate"]
    assert rate["annotatedReports"] >= 1
    assert set(rate) == {"reports", "annotatedReports", "correctionRate", "totalFeedbacks"}


# ============================================================
# 导出
# ============================================================


def test_导出默认是_README_格式(client, report_id) -> None:
    response = client.get(f"/api/reports/{report_id}/export")

    assert response.status_code == 200
    assert response.text.startswith("# ")
    assert "## 证据附录" in response.text


def test_导出_JSON_能解析(client, report_id) -> None:
    payload = json.loads(client.get(f"/api/reports/{report_id}/export?format=json").text)
    assert payload["sections"]
    assert "fullText" not in payload["evidences"][0], "默认不该带正文"


def test_导出_JSON_可以要全文(client, report_id) -> None:
    payload = json.loads(
        client.get(f"/api/reports/{report_id}/export?format=json&include_full_text=true").text
    )
    assert "fullText" in payload["evidences"][0]


def test_导出格式非法是422(client, report_id) -> None:
    assert client.get(f"/api/reports/{report_id}/export?format=pdf").status_code == 422


def test_导出文件名带报告主题(client, report_id) -> None:
    """右键另存时得到的是一串 report_id 的话，存下来的文件第二天就不认识了。

    主题是中文，所以名字在 `filename*` 里（RFC 6266 的百分号编码形式）。
    这条同时也是那个 500 的回归守卫：把中文直接拼进 `Content-Disposition`
    会让 `starlette` 抛 `UnicodeEncodeError`，而**每一份报告都是中文主题**。
    """
    response = client.get(f"/api/reports/{report_id}/export")

    assert response.status_code == 200, "中文主题把响应头写崩了"
    disposition = response.headers["content-disposition"]
    assert "inline" in disposition
    assert "filename*=UTF-8''" in disposition

    # `filename*` 里必须是**完整主题**，能原样解回来。
    # （ASCII 兜底名对纯中文主题是空的，那时退回 report_id——
    # 比一个所有下载都叫 `report.md` 的名字有用。）
    encoded = disposition.split("filename*=UTF-8''", 1)[1]
    assert unquote(encoded).endswith(".md")
    assert unquote(encoded) == f"{client.get(f'/api/reports/{report_id}').json()['data']['subject']}.md"


def test_导出文件名挡掉引号与换行(client, report_id) -> None:
    """报告主题来自模型输出。**不能假定它不含引号**——
    一个引号就能截断 `Content-Disposition` 的值。"""
    record = reports_repo.get(report_id)
    record.subject = '对比 "A" 与\nB'
    reports_repo.save(record)

    disposition = client.get(f"/api/reports/{report_id}/export").headers["content-disposition"]

    assert "\n" not in disposition
    assert disposition.count('"') == 2, f"引号没挡住：{disposition}"


def test_导出和详情渲染的是同一份东西(client, report_id) -> None:
    """两份实现的话，"页面上的"和"导出的"迟早不一样，
    而这件事没人会发现。"""
    detail = client.get(f"/api/reports/{report_id}").json()["data"]
    markdown = client.get(f"/api/reports/{report_id}/export").text

    for section in detail["sections"]:
        assert f"## {section['title']}" in markdown


def test_导出内容不含完整正文(client, report_id) -> None:
    """`fullText` 是 mock 里反复重复的填充段落。导出带上它的话，
    一份 markdown 会从几千字变成几万字，而读者要的是结论。"""
    markdown = client.get(f"/api/reports/{report_id}/export").text
    assert "正文长度被刻意拉长以通过正文质量检查" not in markdown
