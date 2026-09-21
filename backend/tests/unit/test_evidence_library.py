"""知识库的去重层：`library()` / `count_library()` / `library_facets()`。

这个文件守的是**一个数**：知识库说"沉淀了 266 条来源"，
而同一个库里有 2426 行证据。差的 9 倍全是同一篇文章被历次调研反复采到。

为什么这件事值得一个专门的测试文件
--------------------------------
`evidence_id` 是 **URL 的摘要**（见 `models.evidence_id_for`），
所以它**不是全局唯一的**——同一个 URL 在 N 次调研里就是同一个 id、
N 行记录。表的真实主键是 `(evidence_id, task_id)`。

于是"一行"和"一条来源"在这个表里是两个不同的东西，而且它们
**永远不相等**（跑了两次调研就 2:1）。任何一处把行数当成来源数、
或者只写对了一处，表现都是页面上一个偏大的数字——
偏大不会报错、不会崩，只是让人以为自己的证据池比实际深 9 倍。
所以这里的断言都落在**两个口径的具体数值**上，而不是"返回了非空列表"。

去重是安全的（所以可以放心 GROUP BY）：同一个 evidence_id 的
正文、评分、来源类型、域名、发布时间实测完全一致，因为它们都是
从 URL 和抓回来的正文算出来的，与哪次调研无关。
"""
from __future__ import annotations

from app.core.models import Evidence, evidence_id_for
from app.db.repo import evidences as ev_repo

SHARED_URL = "https://example.com/shared-report"
ONLY_IN_A = "https://a.example.com/only-a"
ONLY_IN_B = "https://b.example.com/only-b"


def _ev(url: str, *, brand: str = "Notion", source_type: str = "news", cred: float = 60.0,
        title: str = "", site: str = "") -> Evidence:
    return Evidence(
        evidence_id=evidence_id_for(url),
        url=url,
        title=title or url,
        snippet=f"{url} 的摘要",
        full_text=f"{url} 的正文",
        brand=brand,
        source_type=source_type,
        site_name=site or "example.com",
        captured_at="2026-01-01T00:00:00+00:00",
        credibility=cred,
        query="对比 Notion 与 Obsidian",
    )


def _two_runs_sharing_one_source() -> None:
    """两次调研：A 采到 {共享, 只在A}，B 采到 {共享, 只在B}。

    造这个形状而不是"两次采到完全一样的两条"是有意的：那样的话
    `taskCount` 恒等于 2，一个把 taskCount 写死成 2 的实现也能过。
    这里共享的那条是 2、独有的两条是 1，三种值都出现过。
    """
    ev_repo.save_many(
        "TK-A",
        [_ev(SHARED_URL, cred=70.0), _ev(ONLY_IN_A, brand="Obsidian", source_type="zhihu")],
        report_id="RP-A",
    )
    ev_repo.save_many(
        "TK-B",
        [_ev(SHARED_URL, cred=80.0), _ev(ONLY_IN_B, brand="Notion", source_type="review")],
        report_id="RP-B",
    )


# ============================================================
# 去重本身
# ============================================================


def test_四次写入三次调研只沉淀三条来源(mock_pipeline_db) -> None:
    """**本文件的重点。** 4 行 → 3 条来源。

    抓的 bug：`library()` 少写 `GROUP BY evidence_id` 时它变成
    `SELECT ... FROM evidences`，返回 4 条——其中两条是同一篇文章。
    用户看到的是一份"知识库"里有两张一模一样的卡片，
    而他没有任何办法判断这是不是重复。
    """
    _two_runs_sharing_one_source()

    items = ev_repo.library(limit=50)

    assert len(items) == 3, "共享来源没有被合并"
    assert len({item["url"] for item in items}) == 3, "同一篇文章出现了两份"


def test_共享的那条被标成两次调研(mock_pipeline_db) -> None:
    """`taskCount` 是知识库区别于证据流的那一列，而且它是**数出来的**。

    抓的 bug：把 `COUNT(DISTINCT task_id)` 写成 `COUNT(*)`。在这个夹具里
    两者恰好都等于 2（共享来源每个任务只有一行），所以断言必须
    同时看独有来源的 1——一个 `COUNT(*)` 的实现会让它们也变成 1，
    但一个**写死成常量**的实现会立刻露馅。
    """
    _two_runs_sharing_one_source()

    by_url = {item["url"]: item for item in ev_repo.library(limit=50)}
    assert by_url[SHARED_URL]["taskCount"] == 2
    assert by_url[ONLY_IN_A]["taskCount"] == 1
    assert by_url[ONLY_IN_B]["taskCount"] == 1


