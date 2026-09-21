"""文档里的测试条数由测试守，不靠人记得改。

为什么需要它
------------
这个项目里「文档里的用例数」漂移过**不止一次**：加了几条测试之后，
README / 项目总结 / 技术栈 / 面试问答集 四份文档各自写着不同的数，
而每一份读起来都像是当时数过的（见 [`问题记录.md`](../../../问题记录.md) 的问题 48）。
漂移的机制很平凡——**同一个数被复制到四处，而只有一处会被顺手改掉**。

所以这里不问"这个数应该是多少"，只问一件事：
**文档里写的那个数，和 `pytest --collect-only` 数出来的那个数，是不是同一个。**

为什么不全文搜，而是逐句匹配
----------------------------
`问题记录.md` 里的 `917` 是**历史数**——它记的是"修之前有多少条"，
而当时那个数就是对的（同一篇文档里 `921` 也是对的，见问题 50）。
一个天真的"全文搜 921"守卫会把那段正确的历史判成错误，
然后被人加一行豁免绕过去——**那时候这个守卫就什么都不守了**。
所以每条断言都指向一句特定的话，并且**故意不覆盖 `问题记录.md`**。

一个诚实的限制
--------------
**前端那 314 条在这里只做「四份文档彼此一致」的检查，不做「与 vitest
实际输出一致」的检查**——跑 vitest 要 node、要 30 秒，塞进 pytest 会让
这个守卫比它守的东西更脆。所以它的能力有边界：
它抓得到"改了 README 忘了改技术栈"，
**抓不到"改了代码、四处文档全忘了改"**。后者只有真跑一次 `npm test` 才知道。
把这个边界写下来，比把它糊过去有用。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_BACKEND = _ROOT / "backend"

#: 每份文档里**那句写死了后端条数的话**：(文件, 模式, 这句话在哪)。
#: 每条模式**只捕获后端条数**，别的数字一律用非捕获组。
_BACKEND_CLAIMS: tuple[tuple[str, str, str], ...] = (
    ("README.md", r"后端 \*\*(\d+) 条 pytest\*\*", "开头那句简介"),
    ("README.md", r"pytest \*\*(\d+)\*\* 条（另有 \d+ 条 `live`", "规模表"),
    ("项目总结.md", r"\*\*pytest 合计\*\* \| \*\*(\d+)\*\*", "测试统计表"),
    ("项目总结.md", r"\*\*工程\*\*：\*\*(\d+) 条 pytest", "简历段"),
    ("技术栈.md", r"质量  pytest (\d+) ·", "分层图"),
    ("技术栈.md", r"### pytest (\d+) 条", "测试小节标题"),
    ("面试问答集.md", r"pytest \*\*(\d+)\*\* 条", "数字来源表"),
)

#: 「加上那几条 live 一共多少」在这些文档里的写法。必须等于实测总数。
_TOTAL_CLAIMS: tuple[tuple[str, str], ...] = (
    ("项目总结.md", r"含 live 共 (\d+)）"),
    ("面试问答集.md", r"含 live 共 (\d+)）"),
)

#: README 是**倒过来**写的：它报"另有几条默认不跑"。必须等于实测差值。
_EXCLUDED_CLAIMS: tuple[tuple[str, str], ...] = (
    ("README.md", r"另有 (\d+) 条 `live` 默认不跑"),
)

#: 前端条数在四份文档里的写法。这里**只断言彼此一致**。
_VITEST_CLAIMS: tuple[tuple[str, str], ...] = (
    ("README.md", r"vitest \*\*(\d+)\*\* 条"),
    ("项目总结.md", r"\| vitest \| \*\*(\d+)\*\*（"),
    ("技术栈.md", r"质量  pytest \d+ · vitest (\d+) ·"),
    ("面试问答集.md", r"vitest \*\*(\d+)\*\* 条"),
)

#: 反证脚本的突变条数在两份文档里的写法。
#:
#: **和前端那组一样，这里只断言两处彼此一致**——这两个数只有真跑一遍才知道
#: （前端那个一轮四十多分钟），塞进 pytest 不现实：守卫会比它守的东西更脆。
#: 所以它抓得到"改了一处忘了另一处"，抓不到"两处都没重跑"。
#: 后者正是 2026-09-20 那次的事：清单上写着 `48/48`，真跑是 `46/49`。
#: 判据用 `(\d+)/\1` 的写法而不是 `(\d+)/(\d+)`：**它顺带要求那句话写成 N/N**，
#: 而 `46/49` 这种"总条数与抓住条数不等"的形状会当场匹配不上、报"模式没匹配到"。
_MUTATION_CLAIMS: tuple[tuple[str, str], ...] = (
    ("修补文档.md", r"反证脚本后端 `(\d+)/\1`、前端 `(\d+)/\2`"),
    ("项目总结.md", r"\| 反证突变 \| 后端 \*\*(\d+)\*\* · 前端 \*\*(\d+)\*\*"),
)

#: 代码规模那两个数。
#:
#: 这一组和上面几组**不一样**：行数不是一个能问 `--collect-only` 问出来的事实，
#: 而是一个**快照**——它每次改代码都会变。所以这里不比对"真值"（那要重新数一遍，
#: 而每天都会不等的守卫一定会被人绕过去），只比对**两个能自己验的关系**：
#:
#: 1. 两处文档说的是同一个数（`_SCALE_CLAIMS`）；
#: 2. `项目总结.md` 里那张表的合计**等于它上面那几行加起来**（`_SCALE_ROWS`）。
#:
#: 第 2 条不需要任何外部真值，**它问的是"这张表自己算得对不对"**，
#: 所以它永远只在"改了某一行、忘了改合计"时红——正是这类表最常见的坏法。
#: 两条都**不能说**那几行本身是不是已经过期；那件事只有重新数一遍才知道，
#: 所以文档里拿"后端 939 条测试"当锚点（那个数有守卫）。
_SCALE_CLAIMS: tuple[tuple[str, str, str], ...] = (
    ("项目总结.md", r"\| \*\*合计\*\* \| \*\*约 ([\d,]+) 行\*\*", "规模表合计"),
    ("面试问答集.md", r"约 \*\*([\d,]+)\*\* 物理行", "数字来源表"),
)

#: `项目总结.md` 规模表的明细行。取每行的第一个数（物理行）。
_SCALE_ROWS = r"^\| (?:后端 `app/`|后端 `tests/`|前端 `src/`|`eval/`|`loadtest/`) \| \*{0,2}([\d,]+)\*{0,2} \|"


def _num(text: str) -> int:
    return int(str(text).replace(",", ""))


#: 「问题记录.md 有多少条」在几份文档里的写法。
#:
#: 这个数和测试条数是**同一类东西**：一个被复制到六处的数，而只有一处
#: 会被顺手改掉。区别是它**每次写完一条新问题就要动**，所以比测试条数
#: 更频繁地漂——写问题 52 的时候就把六处全忘了，是这条守卫逼着改的。
#: 它也不在**那四份**主文档的守卫范围内，所以单开一组。
_ISSUE_CLAIMS: tuple[tuple[str, str, str], ...] = (
    ("README.md", r"\[问题记录\.md\]\(\./问题记录\.md\) \| (\d+) 条", "文档索引表"),
    ("修补文档.md", r"加上 `问题记录\.md`（(\d+) 条）", "文档产出表"),
    ("开发文档.md", r"另记在 \[`问题记录\.md`\]\(问题记录\.md\)（(\d+) 条）", "开头那句"),
    ("开发文档.md", r"\[`问题记录\.md`\]\(问题记录\.md\) \| (\d+) 条真实踩坑", "文档索引表"),
    ("项目总结.md", r"\| 问题记录 \| \*\*(\d+) 条\*\* \|", "技术统计表"),
    ("项目总结.md", r"### 8\. (\d+) 条真实的踩坑记录", "第 8 节标题"),
)


def _extract(pattern: str, text: str) -> list[str]:
    """把一段文字里符合某个模式的那句话中的数字全抓出来。

    单独抽出来是为了能被证伪：`test_模式抓得住一个错的数` 拿它去读一段
    合成文字，确认模式真的会匹配、也真的会不等。

    用 `re.MULTILINE`：规模表那几条明细行是用 `^\\| ...` 锚的——**不锚住行首，
    `| 后端 \`app/\` | ...` 这种形状会匹配到正文里任何一处提到它的地方**
    （这张表的说明段落就复述了其中两个数）。而多行模式对上面那些不带 `^`
    的模式没有任何影响。
    """
    return [g for m in re.finditer(pattern, text, re.MULTILINE) for g in m.groups()]


def _read(doc: str) -> str:
    return (_ROOT / doc).read_text(encoding="utf-8")


def _collected() -> tuple[int, int]:
    """跑一次 `pytest --collect-only`，拿到（会跑的条数，含 live 的总数）。

    用子进程而不是在进程内数收集结果：这条守卫要问的问题应该和 CI 问的一样，
    **所以用同一条命令**。自己解析收集结果等于换了一个问题问。
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only"],
        cwd=_BACKEND,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    m = re.search(r"(\d+)/(\d+) tests collected \((\d+) deselected\)", result.stdout)
    assert m, f"没能从 --collect-only 的输出里数出条数：\n{result.stdout[-1500:]}"
    return int(m.group(1)), int(m.group(2))


