"""容错原语。

设计契约
--------
**这里的函数永不抛错。** 它们尽最大努力把模型输出解读成想要的样子，
并把"为了读懂它我做了哪些妥协"记进返回值。

理由不是懒惰，而是失败模式：LLM 的结构化输出会以各种方式轻微畸形——
包了 markdown 围栏、用了中文引号、把数字写成 `"68元"`、多一个尾逗号、
字段名从 `evidence_ids` 变成 `evidenceIds`。抛错的话，每一种畸形
都要在调用点写一个 try，而漏写的那个就是一个静默的空报告。

一个刻意的取舍：**修复要记账**
------------------------------
参考实现直接 `json.loads` 然后 `[... ] or fallback`，修复过程不留痕。
这里的每一次修复都进 `CoercionReport.repairs`，最终进报告的降级披露。
"这份报告是 3 次修复之后才读出来的"是读者有权知道的事。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

#: 取值失败时的哨兵。用哨兵而不是 None，因为 None 可能是**合法的**值
#: （比如"没有免费额度"），两者混淆会让"字段缺失"和"字段为空"变成同一件事。
_MISSING = object()


@dataclass
class CoercionReport:
    """一次解析做过的全部妥协。"""

    repairs: list[str] = field(default_factory=list)
    #: 模型引用过但系统里不存在的 evidence_id。幻觉引用率的分子。
    phantom_ids: list[str] = field(default_factory=list)
    produced_ids: int = 0
    resolved_ids: int = 0
    #: 期望出现但完全没有的字段
    missing: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """有修复或有幻觉，就算降级。报告要披露它。"""
        return bool(self.repairs or self.phantom_ids or self.missing)

    def note(self, message: str) -> None:
        self.repairs.append(message)

    def merge(self, other: CoercionReport) -> CoercionReport:
        self.repairs.extend(other.repairs)
        self.phantom_ids.extend(other.phantom_ids)
        self.missing.extend(other.missing)
        self.produced_ids += other.produced_ids
        self.resolved_ids += other.resolved_ids
        return self

    def to_dict(self) -> dict:
        return {
            "degraded": self.degraded,
            "repairs": list(self.repairs),
            "phantomIds": list(self.phantom_ids),
            "producedIds": self.produced_ids,
            "resolvedIds": self.resolved_ids,
            "missing": list(self.missing),
        }


# ============================================================
# JSON 提取与修复
# ============================================================

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _strip_fence(text: str) -> tuple[str, bool]:
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip(), True
    return text, False


def _extract_balanced(text: str, opener: str = "{", closer: str = "}") -> str:
    """取出第一个括号配对完整的片段。

    必须**字符串感知**：正文里出现一个 `}` 是常事
    （"价格 }"、正则、代码片段），不识别字符串会让截取位置提前，
    得到一个"看起来像 JSON 但少了一半"的片段，然后 json.loads 报一个
    毫无帮助的错。
    """
    start = text.find(opener)
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text[start:]


def _drop_trailing_commas(text: str) -> str:
    """删掉对象/数组末尾的多余逗号。同样要字符串感知。"""
    out: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead] in " \t\r\n":
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                index += 1  # 跳过这个逗号
                continue
        out.append(char)
        index += 1
    return "".join(out)


def parse_json_object(
    text: str, *, report: CoercionReport | None = None
) -> tuple[dict, CoercionReport]:
    """从模型输出里取出一个 JSON 对象。

    依次尝试：原样 → 去围栏 → 截配对片段 → 再去尾逗号。
    每一层都记账，最后失败时返回空 dict 而不是抛错。
    """
    report = report or CoercionReport()
    if not text or not text.strip():
        report.note("模型输出为空")
        report.missing.append("<root>")
        return {}, report

    candidate = text.strip()
    stripped, had_fence = _strip_fence(candidate)
    if had_fence:
        report.note("输出去掉了 markdown 代码围栏")
        candidate = stripped

    for attempt, (name, payload) in enumerate(
        (
            ("原样解析", candidate),
            ("截取配对片段", _extract_balanced(candidate)),
            ("去除尾逗号", _drop_trailing_commas(_extract_balanced(candidate))),
        )
    ):
        if attempt and name == "截取配对片段" and payload != candidate:
            report.note("输出前后有额外文字，已截取 JSON 片段")
        if attempt == 2:
            report.note("输出含多余尾逗号，已修复")
        if not payload:
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed, report
        # 顶层是数组：包一层。模型有时直接给 `[{...}]`。
        if isinstance(parsed, list):
            report.note("顶层是数组，已包成对象")
            return {"items": parsed}, report

    report.note(f"无法解析为 JSON（前 200 字：{text[:200]!r}）")
    report.missing.append("<root>")
    return {}, report


# ============================================================
# 字段取值
# ============================================================


def _key(any_key: str) -> str:
    """把字段名归一化到可以互相匹配的形式。

    `evidenceIds` / `evidence_ids` / `Evidence-IDs` 都变成 `evidenceids`。
    这一条消掉了整类"模型换了命名风格"的解析失败——
    而模型换命名风格是必然会发生的事，不是偶发。
    """
    return re.sub(r"[\s_\-]", "", str(any_key)).lower()


def pick(mapping: Any, *names: str, default: Any = _MISSING) -> Any:
    """按若干个可能的名字取值。找不到返回 default（默认 `_MISSING`）。"""
    if not isinstance(mapping, dict):
        return default
    normalized = {_key(k): v for k, v in mapping.items()}
    for name in names:
        key = _key(name)
        if key in normalized:
            value = normalized[key]
            if value is not None:
                return value
    return default


def as_str(value: Any, *, default: str = "") -> str:
    if value is None or value is _MISSING:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, (list, tuple)):
        # 模型有时把一句话拆成一个词列表。拼回去比丢掉好。
        return "、".join(as_str(item) for item in value if item)
    if isinstance(value, dict):
        for key in ("text", "value", "name", "label", "content"):
            inner = pick(value, key)
            if inner is not _MISSING:
                return as_str(inner, default=default)
        return default
    return str(value)


_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def as_float(value: Any, *, default: float | None = None) -> float | None:
    """把一个"看起来是钱或数量"的值读成浮点数。

    `"68元/月"` → 68.0，`"1,299"` → 1299.0，`"免费"` → default。
    这正是定价表最容易踩的坑：模型很自然地会把单位和符号带上，
    而下游要的是能排序、能算中位数的数。
    """
    if value is None or value is _MISSING:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)):
        for item in value:
            found = as_float(item, default=_MISSING)  # type: ignore[arg-type]
            if found is not _MISSING:
                return found  # type: ignore[return-value]
        return default
    text = as_str(value)
    if not text:
        return default
    match = _NUMBER_RE.search(text.replace(",", "").replace("，", ""))
    if not match:
        return default
    try:
        return float(match.group())
    except ValueError:  # pragma: no cover - 正则保证不会走到
        return default


def as_int(value: Any, *, default: int = 0) -> int:
    found = as_float(value)
    return default if found is None else int(round(found))


_TRUE_WORDS = {"true", "yes", "y", "1", "是", "支持", "有", "full", "full_support"}
_FALSE_WORDS = {"false", "no", "n", "0", "否", "不", "无", "none", "partial", "部分"}


def as_bool(value: Any, *, default: bool = False) -> bool:
    if value is None or value is _MISSING:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = as_str(value).lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return default


def as_list(value: Any) -> list:
    """把任意值读成列表。`None` → `[]`，标量 → 单元素列表。"""
    if value is None or value is _MISSING:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        # 模型可能返回 `{"1": {...}, "2": {...}}` 这种编号对象。
        if value and all(str(k).isdigit() for k in value):
            return list(value.values())
        return [value]
    return [value]


def as_list_of_dicts(value: Any, *key_hints: str) -> list[dict]:
    """读出一个"对象列表"。

    模型把列表藏起来的三种常见方式：直接给列表、给一个 `{"items": [...]}`
    这样的包装、或者给单个对象而不给列表。三种都要能读出来。
    """
    if isinstance(value, dict):
        for hint in key_hints:
            inner = pick(value, hint)
            if inner is not _MISSING:
                return [item for item in as_list(inner) if isinstance(item, dict)]
        # 没有提示字段：如果这个 dict 里恰好只有一个列表值，就是它。
        lists = [v for v in value.values() if isinstance(v, list)]
        if len(lists) == 1:
            return [item for item in lists[0] if isinstance(item, dict)]
        return [value] if value else []
    return [item for item in as_list(value) if isinstance(item, dict)]


# ============================================================
# 引用归一化（幻觉计数的唯一入口）
# ============================================================

_ID_RE = re.compile(r"\bEV-[0-9a-fA-F]{6,}\b")


def normalize_evidence_ids(
    value: Any,
    known: set[str] | None = None,
    *,
    report: CoercionReport | None = None,
) -> tuple[list[str], list[str]]:
    """把任意形态的引用字段读成 `evidence_id` 列表。

    返回 `(有效的, 编造的)`。

    **这是幻觉引用率的唯一计数点。** 把它放在 coercer 里而不是各调用点，
    是因为"模型编了一个 id"必须只被计一次：分散计数的话，
    同一批输出被两个阶段各解析一次，幻觉率就会翻倍，
    而那个翻倍不会有人发现。
    """
    report = report or CoercionReport()
    known = known or set()

    raw: list[str] = []
    for item in as_list(value):
        if isinstance(item, dict):
            inner = pick(item, "evidenceId", "evidence_id", "id", "ref")
            if inner is not _MISSING:
                raw.append(as_str(inner))
            continue
        if isinstance(item, str):
            # 一个字符串里可能塞了多个 id："EV-abc, EV-def"
            raw.extend(_ID_RE.findall(item) or [item])

    valid: list[str] = []
    phantom: list[str] = []
    for candidate in raw:
        candidate = candidate.strip()
        if not candidate:
            continue
        report.produced_ids += 1
        if candidate in known:
            if candidate not in valid:
                valid.append(candidate)
                report.resolved_ids += 1
        elif candidate not in phantom:
            phantom.append(candidate)
            if candidate not in report.phantom_ids:
                report.phantom_ids.append(candidate)

    return valid, phantom


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
