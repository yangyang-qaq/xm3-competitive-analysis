"""`--provider` 是 **LLM 的快捷方式**，不是"三家都用这个值"。

`eval/run_eval.py --live` 的缺省 provider 是 `deepseek`，而原来的
`_set_provider_env()` 不看角色，把这一个值同时填进了 `LLM_PROVIDER` /
`SEARCH_PROVIDER` / `FETCH_PROVIDER`。搜索的注册表里只有
`bocha / mock / tavily`，于是每一次运行都死在

    未注册的 搜索 provider：'deepseek'。可用：bocha, mock, tavily

15 次运行、一条都没跑成。**也就是说 `--live` 从来没跑通过**——
它至今没花过钱，不是因为它省钱，是因为它连第一次网络请求都没发出去。

这个缺陷为什么能活这么久
------------------------
`--provider` 的取值里，**只有 `mock` 同时是三家 provider 的名字**。
`--gate` 用的恒为 `mock`，所以"三家一起填"在那里是对的、也是被测过的；
`--live` 用的是 `deepseek`，是 LLM 专有的名字——而 `--live` 一次都没跑过。
**一个抽象刚好在它唯一被测试过的取值上成立。**

判据
----
下面第 3 条问的不是"deepseek 在不在搜索表里"，而是**注册表**：
"你填进某个角色的名字，在那个角色自己的注册表里存在吗？"

这样写是为了不把测试变成一张手抄的名单。手抄名单的两个结局都不好：
将来真加一个叫 deepseek 的搜索源，它会开始**阻止一件正确的事**；
而漏抄一个新增的角色，它就**悄悄少检查一块**。
"""
from __future__ import annotations

import os

import pytest
from eval.run_eval import _set_provider_env

from app.providers.registry import (
    _FETCH,  # noqa: PLC2701 - 抓取没有公开的 available_*，见文件末尾说明
    available_llm,
    available_search,
    load_builtin_providers,
)

_ROLES: tuple[tuple[str, str], ...] = (
    ("LLM_PROVIDER", "llm"),
    ("SEARCH_PROVIDER", "search"),
    ("FETCH_PROVIDER", "fetch"),
)


def _registered(role: str) -> set[str]:
    load_builtin_providers()
    if role == "llm":
        return set(available_llm())
    if role == "search":
        return set(available_search())
    return set(_FETCH)


def _unregistered_roles() -> list[str]:
    """当前环境里，哪个角色被填进了一个**它自己不认识**的 provider 名。

    这是这条测试真正的断言对象：一个名字是否合法，取决于**它被填给谁**。
    """
    bad: list[str] = []
    for key, role in _ROLES:
        value = os.environ.get(key, "")
        if value and value not in _registered(role):
            bad.append(f"{key}={value}（{role} 的可用值：{sorted(_registered(role))}）")
    return bad


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """三个键先钉成"已知合法"，再让被测代码去改。

    不先钉的话，这一条会随 `.env` 的内容时红时绿——
    而一条会随开发机环境变化的测试，最后一定会被跳过。
    """
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("SEARCH_PROVIDER", "mock")
    monkeypatch.setenv("FETCH_PROVIDER", "mock")


def test_live_的取值只覆盖_LLM() -> None:
    """`--live` 的缺省值是 deepseek，它只能进 LLM 那一格。"""
    _set_provider_env("deepseek")

    assert os.environ["LLM_PROVIDER"] == "deepseek"
    assert os.environ["SEARCH_PROVIDER"] == "mock", (
        "搜索被 LLM 的 provider 名覆盖了。注册表里没有这个名字，"
        "运行会死在'未注册的 搜索 provider'，而不是去联网。"
    )
    assert os.environ["FETCH_PROVIDER"] == "mock"


def test_mock_覆盖三家() -> None:
    """`mock` 是那个特例：它代表"整条链路都用假实现"。

    这个语义必须留着——`--gate` 靠它保证零成本，只覆盖 LLM 的话
    搜索会去打真 API，评测号称零成本而实际在花钱。
    """
    _set_provider_env("mock")

    for key, _ in _ROLES:
        assert os.environ[key] == "mock", f"{key} 没被覆盖成 mock"


@pytest.mark.parametrize("shortcut", ["mock", "deepseek", "zhipu"])
def test_填进每个角色的名字都在对应的注册表里(shortcut: str) -> None:
    """结构性的那一条：不问名字，问注册表。

    三个取值分别代表：三家通用的假实现、真实的 LLM 名、以及一个
    **尚未被真实调用过**的 LLM 名（zhipu 在负面清单里）。
    """
    _set_provider_env(shortcut)

    assert _unregistered_roles() == [], (
        f"--provider {shortcut} 让某个角色拿到了它不认识的名字："
        f"{_unregistered_roles()}"
    )


def test_旧行为过不了这一关() -> None:
    """**反证：证明上面那条真的会红。**

    把修复前的行为原样重放——不看角色，一个值填三格。
    上面那条结构性检查必须当场报出两个角色。

    没有这一条的话，`_unregistered_roles()` 如果因为某个笔误
    （比如 `_registered` 返回了全集）恒为空，上面三条会一起变绿，
    而它们什么都没检查。
    """
    for key, _ in _ROLES:  # 修复前的 `_set_provider_env("deepseek")`
        os.environ[key] = "deepseek"

    bad = _unregistered_roles()
    assert len(bad) == 2, f"应该报出 search 与 fetch 两个角色，实际：{bad}"
    assert any("SEARCH_PROVIDER" in item for item in bad)
    assert any("FETCH_PROVIDER" in item for item in bad)
    assert not any("LLM_PROVIDER" in item for item in bad), (
        "deepseek 在 LLM 的注册表里是合法的，不该被算成坏值"
    )


# 为什么宁可 import 一个私有名 `_FETCH`
# -------------------------------------
# `available_llm()` / `available_search()` 是公开的，抓取没有对应的
# `available_fetch()`。为一个测试去加一个公开 API，等于让生产代码
# 多一个只被测试使用的出口；而"抓取"这一格恰恰是原缺陷的第二个受害角色
# （`FETCH_PROVIDER=deepseek`），不查它这条测试就少了三分之一。
# 权衡下来，在**测试**里 import 一个私有字典是更小的代价。
