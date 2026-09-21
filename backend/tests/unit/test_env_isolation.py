"""测试套件不能依赖本机的 `backend/.env`。

这一条守的是 `tests/conftest.py::_no_ambient_credentials`。
它的来由值得完整读一遍，因为它是这个仓库里**唯一一条"只能在本机证伪"
的守卫**——见下面最后一段。

问题 55 的形状
--------------
发布前把仓库克隆到一个干净目录再跑测试（这是"陌生人照 README 能不能
跑通"的最低限度验证），拿到的是 `1 failed, 942 passed`；而在开发机上
是 `943 passed`。差别只有一个：克隆出来的目录里没有 `backend/.env`。

失败的是 `test_registry.py::test_both_configured_builds_a_fallback`，
它需要 `DEEPSEEK_API_KEY` 存在。开发机上它一直绿，**仅仅因为那台机器
上有 `.env`**。

也就是说：**CI 的 pytest job 从第一次跑就会红。** 而这件事此前完全
不可见——因为这个仓库从来没有 `git init` 过，CI 一次都没执行，
文档里那句"CI 五个 job 全绿"是推出来的（见 `修补文档.md` A5）。

为什么守的粒度是"整个进程环境"而不是"那一条用例"
------------------------------------------------
只给那一条补 `setenv`，修的是那一条；剩下九百多条仍然各自与环境有关，
而这类故障的表现方式是**只在别人机器上红**，写它的人怎么试都试不出来。
所以 `_no_ambient_credentials` 是 autouse 的：凭据必须被显式设进来，
否则就是没有。
"""
from __future__ import annotations

import os

from app.core.config import ENV_FILE


def test_凭据不会从本机的_env_漏进测试() -> None:
    """跑用例时，环境里不该有任何 `*_API_KEY`。

    删掉 `conftest.py::_no_ambient_credentials`，本机（有 `.env`）这条就红。
    """
    leaked = sorted(name for name in os.environ if name.endswith("_API_KEY"))

    assert leaked == [], (
        f"本机 `.env` 里的凭据漏进了测试环境：{leaked}——"
        "这些用例在 CI 上会拿到空值，于是同一份测试在两台机器上结果不同"
    )


def test_环境里有本机的_env_文件时这条守卫才有意义() -> None:
    """把这条守卫的**适用条件**也断言出来，免得它悄悄变成一句空话。

    本机有 `.env`、CI 上没有。所以：

    - 本机：上面那条断言真的有东西可挡，删掉夹具就红；
    - CI：环境里本来就没有凭据，**删掉夹具它照样绿**。

    这是本仓库唯一一处"证伪只能在本机做"的守卫。写下来是因为
    "CI 全绿"这句话很容易被读成"所有守卫都在工作"，而这条不在那个
    集合里——它绿在"本来就没有东西可漏"上。

    这里只断言 `ENV_FILE` 指向哪里，不断言它存不存在：
    断言"存在"的话 CI 上这条自己就红了。
    """
    assert ENV_FILE.name == ".env"
    assert ENV_FILE.parent.name == "backend", (
        f"`.env` 的查找路径变了（现在是 {ENV_FILE}）："
        "上面那条守卫挡的是它，路径一改就挡不住了"
    )