def test_同一任务里重采同一来源不会让来源数膨胀(mock_pipeline_db) -> None:
    """返工重新抓到同一条 URL 时，是**覆盖**那一行，不是新增一行。

    这是正常路径不是异常：`save_many` 的注释写着"新的正文与评分应当胜出"，
    而表的主键是 `(task_id, evidence_id)`，所以同一个任务里同一条 URL
    只可能有一行。

    **这条测试刻意不声称它守住了 `COUNT(DISTINCT task_id)` 里的 DISTINCT。**
    我本来是那么写的，突变校验证明那是假的：那个 DISTINCT 在这个 schema 下
    **无法被证伪**——主键已经让"同一任务同一来源两行"成为不可能的状态，
    于是 `COUNT(DISTINCT task_id)` 恒等于 `COUNT(*)`。两个实现跑出来一样，
    任何测试都分不出高下。

    所以这里退回去测**能测的那件事**：重采不膨胀、且新的评分胜出。
    代码里那个 DISTINCT 作为防御留着（写明了它防的是什么），
    但不能有一条测试假装它在守——那条测试会永远绿，
    而永远绿的测试比没有测试更坏。
    """
    _two_runs_sharing_one_source()
    before = {item["url"]: item for item in ev_repo.library(limit=50)}

    # 返工：同一个任务、同一条 URL、更高的可信度
    ev_repo.save_many("TK-A", [_ev(SHARED_URL, cred=95.0, title="重采后的标题")], report_id="RP-A")

    after = {item["url"]: item for item in ev_repo.library(limit=50)}

    assert len(after) == len(before) == 3, "重采让来源多出了一条"
    assert after[SHARED_URL]["taskCount"] == 2, "重采把调研次数算多了"
    assert after[SHARED_URL]["credibility"] == 95.0, "重采后的新评分没有胜出"


# ============================================================
# 三个口径必须一致
# ============================================================


def test_计数与列表是同一个数(mock_pipeline_db) -> None:
    """`count_library()` 与 `library()` 必须同口径。

    抓的 bug：分页器用 `count_library()` 算页数、列表用 `library()` 取数，
    两者口径不同的话，最后一页会是空的（或者更糟：显示"共 2426 条"
    而实际翻到第 2 页就没了）。断言写成"计数 == 把所有行取回来的长度"，
    这样一个改成 COUNT(*)、另一个没改，立刻红。
    """
    _two_runs_sharing_one_source()

    assert ev_repo.count_library() == len(ev_repo.library(limit=1000)) == 3


def test_筛选面与列表也是同一个数(mock_pipeline_db) -> None:
    """`library_facets()` 的每一项都必须能点得进去。

    抓的 bug：筛选面按**行数**算、列表按**去重来源**算。这是最容易犯的
    一个——`facets()`（按行的那份）就在同一个文件里，改错了会
    "选项写着 2 条、点进去 1 条"。所以这里逐项验：
    每个来源类型桶的计数，都要等于按它筛出来的列表长度。
    """
    _two_runs_sharing_one_source()

    facets = ev_repo.library_facets()
    assert facets["total"] == ev_repo.count_library() == 3
    assert sum(bucket["count"] for bucket in facets["bySourceType"]) == 3

    for bucket in facets["bySourceType"]:
        assert bucket["count"] == ev_repo.count_library(source_type=bucket["value"])
        assert bucket["count"] == len(ev_repo.library(source_type=bucket["value"], limit=1000))

    for bucket in facets["byBrand"]:
        assert bucket["count"] == ev_repo.count_library(brand=bucket["value"])


