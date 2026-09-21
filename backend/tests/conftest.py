"""全局测试夹具。

四份进程级状态都要在用例之间清掉
--------------------------------
`Settings` 是 `lru_cache` 的、provider 是模块级 dict 缓存的、journal 与
runner 是模块级 dict 缓存的任务、mock 的证据池是模块级列表。不清理的话，
前一个用例的实例会被后一个用例复用：`monkeypatch.setenv` 设的环境变量对
缓存住的 Settings 无效，前一个任务的事件会出现在后一个任务的订阅里，
前一个任务采到的证据 id 会被后一个任务的论点引用。表现是"单独跑绿、
一起跑红"，而且红得毫无规律，最难查的一类失败。

这四者是同一类东西（进程级状态），所以用同一个 autouse 夹具收口，
而不是让每个用例自己记得清——`reset_mock_state` 的 docstring 里写着
"测试之间必须调用"，而在有这条夹具之前**没有任何地方调用它**，
于是 mock 的确定性在不同执行顺序下并不成立。
"要记得清"的约定等于没约定。
"""
from __future__ import annotations

import os

import pytest

from app.core.config import get_settings
from app.core.observability.events import reset_journals
from app.core.pipeline.runner import reset_runners
from app.providers import registry
from app.providers.mock import reset_mock_state


@pytest.fixture(autouse=True)
def _isolate_process_state():
    get_settings.cache_clear()
    registry.reset_providers()
    reset_runners()
    reset_journals()
    reset_mock_state()
    yield
    get_settings.cache_clear()
    registry.reset_providers()
    reset_runners()
    reset_journals()
    reset_mock_state()