def test_后端条数在每份文档里都对得上() -> None:
    """四份文档里每一处写了后端条数的地方，都要等于实测值。

    这条红了不代表代码坏了，代表**有人加/删了测试但没同步文档**——
    而文档里那个数正是面试时会说出口的那个。
    """
    ran, total = _collected()
    assert total - ran == 6, (
        f"被排除的条数变了（实测 {ran}/{total}）——"
        "文档里「另有 6 条 live」那句话要跟着改"
    )

    for doc, pattern, where in _BACKEND_CLAIMS:
        found = _extract(pattern, _read(doc))
        assert found, (
            f"[{doc}] 的{where}：模式 {pattern!r} 一句都没匹配到，"
            "那句话可能被改写过了（改了它这条守卫就失效了）"
        )
        for value in found:
            assert int(value) == ran, (
                f"[{doc}] 的{where}写着 {value} 条，实测是 {ran} 条"
                f"（含 live 共 {total}）"
            )

    for doc, pattern in _TOTAL_CLAIMS:
        found = _extract(pattern, _read(doc))
        assert found, f"[{doc}] 里那个「加不加 live」的数没匹配到：{pattern!r}"
        for value in found:
            assert int(value) == total, (
                f"[{doc}] 写着含 live 共 {value}，实测是 {total}"
            )

    for doc, pattern in _EXCLUDED_CLAIMS:
        found = _extract(pattern, _read(doc))
        assert found, f"[{doc}] 里那个「默认不跑几条」的数没匹配到：{pattern!r}"
        for value in found:
            assert int(value) == total - ran, (
                f"[{doc}] 写着默认不跑 {value} 条，实测是 {total - ran} 条"
            )


