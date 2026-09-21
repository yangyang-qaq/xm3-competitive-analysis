"""`report/refine.py` 里的纯函数：从批注到检索目标的那一段。

这一段值得单独测，因为它是**唯一一处把自然语言变成查询的地方**，
而它有两条错路，两条都不难走进去：

1. **按空格切中文。** 第一版就是按空格切的，实测把
   "这部分太笼统了 我想知道他们到底怎么收费的" 切出了
   `['这部分太笼统了']`——7 个字的半句话。中文没有词边界，
   按空格切出来的从来不是词。这半句话会变成一个搜索查询，
   换回一堆无关页面，而用户会以为搜索源不行。
2. **拿章节 key 当维度名。** 章节 key（`pricing`）和维度名（`定价策略`）
   不是一回事，混用会凭空发明一个维度，把覆盖率的分母撑大。
"""
from __future__ import annotations

import pytest

from app.core.report.refine import (
    MAX_DEEPEN_QUERIES,
    _annotation_terms,
    _section_of,
    _targets,
)
from app.core.schemas.report import ReportSection


class _Record:
    """`_targets` 只用到这四个属性。用真 `ReportRecord` 也行，
    但那会把"接口改了个字段名"这种无关的失败一起带进来。"""

    def __init__(self, *, brands=("A", "B"), subject="主题", dimensions=("定价策略",)):
        self.brands = list(brands)
        self.subject = subject
        self.data = {"dimensions": list(dimensions)}


def _section(key="executive_summary", title="执行摘要") -> ReportSection:
    return ReportSection(key=key, title=title, content="正文")


# ============================================================
# 批注 → 检索词
# ============================================================


def test_中文批注不切词() -> None:
    """**本文件的重点。** 整句中文一个人话，切出来的半句话不是词。"""
    assert _annotation_terms("这部分太笼统了 我想知道他们到底怎么收费的") == []


def test_中文整句不留一个词() -> None:
    """没有空格的整句中文同样不能产出"词"。"""
    assert _annotation_terms("这一节太笼统了我想知道具体怎么收费的") == []


def test_英文与型号留下来() -> None:
    """`API`、`GPT-4`、`SaaS` 本身就是完整词，带上能提高精度——
    这也正是"不切中文"能成立的前提：留下的都是真词。"""
    assert _annotation_terms("我想看 API 限流和 GPT-4 支持情况") == ["API", "GPT-4"]


def test_标点被剥掉() -> None:
    assert _annotation_terms("看看 API，还有 SaaS。") == ["API", "SaaS"]


def test_跟着中文的英文词也能捞出来() -> None:
    """**这条是上一版实现栽的地方。**

    中文批注里的英文词通常和中文**粘在一起**，中间没有空格。
    上一版按空格切，切出来的 token 是 `"API，还有"`——`isascii()` 为假，
    于是整个词被丢掉，而它恰恰是最该留下的那一类。
    只有带上中文标点、或者刚好被空格夹在中间的英文词才活得下来，
    那是一种看起来能用、实际只在人造例子上能用的实现。
    """
    assert _annotation_terms("看看API，还有SaaS的价格") == ["API", "SaaS"]


def test_重复的词只留一次() -> None:
    """同一个词在批注里出现两遍（"API 限流，API 配额"），
    检索词里出现两次只会让查询变长，不会让它更准。"""
    assert _annotation_terms("API 限流和 API 配额") == ["API"]


def test_型号里的点与加号() -> None:
    """`GPT-4`、`C++`、`Node.js` 都是完整词，尾部的符号要收干净。"""
    assert _annotation_terms("用 Node.js 写过 C++ 吗") == ["Node.js", "C++"]


def test_单个字符不留() -> None:
    """单字母（`a`、`x`）当搜索词没有检索价值，只会污染查询。"""
    assert _annotation_terms("a API") == ["API"]


def test_超长的英文串不留() -> None:
    """一整串没有空格的英文多半是粘贴进来的一段东西，不是关键词。"""
    assert _annotation_terms("a" * 40 + " API") == ["API"]


