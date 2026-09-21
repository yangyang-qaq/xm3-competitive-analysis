"""CI 的第一道关是 ruff，而它**在本机一直是红的**——没有人知道。

现象
----
`.github/workflows/ci.yml` 的 backend job 是这么排的：

```yaml
- name: ruff
  working-directory: backend
  run: ruff check app tests scripts     # ← 没有 || true

- name: pytest（排除 live）
  run: pytest -q                        # ← 上面挂了就永远跑不到这里
```

本机跑一遍同一条命令，5 条错误：两个没用到的 import、
一个少了个空行的 import 块、还有两条在测试里。

这 5 条都不是"代码错了"，所以 `pytest` 全绿、`vitest` 全绿、
文档里的 917 条也全绿——**而 CI 会停在 ruff 那一步，连测试都不会开始跑。**
"917 条全绿"和"CI 是绿的"在这里是两句不同的话。

为什么加一条测试而不是"记得跑 ruff"
--------------------------------
这条测试做的事就是 CI 做的那件事：拿同一条命令问同一个问题。
理由和 `test_docs_links.py` 一样——**能被工具抓住的缺陷就该让工具去抓**，
而"记得在提交前跑一下 lint"这种约定等于没有约定
（[`conftest.py`](conftest.py) 里那条 autouse 夹具的 docstring
为同一件事吃过一次亏）。

判据只有一条：**`ruff check` 的退出码**。不解析它的输出，
不自己维护一份规则清单——那份清单会在 ruff 升级后变成一个假的检查。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: 与 `.github/workflows/ci.yml` 里那条命令**逐字相同**。
#: 分成列表是因为 Windows 上不能靠 shell 展开。
_CI_ARGS = ["check", "app", "tests", "scripts"]

_BACKEND = Path(__file__).resolve().parents[2]


def _run_ruff() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ruff", *_CI_ARGS],
        cwd=_BACKEND,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_ruff_装得上否则这条测试没意义() -> None:
    """ruff 是 dev 依赖。它不该缺失——缺失的话下面那条会静默变成空断言。"""
    probe = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        cwd=_BACKEND,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert probe.returncode == 0, f"ruff 没装或跑不起来：{probe.stderr}"


def test_ci_的_ruff_这一步在本机是绿的() -> None:
    """这条测试红了，CI 就是红的——而且 pytest 那 917 条一条都不会跑。"""
    result = _run_ruff()
    assert result.returncode == 0, (
        "ruff 有问题（CI 会停在这一步）：\n"
        + (result.stdout or result.stderr).strip()
    )


def test_跑的是同一个目录集合() -> None:
    """自我证伪：命令里少一个目录，这条测试就会**少检查一整块**却依然全绿。

    所以断言的是"命令本身"——`app` / `tests` / `scripts` 三个都在。
    有一天有人觉得 `scripts` 不重要而把它去掉，这里会先响。
    """
    assert _CI_ARGS == ["check", "app", "tests", "scripts"]

    ci = (_BACKEND.parent / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "ruff check app tests scripts" in ci, (
        "CI 里的 ruff 命令变了，这条测试问的已经不是同一个问题"
    )


def test_改坏一处_ruff_就该红(tmp_path: Path) -> None:
    """再证伪一次：确认这个检查真的会失败，不是"怎么跑都绿"。

    在**仓库外**造一个必然违规的文件（`import os` 不用），
    断言 ruff 对它返回非零——证明上面那条 `returncode == 0` 是有意义的。
    放临时目录是为了不让探针文件留在 `tests/` 里影响别的用例。
    """
    probe = tmp_path / "probe.py"
    probe.write_text("import os\n\n\ndef f() -> None:\n    assert True\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(probe)],
        cwd=_BACKEND,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode != 0, "ruff 对一条明确的 F401 返回了 0，这个检查是假的"
    assert "F401" in result.stdout