def test_行数与来源数必须同时给出且不相等(mock_pipeline_db) -> None:
    """`mentions`（行）与 `total`（来源）是**两个数**，差 4:3。

    抓的 bug：把 `mentions` 也写成 `COUNT(DISTINCT evidence_id)`——
    那样"266 条来源，被引用 2426 次"会变成"266 条来源，被引用 266 次"，
    页面上的两个数一模一样，读起来像是重复渲染。
    这条同时钉住了**它们在这份夹具里确实不同**：
    如果哪天去重口径坏了、两者恰好相等，这条会红。
    """
    _two_runs_sharing_one_source()

    facets = ev_repo.library_facets()

    assert facets["mentions"] == 4, "行数不是表里的真实行数"
    assert facets["total"] == 3
    assert facets["mentions"] != facets["total"], "行数与来源数撞在一起了，说明有一个口径写错了"


# ============================================================
# 排序与翻页
# ============================================================


def test_用得多的排在前面(mock_pipeline_db) -> None:
    """知识库按**复用次数**排序，不按可信度。

    抓的 bug：排序键写回 `credibility DESC`。那样一条 95 分但没人用过的
    来源会排在一条 80 分、被 11 次调研采到的前面——而知识库的全部价值
    就是后者。夹具里让共享那条可信度**更低**（70/80 对独有的 60），
    所以按可信度排会得到相反的顺序，这条测试会红。
    """
    ev_repo.save_many("TK-A", [_ev(SHARED_URL, cred=50.0)], report_id="RP-A")
    ev_repo.save_many("TK-B", [_ev(SHARED_URL, cred=50.0)], report_id="RP-B")
    ev_repo.save_many("TK-C", [_ev(ONLY_IN_A, cred=99.0)], report_id="RP-C")

    items = ev_repo.library(limit=50)

    assert items[0]["url"] == SHARED_URL, "高分但只用过一次的排到了复用两次的前面"


def test_翻页不重不漏(mock_pipeline_db) -> None:
    """两页取回来的并集 = 全集，交集 = 空。

    抓的 bug：`LIMIT/OFFSET` 与 `GROUP BY` 的配合写错——把参数按
    `(*params, offset, limit)` 的次序传进去（两个都是 int，SQL 照收），
    于是 `limit=3, offset=0` 变成 `LIMIT 0 OFFSET 3`，第一页直接是空的。
    这个错误不会报错，只会让知识库"翻不到东西"。

    **同样不声称守住了排序里那个 `evidence_id` tiebreaker。**
    突变校验证明去掉它测试照样绿：`GROUP BY evidence_id` 在 SQLite 里
    本来就是按分组键有序输出的，所以最后那个 `evidence_id` 是冗余的。
    它是防御（防的是查询计划换一套实现），不是一条测试能守的东西。
    """
    for index in range(6):
        ev_repo.save_many(f"TK-{index}", [_ev(f"https://example.com/p{index}")])

    first = ev_repo.library(limit=3, offset=0)
    second = ev_repo.library(limit=3, offset=3)

    assert len(first) == len(second) == 3
    assert {item["evidenceId"] for item in first} & {item["evidenceId"] for item in second} == set()
    assert len({item["evidenceId"] for item in first + second}) == 6


def test_空库返回空而不是报错(mock_pipeline_db) -> None:
    """`mock_pipeline_db` 建了表但没写数据。

    抓的 bug：`library_facets()` 里那几个 `fetchone()` 在空表上返回 None，
    没兜底的话 `.fetchone()["n"]` 会 `TypeError`——表现是
    **全新的库一打开知识库页就 500**，而那正是每个新用户的第一屏。
    """
    assert ev_repo.library(limit=50) == []
    assert ev_repo.count_library() == 0
    assert ev_repo.library_facets() == {
        "total": 0, "mentions": 0, "bySourceType": [], "byBrand": [],
    }


# ============================================================
# 取数改成两步之后，值必须一个不差
#
# `_library_rows` 原来是一条 SQL，压测里发现它在 48,384 行的表上要 338~535 ms
# ——因为它把 `MIN(full_text)` 这种宽列的聚合做在**整张表**上，而最后只要 30 组。
# 改成"先排窄列，再回表取那 30 组"之后快了一倍左右。
#
# 提速不是价值，**提速之后结果没变**才是。所以下面这条拿改写的版本
# 与改写前那条 SQL 逐行逐列比对。
# ============================================================


