"""HTTP 层冒烟测试：应用能装配、路由能响应。

⚠️ 这个文件**刻意不挂 `mock_pipeline_db`**
--------------------------------------
它的目的就是"用最少的假设把应用装起来"——挂上夹具会让它测的是
"夹具装出来的应用"，而不是"应用"。但代价是它曾经直接摸到生产库：
`TestClient(app)` 一进 `with` 就跑 lifespan，lifespan 会判定所有
非终态任务失败，于是**每跑一次这个文件，真实库里正在跑的任务全死**
（见 `问题记录.md` 问题 28）。

现在这件事由 `conftest.py` 的 `_never_touch_production_db` 兜住——
autouse，默认把 `DB_PATH` 指到临时目录。所以这个文件可以继续
"不挂夹具"，而不会碰到 `backend/data/xm3.db`。

下面最后那条 `test_测试里构造的应用永远不指向生产库` 就是守它的。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app


def test_health_returns_ok() -> None:
    with TestClient(create_app()) as client:
        resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["llm_provider"]
    assert body["search_provider"]


def test_health_exposes_no_secrets() -> None:
    """无鉴权接口，任何 key 片段都不能出现在响应里。"""
    with TestClient(create_app()) as client:
        raw = client.get("/health").text

    assert "sk-" not in raw
    assert "api_key" not in raw.lower()


def test_测试里构造的应用永远不指向生产库(tmp_path) -> None:
    """**这条守的是 conftest 里那句 `DB_PATH`。**

    它是这个文件里唯一一条不是为了测 `/health` 而存在的用例。

    抓的 bug：构造应用就会跑 lifespan，而 lifespan 会调
    `interrupt_unfinished()`——把库里所有非终态任务判失败。它写的是
    `get_settings().db_file`，默认值是 `backend/data/xm3.db`。

    实测过的危害（不是推理）：往真库插一条 `running` 的金丝雀任务，
    跑一次 `pytest tests/integration/test_health_api.py`，
    金丝雀变成 `failed` + "后端进程重启时这个任务还没跑完…"。
    而真库上此刻有用户正在等的结果——**跑测试等于把用户的任务杀掉**。

    断言写成"父目录必须是本用例的 tmp_path"而不是"不等于生产路径"：
    前者换个环境（比如 CI 上有人导出了 `DB_PATH`）也不会被满足，
    后者会被一个恰好指向别处的环境变量蒙混过去。
    """
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200

    db_file = Path(get_settings().db_file)

    assert db_file.parent == tmp_path, (
        f"测试进程摸到了 {db_file}。构造应用就会跑 lifespan，"
        "而 lifespan 会把库里所有非终态任务判失败——"
        "等于每跑一次测试就把真库里正在跑的任务杀掉。"
        "见 conftest.py 的 `_never_touch_production_db`。"
    )
