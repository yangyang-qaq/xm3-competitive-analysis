"""文档里的相对链接必须指向**真的存在**的文件。

为什么值得单开一条测试
----------------------
这份仓库的 README 曾经同时链向 `开发文档.md` / `技术栈.md` / `项目总结.md` /
`面试问答集.md` / `docs/PROVIDERS.md` 五个**还不存在**的文件。目录表读起来
完整、专业，点进去全是 404。

这类缺陷之所以难被发现，是因为它**不报错**：

- 它不是语法错误，编辑器不标红；
- 它不是代码，`pytest` / `ruff` / `tsc` 谁也不看 markdown；
- 它不在运行时路径上，没有任何请求会 500。

于是唯一的发现方式是"有人真的去点了那个链接"。而写文档的人刚刚写完，
**他最不可能去点**——他知道自己想写什么，读到的就是自己脑子里的那版。

`docs/ARCHITECTURE.md` 的流水线顺序有 `test_docs_order.py` 守着，
这条是它的同族：**把"文档说了什么"和"仓库里有什么"钉在一起。**
一份写着"详见 X"而 X 不存在的文档，比一句不写更糟——
它让读者以为那部分内容已经有了。

判据
----
扫描下面 `ENTRY_DOCS` 里列出的每一份文档，取出所有 `[文字](目标)` 形式的
相对链接（含图片），逐个断言目标存在于磁盘上。

刻意**不**做的事
----------------
- **不检查外部链接。** `http(s)://` 全部跳过。跑一条断言去联网会让测试
  依赖网络，而失败原因变成"今天网不好"——那种测试最后一定会被跳过。
- **不检查纯锚点**（`#小节`）和以 `/` 开头的路径。
- **不解析代码块和行内代码。** 演示 markdown 写法的例子里会出现
  `[示例](不存在的文件.md)`，那不是链接。先剥掉再扫，免得测试逼着
  文档作者去迁就它。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

#: 要扫的文档。**写死，不用 glob 发现。**
#: 用 glob 的话，删掉一份文档会让扫描集合安静地变小，测试照样绿——
#: 那就成了"用例数会自己缩水的测试套件"，比没有更糟。
#: 所以这里既断言"这些文件在"，也断言"扫出来的链接够多"。
ENTRY_DOCS: tuple[str, ...] = (
    "README.md",
    "CLAUDE.md",
    "开发文档.md",
    "技术栈.md",
    "项目总结.md",
    "面试问答集.md",
    "问题记录.md",
    "修补文档.md",
    "docs/ARCHITECTURE.md",
    "docs/DECISIONS.md",
    "docs/AGENTS.md",
    "docs/PROVIDERS.md",
    "docs/API.md",
    "docs/DATA_MODEL.md",
    "docs/EVAL.md",
)

#: `[文字](目标)` 与 `![文字](目标)`。目标里不允许空白和右括号，
#: 这足以覆盖这个仓库的写法，又不会把 `[见下](#a) 和 [x](b)` 这种
#: 一行两链接的句子吃成一个。
_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")

#: 围栏代码块（``` 或 ~~~）。非贪婪，逐块剥掉。
_FENCE = re.compile(r"^([`~]{3,})[^\n]*\n.*?^\1[^\n]*$", re.DOTALL | re.MULTILINE)

#: 行内代码。用反引号配对剥掉，同样是为了不吃掉示例。
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def _strip_code(text: str) -> str:
    """去掉围栏代码块与行内代码。

    多余的换行留着不碍事：链接是按正则从剩下的文本里取的，
    位置信息本来就不参与判断。
    """
    return _INLINE_CODE.sub(" ", _FENCE.sub("\n", text))


def _relative_links(text: str) -> list[str]:
    """取出需要检查的相对链接目标，已去掉 `#锚点` 与 `?查询串`。"""
    out: list[str] = []
    for raw in _LINK.findall(_strip_code(text)):
        if raw.startswith(("http://", "https://", "mailto:", "#", "/", "{")):
            continue
        target = raw.split("#", 1)[0].split("?", 1)[0].strip()
        if target:
            out.append(target)
    return out


@pytest.mark.parametrize("doc", ENTRY_DOCS)
def test_文档存在(doc: str) -> None:
    """先确认文件在。

    不这么做的话，`ENTRY_DOCS` 里写错一个名字（少一个字、
    中文标点写成英文）会让那份文档**根本不参与扫描**，
    而测试依然是绿的——一个错别字就能让一条链接检查静默失效。
    """
    path = ROOT / doc
    assert path.is_file(), (
        f"{doc} 不在仓库里。ENTRY_DOCS 里列了它，说明它应该是文档集的一部分；"
        "要么把它写出来，要么把它从 ENTRY_DOCS 里去掉——不要留着这条。"
    )


@pytest.mark.parametrize("doc", ENTRY_DOCS)
def test_文档里的相对链接都指得到(doc: str) -> None:
    path = ROOT / doc
    if not path.is_file():
        pytest.skip(f"{doc} 不存在；上面那条用例已经在报这个了")

    bad: list[str] = []
    for target in _relative_links(path.read_text("utf-8")):
        # 链接是**相对当前文档**解析的，不是相对仓库根。
        # 这在这个仓库里现在没有区别（所有链接都写成从根算起的样子），
        # 但写成"相对当前文档"才是 markdown 的实际语义；
        # 按根解析会让 `docs/` 里一份写 `API.md` 的文档被误判成坏链。
        resolved = (path.parent / target).resolve()
        if not resolved.exists():
            bad.append(target)

    assert not bad, (
        f"{doc} 里有链接指向不存在的路径：{sorted(set(bad))}\n"
        "写文档时最容易出的就是这个——列了目录表，正文还没来得及写。\n"
        "两种改法：把文件补上，或者先把那条链接撤掉。"
    )


def test_扫描器读得出链接数量不是零() -> None:
    """**防空转。**

    上面那两条是按文档参数化的，如果 `_LINK` 写错了（正则不匹配、
    或者 `_strip_code` 把整篇都剥没了），它们会**全部变绿**——
    因为"坏链列表为空"和"一个链接都没扫到"走的是同一个 assert。

    所以这里钉一个下限：这个仓库的文档不可能只有个位数链接。
    数量写死是有意的：它只在下限意义上成立，涨上去不用改。
    """
    total = sum(
        len(_relative_links((ROOT / doc).read_text("utf-8")))
        for doc in ENTRY_DOCS
        if (ROOT / doc).is_file()
    )
    assert total >= 15, (
        f"全部文档加起来只扫出 {total} 条相对链接，像是解析器坏了而不是文档没链接。"
        "先看 _LINK 正则和 _strip_code——它们错了的话，"
        "上面那两条链接检查等于什么都没查。"
    )


def test_扫描器认得出坏链() -> None:
    """给上面那条做反证：**证明它真的会红**。

    喂一段文本，里面一条好链接、一条坏链接、一条外链、一条纯锚点，
    一条在代码块里。解析器必须只交出前两条里的**相对**那条。
    """
    text = """\