def _naive_library_rows(
    *, brand: str = "", source_type: str = "", min_credibility: float = 0.0,
    domain: str = "", text: str = "", limit: int = 40, offset: int = 0,
) -> list[dict]:
    """**改写前**的那一条 SQL。作为参照物，不是"另一份实现"。

    故意留在这里而不是删掉：改写值不值，靠的就是能拿它比。
    """
    from app.db.connection import get_conn

    where, params = ev_repo._filters(brand, source_type, min_credibility, None, domain, text)
    sql = (
        f"SELECT {', '.join(ev_repo._LIBRARY_COLUMNS)} FROM evidences {where} "
        "GROUP BY evidence_id "
        "ORDER BY task_count DESC, credibility DESC, evidence_id LIMIT ? OFFSET ?"
    )
    return [dict(row) for row in get_conn().execute(sql, (*params, limit, offset)).fetchall()]


def _seed_corpus() -> None:
    """6 个来源，被采到的次数分别是 1..6 次，且可筛选的字段各不相同。

    **可信度刻意让它随出现次数变化**（`50.0 + run`）。真实数据里
    同一个 `evidence_id` 的可信度是一致的（都是从同一个 URL 和正文算出来的），
    所以 `MIN`/`MAX` 取哪个都一样——那样就**测不出**两步之间的分歧了。
    这里造出一个现实里不该出现的形状，正是为了让"第一步的 `MAX(credibility)`
    与第二步的 `MAX(credibility)` 算的是同一批行"这件事被真的验证一次：
    `min_credibility` 会切掉一部分出现，切错了两边就对不上。
    """
    for index in range(6):
        url = f"https://example.com/s{index}"
        for run in range(index + 1):
            ev_repo.save_many(
                f"TK-{index}-{run}",
                [
                    _ev(
                        url,
                        brand="Notion" if index % 2 else "Obsidian",
                        source_type="news" if index % 3 else "blog",
                        cred=50.0 + run,
                        title=f"标题 {index}",
                        site=f"s{index}.example.com",
                    )
                ],
                report_id=f"RP-{index}-{run}",
            )


def test_两步改写与改写前那条SQL逐列一致(mock_pipeline_db) -> None:
    """`_library_rows` 与参照物，在各种筛选与翻页下逐行逐列相等。

    比对的是**整个字典**，不是"id 集合相同"：宽列（`full_text`、`images`）
    走的正是第二条查询，只比 id 的话那一步取错了列也发现不了。
    """
    _seed_corpus()

    # `_library_rows` 的每个参数都是必填的关键字参数，所以用例只写改动项，
    # 其余从 BASE 补齐。写成完整字典的话，加一个筛选参数就要改十几处。
    base = {
        "brand": "", "source_type": "", "min_credibility": 0.0,
        "domain": "", "text": "", "limit": 40, "offset": 0,
    }
    cases: list[dict] = [
        {},                                            # 无筛选
        {"limit": 2},                                  # 只取前两行
        {"limit": 2, "offset": 2},                     # 第二页
        {"limit": 2, "offset": 4},                     # 末页
        {"limit": 100},                                # 比总数大的 limit
        {"offset": 100},                               # 越界的 offset
        {"brand": "Notion"},
        {"brand": "Obsidian"},
        {"source_type": "blog"},
        {"min_credibility": 53.0},                     # 切掉一部分"出现"
        {"min_credibility": 60.0},                     # 切到只剩来源 5
        {"domain": "s3.example.com"},
        {"text": "标题 3"},
        {"brand": "Notion", "source_type": "news", "min_credibility": 52.0},
    ]

    for case in cases:
        kwargs = {**base, **case}
        got = [dict(row) for row in ev_repo._library_rows(**kwargs)]
        want = _naive_library_rows(**kwargs)
        assert got == want, f"两步取数与参照物不一致，参数：{case}"


def test_两步取数在越界翻页上返回空(mock_pipeline_db) -> None:
    """`offset` 越过总数时第一步就没有 id，第二条查询不该再发出去。

    没有这个早退的话，会拼出一条 `evidence_id IN ()` —— SQLite 语法错误，
    表现是知识库翻到最后一页之后再点"下一页"直接 500。
    """
    _seed_corpus()

    assert ev_repo.library(limit=10, offset=999) == []
