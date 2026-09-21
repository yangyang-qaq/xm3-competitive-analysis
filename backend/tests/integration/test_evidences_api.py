"""`/api/evidences`：知识库列表与筛选面。

这个文件主要守一条**结构性的**决定：**列表不返回正文**
----------------------------------------------------
理由和报告列表那条不一样。报告列表省的是"整份 JSON 几 MB"，
这里的 `full_text` 单条只有几 KB——听起来不值得省。

值得，因为**这个接口读的是全表最大的那张表**，而知识库是要翻页的：
40 条 × 几 KB = 几百 KB，翻一次页读一遍。而列表卡片只显示 `snippet`，
正文一个字都不用。

还有一件这件事顺带保证了的事：**正文只能从报告页拿**。
`evidences` 表没有全局唯一的 id（主键是 `(evidence_id, task_id)`，
而 `evidence_id` 是 URL 的摘要，跨任务重复），所以"按 id 取详情"
这个接口不存在——取哪一行都是赌。这里把 `fullText` 挡在列表外，
是为了不给前端留一个"看起来能拿全文"的入口。
"""
from __future__ import annotations

from app.core.models import Evidence, evidence_id_for
from app.db.repo import evidences as ev_repo

SHARED = "https://example.com/shared"


def _ev(url: str, *, brand: str = "Notion", source_type: str = "news", cred: float = 60.0) -> Evidence:
    return Evidence(
        evidence_id=evidence_id_for(url), url=url, title=f"{url} 标题",
        snippet=f"{url} 摘要", full_text=f"{url} 正文", brand=brand,
        source_type=source_type, site_name="example.com",
        captured_at="2026-01-01T00:00:00+00:00", credibility=cred, query="对比 Notion 与 Obsidian",
    )


def _seed() -> None:
    """两次调研共享一条来源，另外各有一条独有的。

    形状是刻意的：只有共享的那条 `taskCount` 是 2，
    独有的是 1——一个把 `taskCount` 写死的实现会立刻露馅。
    """
    ev_repo.save_many("TK-A", [_ev(SHARED, cred=70.0), _ev("https://a.example.com/x", source_type="zhihu")])
    ev_repo.save_many("TK-B", [_ev(SHARED, cred=70.0), _ev("https://b.example.com/y", brand="Obsidian")])


# ============================================================
# 正文不出现在列表里
# ============================================================


def test_列表不含正文(client) -> None:
    """**本文件的重点。**"""
    _seed()

    payload = client.get("/api/evidences").json()

    assert payload["items"], "夹具没写进去"
    for item in payload["items"]:
        assert "fullText" not in item, "列表把正文带出来了"
        assert "full_text" not in item, "正文以蛇形键漏了出来"


def test_列表卡片字段够渲染(client) -> None:
    """知识库页要显示的每个字段都在。

    缺字段的表现是界面上空一块，而**空一块不会让任何测试变红**，
    所以字段名在这里逐个钉住。
    """
    _seed()

    item = client.get("/api/evidences").json()["items"][0]

    assert {
        "evidenceId", "url", "title", "snippet", "brand", "sourceType",
        "siteName", "publishedAt", "capturedAt", "credibility", "degraded", "taskCount",
    } <= set(item), f"字段与前端约定不一致：{sorted(item)}"
    assert item["taskCount"] >= 1


def test_一条来源一行(client) -> None:
    """接口这一层也要去重——`total` 是来源数，不是行数。

    抓的 bug：接口绕开 `library()` 直接用 `search()`（那个是**按行**的）。
    表现是知识库里同一篇文章出现两遍，而"知识库"这个词承诺的是沉淀。
    """
    _seed()

    payload = client.get("/api/evidences").json()

    assert payload["total"] == 3, "接口没有去重"
    assert len(payload["items"]) == 3
    assert len({item["url"] for item in payload["items"]}) == 3


def test_共享来源的引用次数露出来了(client) -> None:
    _seed()

    items = {item["url"]: item for item in client.get("/api/evidences").json()["items"]}

    assert items[SHARED]["taskCount"] == 2


# ============================================================
# 筛选与筛选面
# ============================================================