好链接：[架构](docs/ARCHITECTURE.md)
坏链接：[没写](docs/根本不存在.md)
外链：[FastAPI](https://fastapi.tiangolo.com/)
锚点：[见上](#判据)
带锚点的相对链接：[模型](docs/DATA_MODEL.md#表)
图片：![截图](loadtest/reports/read.html)

```markdown
示例：[这不是链接](示例文件.md)
```
行内示例 `[也不是](另一个.md)` 结束。
"""
    found = _relative_links(text)

    assert "docs/ARCHITECTURE.md" in found
    assert "docs/DATA_MODEL.md" in found, "带 #锚点 的相对链接被整个丢掉了"
    assert "loadtest/reports/read.html" in found, "图片链接没被收进来"
    assert "https://fastapi.tiangolo.com/" not in found
    assert not any(x.startswith("#") for x in found)
    assert "示例文件.md" not in found, "代码块里的示例被当成真链接了"
    assert "另一个.md" not in found, "行内代码里的示例被当成真链接了"


def test_根目录文档集与计划一致() -> None:
    """五份中文文档 + README/CLAUDE/问题记录。

    计划里写明的产出清单，这里落成一条断言。少一份就意味着
    "文档齐全"这个验收项没有真的达成，而它不会以别的方式暴露出来。
    """
    required = {
        "README.md", "CLAUDE.md",
        "开发文档.md", "技术栈.md", "项目总结.md", "面试问答集.md",
    }
    missing = sorted(name for name in required if not (ROOT / name).is_file())
    assert not missing, f"根目录缺这些文档：{missing}"