def test_纯数字不留() -> None:
    assert _annotation_terms("2024 2025 API") == ["API"]


def test_空批注不炸() -> None:
    assert _annotation_terms("") == []
    assert _annotation_terms("   ") == []


# ============================================================
# 批注 + 章节 → 采集目标
# ============================================================


def test_检索词带上章节标题() -> None:
    """批注里切不出中文词，所以检索词的主力是**章节标题**——
    它本来就是这一章的主题（"定价分析"），一定存在、一定相关。"""
    targets = _targets(_Record(), _section(title="定价分析"), "太笼统了")

    assert targets
    assert all("定价分析" in item["keyword"] for item in targets)


def test_批注里的英文词也进检索词() -> None:
    targets = _targets(_Record(), _section(title="定价分析"), "想知道 GPT-4 怎么算")

    assert all("GPT-4" in item["keyword"] for item in targets)


def test_每个品牌一条目标() -> None:
    targets = _targets(_Record(brands=["甲", "乙"]), _section(), "x")
    assert [item["brand"] for item in targets] == ["甲", "乙"]


def test_目标数有上限() -> None:
    """品牌多的时候不能按品牌数发查询——那是拿用户的钱换覆盖率。"""
    record = _Record(brands=[f"品牌{i}" for i in range(10)])
    targets = _targets(record, _section(), "x")
    assert len(targets) == MAX_DEEPEN_QUERIES


def test_没有品牌时退回主题() -> None:
    """`brands` 为空（清单一类的调研没有具体品牌）时，用主题当检索主体，
    否则查询会变成光秃秃的"定价分析"，搜出来的东西和这次调研无关。"""
    targets = _targets(_Record(brands=[], subject="笔记软件"), _section(), "x")
    assert [item["brand"] for item in targets] == ["笔记软件"]


def test_章节key不是维度名时不带维度() -> None:
    """`pricing` 和 `定价策略` 不是一回事。拿 key 当维度名会凭空发明一个
    维度，而它会进覆盖率的分母——覆盖率于是凭空变低。"""
    record = _Record(dimensions=["定价策略"])
    targets = _targets(record, _section(key="pricing"), "x")
    assert all(item["dimension"] == "" for item in targets)


def test_章节key刚好是维度名时带上() -> None:
    record = _Record(dimensions=["定价策略"])
    targets = _targets(record, _section(key="定价策略"), "x")
    assert all(item["dimension"] == "定价策略" for item in targets)


def test_报告没有维度清单时不带维度() -> None:
    record = _Record(dimensions=[])
    targets = _targets(record, _section(key="定价策略"), "x")
    assert all(item["dimension"] == "" for item in targets)


# ============================================================
# 找章节
# ============================================================


def _body(*keys: str) -> dict:
    return {
        "sections": [
            {"key": key, "title": key, "content": f"{key} 的正文"} for key in keys
        ]
    }


def test_按key找章节() -> None:
    index, section = _section_of(_body("a", "b", "c"), "b")
    assert index == 1
    assert section.key == "b"


def test_留空时取第一章() -> None:
    """批注挂在整份报告上时，"深化"落到第一章——
    报错让用户去猜填什么，是在为难人。"""
    index, section = _section_of(_body("a", "b"), "")
    assert index == 0
    assert section.key == "a"


def test_找不到时抛KeyError() -> None:
    with pytest.raises(KeyError):
        _section_of(_body("a"), "nope")


def test_一章都没有时抛LookupError而不是KeyError() -> None:
    """这两种要分得开：**章节不存在是请求指错了地方**（404），
    而**报告里一章都没有是数据有问题**（422）。

    `KeyError` 是 `LookupError` 的子类，所以调用方必须先接住 `KeyError`
    ——写这条时就是先写了 `except LookupError`，于是所有错的
    `sectionKey` 都被翻译成了 422。
    """
    with pytest.raises(LookupError) as excinfo:
        _section_of({"sections": []}, "a")
    assert not isinstance(excinfo.value, KeyError)
