"""JSON 列的编解码。

SQLite 没有 JSON 类型，所以结构化的值一律存成 TEXT。集中在这里做，
是因为"存进去和读出来必须是同一件事"需要一处保证：
`load_json` 遇到损坏的内容返回默认值而不是抛错——一条修不好的旧行
不该让整个列表页打不开。
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_json(raw: Any, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default if default is not None else {}
    if isinstance(raw, (dict, list)):
        return raw  # 调用方已经给了解析好的值
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("JSON 列内容损坏，已退回默认值：%.120s", raw)
        return default if default is not None else {}


def load_str_list(raw: Any) -> list[str]:
    value = load_json(raw, [])
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def load_dict_list(raw: Any) -> list[dict]:
    value = load_json(raw, [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def load_dict(raw: Any) -> dict:
    value = load_json(raw, {})
    return value if isinstance(value, dict) else {}