@pytest.fixture(autouse=True)
def _never_touch_production_db(tmp_path, monkeypatch):
    """**每一条测试的库都落在临时目录里，包括那些没用夹具的。**

    这条夹具是补的一个洞，洞的形状值得记下来（问题 28）：
    `TestClient(app)` 一进 `with` 就跑应用的 lifespan，而 lifespan 会
    调 `tasks_repo.interrupt_unfinished()`——**把库里所有非终态任务判失败**。
    它写的是 `get_settings().db_file`，而那个默认值是
    `backend/data/xm3.db`，也就是**生产库**。

    于是 `test_health_api.py`（唯一一个没挂 `mock_pipeline_db` 的文件）
    每跑一次，就把真实库里正在跑的任务全部判死，失败原因写着
    "后端进程重启时这个任务还没跑完"。实测过：往真库插一条 `running`
    的金丝雀，跑一次那个文件，金丝雀变成 `failed`。

    危害不是"测试脏了数据"，是**开发时每跑一次测试，用户正在跑的
    任务就死一次**——而现象（任务失败）与原因（跑测试）之间没有任何
    可见的联系。

    为什么不给 `test_health_api.py` 单独补夹具就完事
    ----------------------------------------------
    因为那是一句"要记得加"的约定，而这个仓库已经栽过一次：
    `reset_mock_state()` 的 docstring 写着"测试之间必须调用"，
    而在有那条 autouse 夹具之前**没有任何地方调用它**（见本文件开头）。
    "记得加"等于没有。所以这里做成默认安全：谁都够不到生产库，
    除非它显式把 DB_PATH 指过去。

    由 `test_health_api.py::test_测试里构造的应用永远不指向生产库` 守着：
    把下面那行 `DB_PATH` 删掉，它就红。
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "xm3-default.db"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """把本机 `backend/.env` 里的 provider 凭据从进程环境里摘掉。

    **这条夹具的存在是为了让"本机绿"和"CI 绿"是同一句话。**

    `config.py` 在模块导入时做了一次 `load_dotenv(backend/.env)`，而凭据
    是按 `<PROVIDER>_API_KEY` 的命名约定从 `os.environ` 动态查的
    （不是 Settings 的字段，见 `config.py` 开头）。于是一台开发机上跑测试时，
    环境里**一直有两把真气钥匙**；而 CI 上没有 `.env`，同一个用例看到的是
    空字符串。同一份测试，两套环境，两个结果。

    第一次量出来是发布前的一次克隆验证（问题 55）：把仓库克隆到一个
    没有 `.env` 的目录再跑，`943 passed` 变成 `1 failed, 942 passed`。
    失败的那条 `test_registry.py::test_both_configured_builds_a_fallback`
    本地之所以绿，**仅仅因为这台机器上有 `.env`**。也就是说
    **CI 的 pytest job 从第一次跑就会红**——而这个仓库的 CI 从来没有
    执行过，所以它红了多久没人知道，文档里那句"CI 五个 job 全绿"
    也就一直是推出来的。

    为什么不做成"给那条用例补一个 setenv"就完事
    ------------------------------------------
    那修的是那一条，剩下九百多条仍然各自和环境有关，而这类故障的表现
    方式是**只在别人机器上红**：写它的人怎么试都试不出来，CI 又没跑过。
    所以做成默认干净：凭据必须被显式设进来，否则就是没有。
    这与 `_never_touch_production_db` 是同一条理由——"要记得加"等于没有。

    由 `tests/unit/test_env_isolation.py` 守着。**那条守卫的证伪只能在
    本机做**（有 `.env` 时才红）：CI 上没有凭据，删掉这个夹具它照样绿。
    这个不对称本身值得记住——它意味着"CI 绿"不能证明这条夹具还在。
    """
    for name in list(os.environ):
        if name.endswith("_API_KEY"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def mock_pipeline_db(tmp_path, monkeypatch):
    """把三个 provider 都切到 mock，库指到一个临时文件，并跑好迁移。

    返回库文件路径。**这个夹具不花一分钱**：mock provider 不出网、
    不读凭据，所以流水线的集成测试可以进 CI。
    """
    db_path = tmp_path / "xm3-test.db"
    # 必须在构造 Settings 之前设：`get_settings()` 是带缓存的。
    for key, value in {
        "DB_PATH": str(db_path),
        "LLM_PROVIDER": "mock",
        "SEARCH_PROVIDER": "mock",
        "FETCH_PROVIDER": "mock",
        "CASSETTE_MODE": "off",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    from app.db.connection import close_all, get_conn, reset_connections
    from app.db.migrations import migrate
    from app.providers.registry import load_builtin_providers

    # 上一个用例可能在本线程留下一条指向旧库的连接。不关掉的话，
    # 这个用例会写进那个旧库，而断言读的是新库——表现为"数据没写进去"。
    reset_connections()
    close_all()
    migrate(get_conn())
    load_builtin_providers()

    yield db_path

    close_all()
    reset_connections()


# ============================================================
# 跑一次完整的 Mock 流水线
# ============================================================


@pytest.fixture
def run_mock_pipeline(mock_pipeline_db):
    """返回一个 `async (query=..., mode=...) -> Outcome` 的函数。

    刻意走 `runner.stream()` 而不是直接读 journal：这样拿到的就是
    前端真正会解析的那些字节，而不是内存里的对象。顺带验证了
    "订阅拿到的是三行一帧的真实 SSE"。

    集成测试与契约测试都从这里拿事件。两者各自实现一遍的话，
    "哪些事件算数"会有两个答案——而契约测试的全部意义就是它数的那批
    事件与集成测试数的是同一批。
    """
    from app.core.models import TaskRecord
    from app.core.pipeline.runner import ensure_runner, new_task_id
    from app.db.repo import tasks as tasks_repo
    from scripts.run_pipeline_cli import parse_sse_frame
    from tests.support import DEFAULT_MODE, DEFAULT_QUERY, Outcome

    async def _run(query: str = DEFAULT_QUERY, mode: str = DEFAULT_MODE) -> Outcome:
        task_id = new_task_id()
        tasks_repo.create(
            TaskRecord(task_id=task_id, query=query, mode=mode, status="pending")
        )
        runner = ensure_runner(task_id, query, mode_key=mode, auto_clarify=True)
        events = [parse_sse_frame(frame) async for frame in runner.stream(0)]
        result = await runner.wait()
        return Outcome(task_id=task_id, runner=runner, result=result, events=events)

    return _run


@pytest.fixture
def client(mock_pipeline_db):
    """三个 provider 都是 mock 的测试客户端。**零网络、零花费。**

    放在 conftest 而不是某个测试文件里：报告接口与"深化本节"两个文件
    都要用它，各写一份的话，将来改一处漏一处，
    而"某个文件跑的是另一套 provider"这种偏差不会让任何测试变红。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
async def report_id(mock_pipeline_db) -> str:
    """跑完一条真流水线，返回它落库那份报告的 id。

    用真报告而不是手搓一行：手搓的话字段名写错了测试照样绿，
    而这些字段正是这些接口唯一需要写对的东西。
    """
    from app.core.models import TaskRecord
    from app.core.pipeline.runner import ensure_runner, new_task_id
    from app.db.repo import tasks as tasks_repo

    task_id = new_task_id()
    query = "对比 Notion 与 Obsidian"
    tasks_repo.create(
        TaskRecord(task_id=task_id, query=query, mode="quick", status="pending")
    )
    runner = ensure_runner(task_id, query, mode_key="quick", auto_clarify=True)
    async for _ in runner.stream(0):
        pass
    result = await runner.wait()
    assert result.report is not None
    return result.report.report_id