def test_前端条数四份文档彼此一致() -> None:
    """看清限制：这**不检查** 314 是不是真的——只检查四份文档说的是同一个数。

    抓得到"改了一处忘了另外三处"，抓不到"改了代码忘了全部四处"。
    """
    seen: dict[str, str] = {}
    for doc, pattern in _VITEST_CLAIMS:
        found = _extract(pattern, _read(doc))
        assert found, f"[{doc}] 的模式 {pattern!r} 没匹配到，那句话可能被改写过了"
        seen[doc] = found[0]

    assert len(set(seen.values())) == 1, (
        "前端条数在几份文档里对不上：\n"
        + "\n".join(f"  {doc}: {value}" for doc, value in seen.items())
    )


def test_反证突变条数两处一致() -> None:
    """看清限制：**这检查不了那两个数是不是真的**——只检查两份文档说的是同一个。

    真值只有跑一遍脚本才知道，而那是四十分钟。所以这里守的是
    "改了一处忘了另一处"，不是"两处都没重跑"。
    """
    seen: dict[str, tuple[str, ...]] = {}
    for doc, pattern in _MUTATION_CLAIMS:
        found = _extract(pattern, _read(doc))
        assert found, (
            f"[{doc}] 的模式 {pattern!r} 没匹配到——那句话可能被改写过了，"
            "或者被写成了 N/M（总条数与抓住条数不等）"
        )
        seen[doc] = tuple(found[:2])

    assert len(set(seen.values())) == 1, (
        "反证突变条数在两份文档里对不上：\n"
        + "\n".join(f"  {doc}: {value}" for doc, value in seen.items())
    )


def test_规模表自己算得对() -> None:
    """表里的合计要等于上面那几行加起来。

    这一条**不需要知道真值**，所以它不会因为"今天又改了几个文件"而红——
    它只在"改了某一行、忘了改合计"时红。看清它的边界：合计对得上
    **不代表那些行还是新的**，只代表这张表没被改坏。
    """
    text = _read("项目总结.md")
    rows = _extract(_SCALE_ROWS, text)
    assert len(rows) == 5, (
        f"规模表应该有 5 行明细，抓到 {len(rows)} 行：{rows}"
        "——某个目录名或列数被改过了，这条守卫会静默失效"
    )

    claimed = _extract(_SCALE_CLAIMS[0][1], text)
    assert claimed, f"项目总结.md 里那个合计数没匹配到：{_SCALE_CLAIMS[0][1]!r}"

    total = sum(_num(value) for value in rows)
    assert _num(claimed[0]) == total - total % 100, (
        f"规模表写着合计 {claimed[0]} 行，五行加起来是 {total} 行"
        "（合计是「约」到百位的数，所以比对的是把零头抹掉之后的值）"
    )


def test_规模数字在两份文档里一致() -> None:
    """看清限制：这**检查不了那两个数是不是真的**——只检查两处说的是同一个。

    和前端那组一样。真值要把每个目录重新数一遍，而那个数每天都在变。
    """
    seen = {}
    for doc, pattern, where in _SCALE_CLAIMS:
        found = _extract(pattern, _read(doc))
        assert found, f"[{doc}] 的{where}：模式 {pattern!r} 没匹配到，那句话可能被改写过了"
        seen[doc] = _num(found[0])

    assert len(set(seen.values())) == 1, (
        "代码规模在两个地方对不上：\n"
        + "\n".join(f"  {doc}: {value}" for doc, value in seen.items())
    )