def test_筛选面与列表同口径(client) -> None:
    """**筛选面里的每个数都要点得进去。**

    这条走 HTTP 而不是直接调 repo：`routes_evidences` 里列表与筛选面
    是两次独立的调用，各接各的参数。一个只改了其中一处口径的实现，
    在 repo 层的测试里看不出来。
    """
    _seed()

    facets = client.get("/api/evidences/facets").json()
    listed = client.get("/api/evidences?limit=200").json()

    assert facets["total"] == listed["total"] == 3
    # 行数与来源数是两个口径，都要给：4 行 / 3 条来源
    assert facets["mentions"] == 4

    for bucket in facets["bySourceType"]:
        narrowed = client.get(f"/api/evidences?sourceType={bucket['value']}").json()
        assert narrowed["total"] == bucket["count"], f"{bucket['value']} 的选项数与实际结果不符"


def test_按来源类型筛选真的变窄(client) -> None:
    _seed()

    # `_seed()` 里 SHARED 与 b.example.com/y 都是 news、a.example.com/x 是 zhihu。
    # 写死这三个数（而不是 >= 1）是有意的：一个把 sourceType 当成
    # 前缀/模糊匹配的实现会让 zhihu 也命中 news 之外的东西。
    assert client.get("/api/evidences?sourceType=zhihu").json()["total"] == 1
    assert client.get("/api/evidences?sourceType=news").json()["total"] == 2
    assert client.get("/api/evidences?sourceType=nonexistent").json()["total"] == 0


def test_按品牌筛选(client) -> None:
    _seed()

    payload = client.get("/api/evidences?brand=Obsidian").json()

    assert payload["total"] == 1
    assert payload["items"][0]["brand"] == "Obsidian"


def test_搜索能搜到标题也能搜到摘要(client) -> None:
    """`q` 必须**同时**匹配 title 和 snippet。

    抓的 bug：漏掉 `OR snippet LIKE ?`。摘要里命中而标题里没有的来源
    会搜不出来，而用户往往是从摘要里记住那条内容的。

    夹具刻意让两条来源**分别只在一个字段里命中**：
    一条的关键词只在标题里、另一条的只在摘要里。这样"只搜标题"
    的实现会让第二条搜不到——如果两条的关键词在两个字段里都有，
    这个 bug 就测不出来了。
    """
    ev_repo.save_many("TK-A", [
        Evidence(
            evidence_id=evidence_id_for("https://only/title"),
            url="https://only/title", title="只在标题里的关键词", snippet="普通摘要",
            full_text="x", captured_at="2026-01-01T00:00:00+00:00",
        ),
        Evidence(
            evidence_id=evidence_id_for("https://only/snippet"),
            url="https://only/snippet", title="普通标题", snippet="只在摘要里的关键词",
            full_text="x", captured_at="2026-01-01T00:00:00+00:00",
        ),
    ])

    assert client.get("/api/evidences?q=只在标题里的关键词").json()["total"] == 1
    assert client.get("/api/evidences?q=只在摘要里的关键词").json()["total"] == 1, (
        "只搜了标题，没搜摘要"
    )
    assert client.get("/api/evidences?q=普通").json()["total"] == 2


def test_搜不到就是空列表不是报错(client) -> None:
    _seed()

    payload = client.get("/api/evidences?q=完全不存在的词").json()

    assert payload["items"] == [] and payload["total"] == 0


# ============================================================
# 分页与上限
# ============================================================


def test_有上限(client) -> None:
    """`?limit=100000` 不该被接受——那是一个免费的拒绝服务入口，
    而这个接口读的正是全表最大的那张表。"""
    assert client.get("/api/evidences?limit=100000").status_code == 422
    assert client.get("/api/evidences?limit=0").status_code == 422
    assert client.get("/api/evidences?offset=-1").status_code == 422


def test_翻页不重不漏(client) -> None:
    for index in range(5):
        ev_repo.save_many(f"TK-{index}", [_ev(f"https://example.com/p{index}")])

    first = client.get("/api/evidences?limit=2&offset=0").json()
    second = client.get("/api/evidences?limit=2&offset=2").json()

    assert len(first["items"]) == len(second["items"]) == 2
    assert {item["evidenceId"] for item in first["items"]} & {
        item["evidenceId"] for item in second["items"]
    } == set()
    assert first["total"] == second["total"] == 5, "total 不该随翻页变化"


def test_空库返回空而不是报错(client) -> None:
    """新用户第一次点开知识库就是这条路径。

    抓的 bug：`library_facets()` 里那几个 `fetchone()` 在空表上返回 None，
    没兜底就 `TypeError`——表现是**全新装的系统一打开知识库页就 500**。
    """
    payload = client.get("/api/evidences").json()

    assert payload == {"items": [], "total": 0, "limit": 40, "offset": 0}
    assert client.get("/api/evidences/facets").json() == {
        "total": 0, "mentions": 0, "bySourceType": [], "byBrand": [],
    }
