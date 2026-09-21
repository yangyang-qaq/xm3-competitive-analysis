"""结构化知识的 Schema 与容错校验。

`base.py` 是全部容错原语的唯一实现处；其余模块只声明"我要哪些字段"。

为什么叫 coercer 而不是 parser
------------------------------
因为这些函数的契约是**永不抛错**，只做尽力而为的解读并报告做过哪些修复。
一次 JSON 畸形不该让 12 个章节的写作全部回退——那是参考实现踩过的坑：
单次巨型调用产出的 JSON 一旦坏掉，整份报告带着空图表和一个
看起来很自信的指标面板渲染出来，而失败完全不可见。

这里的替代方案是：每个模块各自解析、各自失败、各自降级，
并把 `degraded` 标记与修复记录一路带到报告里。
"""
from app.core.schemas.base import (
    CoercionReport,
    as_bool,
    as_float,
    as_int,
    as_list,
    as_list_of_dicts,
    as_str,
    normalize_evidence_ids,
    parse_json_object,
    pick,
)

__all__ = [
    "CoercionReport",
    "as_bool",
    "as_float",
    "as_int",
    "as_list",
    "as_list_of_dicts",
    "as_str",
    "normalize_evidence_ids",
    "parse_json_object",
    "pick",
]
