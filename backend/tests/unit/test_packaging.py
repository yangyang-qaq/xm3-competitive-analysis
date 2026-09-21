"""`backend/` 要能**单独**被安装。

这条守的是 CI 的第一步 `pip install -e "backend[dev]"`。
它的来由是问题 56：那次 CI 第一次真的跑，五个 job 里**四个死在第一步**，
而本机一次都没复现过。

为什么会漏
----------
`backend/pyproject.toml` 里写着 `readme = "../README.md"`，指向项目目录之外。
新版的 setuptools 直接拒绝：

    DistutilsOptionError: Cannot access '.../backend/../README.md'
    (or anything outside '.../backend')

而这个字段活了很久没人发现，因为它**只在"重新安装"时才被求值**：
本机那份 `xm3-backend` 早就是用当时那版 setuptools 装好的可编辑安装，
之后没有任何事会再走一遍 `pip install -e`。于是：

- `pytest` 天天跑，读的是 `pythonpath`，**根本不经过打包**；
- "克隆到空目录再跑测试"那次也没抓到——它用的是**已有的** venv，
  而那份 `xm3-backend` 指向的是原来那个目录。**那一步验的是代码，不是安装。**
- 只有 CI 每次从零装一次，所以只有 CI 会红。

这正是问题 55 的同一个形状：**本机绿不是 CI 绿的证据**。
区别是 55 靠"克隆 + 空 .env"就能复现，而这一条要
**"新建一个 venv 从零装一遍"**——比克隆贵得多，所以更少有人做。

守的是什么
----------
不跑安装（那要联网、要几十秒），只问一件在元数据里就能问清的事：
**`pyproject.toml` 里有没有文件指向 `backend/` 之外。**
有的话，这个包就不可能被单独安装——不管是谁来装、在哪台机器上装。

这是一个**结构性**判据，不是"哪条命令能不能跑通"，所以它不需要网络、
不需要 setuptools 的某个版本，也不会因为平台不同而给出不同答案。
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

#: tests/unit/test_packaging.py -> tests/unit -> tests -> backend
_BACKEND = Path(__file__).resolve().parents[2]

#: 只看**构建元数据**这两张表：`[project]`（readme / license / dynamic）
#: 与 `[tool.setuptools]`（packages.find / license-files / package-data）。
#:
#: 为什么不是"整个 pyproject 都不许出现 `..`"——这条线是**跑出来的，不是想出来的**：
#: 第一版就是这么写的，当场红了，命中的是
#:
#:     [tool.pytest.ini_options] pythonpath = [".", ".."]
#:
#: 那个 `".."` 是仓库根，加进去是为了让测试里 `import eval` 能成立
#: （见上面 [tool.pytest.ini_options] 的注释），它是**故意的**，
#: 而且它落在 `sys.path` 上、不落在构建路径上——setuptools 根本不会看它。
#:
#: 同一个字符串 `".."`，在构建元数据里是致命的，在 pytest 配置里是必须的。
#: 所以判据不是"值长什么样"，而是"**这个值会不会被构建后端当成路径**"。
_BUILD_TABLES: tuple[tuple[str, ...], ...] = (("project",), ("tool", "setuptools"))


def _at(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    """按 `[a.b.c]` 的路径取值，缺表就是空表。"""
    node: Any = data
    for key in path:
        if not isinstance(node, dict):
            return {}
        node = node.get(key, {})
    return node


def _strings(node: Any):
    """把一份解析好的 TOML 里所有的字符串挖出来。

    逐层递归而不是只读 `readme`：同样会踩坑的字段还有 `license.file`、
    `tool.setuptools.license-files`、`dynamic` 里那几条，
    以及将来谁加进来的新字段。**按"值长什么样"判，而不是按"字段叫什么"判**，
    就是为了不必每次有人加字段时回来补一张名字表。
    """
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def test_元数据里没有文件指向_backend_之外() -> None:
    """`..` 出现在任何一条元数据字符串里，这个包就装不上。

    证伪很简单：把 `readme = "../README.md"` 加回去，这条就红。
    """
    data = tomllib.loads((_BACKEND / "pyproject.toml").read_text(encoding="utf-8"))

    escaped = [
        value
        for path in _BUILD_TABLES
        for value in _strings(_at(data, path))
        if ".." in Path(value).parts
    ]

    assert not escaped, (
        f"`backend/pyproject.toml` 里有文件指向了 backend/ 之外：{escaped}——"
        "setuptools 会拒绝这种路径（DistutilsOptionError: Cannot access ..."
        "or anything outside ...），于是 `pip install -e backend` 直接失败。"
        "而这个失败**本机看不到**：本机的可编辑安装早就装好了，"
        "只有 CI（或任何一台新机器）会重新装一遍（见问题 56）"
    )


def test_项目目录之外真的有一份_README() -> None:
    """把上一条的**前提**也断言出来，免得它退化成一句空话。

    上一条说"不许指向外面"。如果哪天有人为了绕开它而把整个包搬到
    别的目录去（`backend/` 不再是安装根），那条断言会照样绿——
    而它当时想守的东西已经不在了。

    所以这里把"`backend/` 的上一级有一份 README"这件事也说清楚：
    上一条之所以是个**取舍**（放弃长描述，而不是复制一份 README），
    是因为那份 README 属于整个仓库，它就该待在仓库根。
    """
    assert (_BACKEND / "pyproject.toml").is_file()
    assert (_BACKEND.parent / "README.md").is_file(), (
        "仓库根的 README.md 不在了——`backend/` 的安装根位置可能被改过，"
        "上一条断言要跟着重新想过"
    )