def test_模式抓得住一个错的数() -> None:
    """自我证伪：确认上面那些模式真的会匹配、也真的会报出"不相等"。

    拿一段合成文字走**同一套模式**，断言抓出来的是那个错的数，
    且它不等于实测值。少了这一步，上面两条可能只是"怎么跑都绿"。
    """
    fake = "后端 **99999 条 pytest**，规模表里是 pytest **88888** 条。"
    found = _extract(_BACKEND_CLAIMS[0][1], fake)
    assert found == ["99999"], f"模式没抓住那个错的数，抓到的是 {found}"

    ran, _total = _collected()
    assert int(found[0]) != ran, "合成文字里的数与实测数相同，这条证伪没有意义"

    # 问题记录那一组也走一遍同一个检查。**这组用的是 `_read()` 之外的
    # 唯一一条路径**（`_issues_recorded()` 直接数标题），所以两半都要证伪。
    fake_issues = "| [问题记录.md](./问题记录.md) | 77777 条按「现象→定位→根因→修复→验证」记录的工程问题 |"
    found_issues = _extract(_ISSUE_CLAIMS[0][1], fake_issues)
    assert found_issues == ["77777"], (
        f"模式没抓住那个错的数，抓到的是 {found_issues}"
    )
    assert int(found_issues[0]) != _issues_recorded(), (
        "合成文字里的数与 `问题记录.md` 的实际条数相同，这条证伪没有意义"
    )

    # 规模那两条也要证伪：它们的模式指向的句子形状更花（带千分位逗号、
    # 带两头的加粗），而"匹配不到"与"数不对"在这个文件里是两种不同的失败。
    fake_scale = (
        "| **合计** | **约 99,900 行** | 约 1 行 | **9 个源文件** |\n"
        "| 后端 `app/` | **11,111** | 1 | 1 个 `.py` |\n"
        "| 前端 `src/` | **22,222** | 1 | 1 个 `.ts(x)` |\n"
        "约 **88,800** 物理行"
    )
    assert _extract(_SCALE_CLAIMS[0][1], fake_scale) == ["99,900"], (
        f"没抓住合成的那句合计：{_extract(_SCALE_CLAIMS[0][1], fake_scale)}"
    )
    assert _extract(_SCALE_CLAIMS[1][1], fake_scale) == ["88,800"], (
        f"没抓住合成的那句规模：{_extract(_SCALE_CLAIMS[1][1], fake_scale)}"
    )
    # 这两句合成的数**故意互不相等**：上面那条"两处一致"的守卫
    # 如果哪天退化成"抓到就算过"，拿这段文字走一遍就会绿——它必须红。
    assert _extract(_SCALE_CLAIMS[0][1], fake_scale) != _extract(
        _SCALE_CLAIMS[1][1], fake_scale
    ), "合成文字里两处规模相同，这条证伪没有意义"


def _issues_recorded() -> int:
    """数 `问题记录.md` 里有多少条 `## 问题 N：`。

    顺带断言编号是**从 1 连续**的：这几处文档里的数都是从这个标题上抄的，
    而跳号或重号时"最大的那个号"就不等于条数了——那时这个守卫会去
    比对两个不同的量，还会显得很有道理。
    """
    text = _read("问题记录.md")
    nums = [int(n) for n in re.findall(r"^## 问题 (\d+)：", text, re.M)]
    assert nums, "一条 `## 问题 N：` 都没匹配到——标题格式可能被改过了"
    assert nums == list(range(1, len(nums) + 1)), (
        f"问题编号不是从 1 连续的（{len(nums)} 条，"
        f"前三个 {nums[:3]}、后三个 {nums[-3:]}）"
    )
    return len(nums)


def test_问题记录条数在每份文档里都对得上() -> None:
    """六处写了"问题记录有多少条"的地方，都要等于 `问题记录.md` 里的实际条数。

    **和测试条数是同一个机制**：写完一条新问题，只想得起改一处。
    这条守卫是写问题 52 时补的——那六处当时全忘了改。
    """
    count = _issues_recorded()

    for doc, pattern, where in _ISSUE_CLAIMS:
        found = _extract(pattern, _read(doc))
        assert found, (
            f"[{doc}] 的{where}：模式 {pattern!r} 一句都没匹配到，"
            "那句话可能被改写过了（改了它这条守卫就失效了）"
        )
        for value in found:
            assert int(value) == count, (
                f"[{doc}] 的{where}写着 {value} 条，"
                f"`问题记录.md` 里实际是 {count} 条"
            )
