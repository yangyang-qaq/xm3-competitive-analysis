"""名册加载器。**只读 `experts.json`，不做任何判断。**

这份文件取代了阶段 2 的临时名册 `roster.py`（15 人）。它不再手写任何人，
也不再自己算配色或图标——那些字段在 `experts.json` 里已经生成好了，
由 `scripts/gen_experts.py` 保证一致，由 `tests/unit/test_experts.py` 守着。

为什么表现层的字段要落进 JSON，而不是在这里算
--------------------------------------------
配色、徽章、图标这三样从前是 `Expert` 上的 `@property`，运行期现算。
改成生成物有三个理由：

1. **生成器才有可能"重复生成字节一致"。** 字段若在运行期推出来，
   `gen_experts.py` 就什么都不产出，"生成而非手写"这句话也就没有落点。
2. **`experts.json` 因此是一份自解释的产物。** 审阅者不必跑 Python
   就能数出 48 个人、看到分布、确认每个人都有配色，这在评审时是省事的。
3. **前端不必把配色表复制一份到 TS。** 接口返回什么就渲染什么，
   "Python 里的颜色和前端里的颜色不一致"这件事在结构上不会发生。

代价是：改配色要重新生成并提交 JSON。这个代价是好的——它让改动**可见**，
而不是藏在一个没人会去读的属性里悄悄生效。

`roster_digest()`：为什么它必须挤得下全部 48 个 id
--------------------------------------------------
这个字符串会被塞进调度调用的 prompt（`pipeline/dispatch.py:91, 132`），
它的唯一使命是**告诉模型合法 id 空间有多大**。名册只有 15 人时，
模型编出了 11 个不存在的 id——不是模型不听话，是我们没把它能选的告诉它。

所以摘要的优先级是明确的：**id 与岗位名一个都不能少，一句话简介是可选部分。**
先按"每人一行（id + 岗位）"铺底，有余量再给决策层与战略层补上一句话简介
（这两层的"立场"对规划有实际影响，执行层的岗位名本身已经说明了专长）。
超长时截断的是简介，不是人——截掉人就会把已经修好的问题重新引回来。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

#: `experts.json` 与本模块同目录。
_EXPERTS_PATH = Path(__file__).resolve().parent / "experts.json"

#: 摘要的默认预算（字符数）。
#:
#: 2600 不是拍的：全量"id + 岗位"约 1300 字，加上决策层与战略层
#: （12 人）的一句话简介约 550 字，共约 1850 字，留了四成余量。
#: **余量是刻意的**——它让"再加几个人"不会突然把执行层挤出上下文。
#: `tests/unit/test_experts.py` 断言默认预算下 48 个 id 全部出现且无截断标记，
#: 所以余量真的用完时测试会红，而不是报告悄悄变差。
DIGEST_BUDGET = 2600

#: 摘要的层级顺序：先决策，再战略，最后执行。
#: 与 prompt 的阅读顺序一致，也让"这份报告由谁定调"在第一屏就看得出来。
_LEVEL_ORDER = ("L3", "L2", "L1")

#: 补齐一句话简介的层级。执行层的岗位名（"舆情分析师"）本身已说明专长，
#: 而决策层与战略层的价值在立场上，那必须写出来。
_DIGEST_DETAIL_LEVELS = frozenset({"L3", "L2"})


@dataclass(frozen=True)
class Expert:
    """名册里的一个人。

    字段全部来自 `experts.json`，**没有派生属性**。加一个字段时只改两处：
    `gen_experts.py`（生成它）与这里（读它）。`to_dict()` 与 JSON 的键
    逐字对应，所以"接口返回的形状"与"落盘的形状"不可能漂开——
    它们读的是同一份东西。
    """

    expert_id: str
    level: str
    level_label: str
    group: str
    name: str
    role_title: str
    one_liner: str
    skills: tuple[str, ...] = ()
    knowledge_base: str = ""
    knowledge_tags: tuple[str, ...] = ()
    avatar_color: str = ""
    badge_color: str = ""
    domain_icon: str = ""
    #: 初始统计行。`source == "seed"` 表示**还没有被量过**，
    #: 里面的 0 是"此刻正确"而不是"占位"。接口与前端据此决定要不要显示。
    stats: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Expert:
        return cls(
            expert_id=str(row["id"]),
            level=str(row["level"]),
            level_label=str(row.get("levelLabel") or row["level"]),
            group=str(row["group"]),
            name=str(row["name"]),
            role_title=str(row.get("roleTitle") or ""),
            one_liner=str(row.get("oneLiner") or ""),
            skills=tuple(str(item) for item in row.get("skills") or ()),
            knowledge_base=str(row.get("knowledgeBase") or ""),
            knowledge_tags=tuple(str(item) for item in row.get("knowledgeTags") or ()),
            avatar_color=str(row.get("avatarColor") or ""),
            badge_color=str(row.get("badgeColor") or ""),
            domain_icon=str(row.get("domainIcon") or ""),
            stats=dict(row.get("stats") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """接口形状。camelCase，与 `experts.json` 的键逐字相同。"""
        return {
            "expertId": self.expert_id,
            "level": self.level,
            "levelLabel": self.level_label,
            "group": self.group,
            "name": self.name,
            "roleTitle": self.role_title,
            "oneLiner": self.one_liner,
            "skills": list(self.skills),
            "knowledgeBase": self.knowledge_base,
            "knowledgeTags": list(self.knowledge_tags),
            "badgeColor": self.badge_color,
            "avatarColor": self.avatar_color,
            "domainIcon": self.domain_icon,
            "stats": dict(self.stats),
        }


#: 确定性默认队伍。模型给的 id 全部非法时用它——
#: **绝不因为一次调度失败就让整个任务失败**：一份用默认队伍跑出来的
#: 报告，比一个"调度失败"的错误页有用得多。
#:
#: 这里的 id 是锁定的，`tests/unit/test_experts.py` 断言它们都还在名册里。
#: 名册扩容时新增的人不改变这份默认队伍——它是"兜底"，不是"最佳"，
#: 换掉它会让同一个 query 在调度失败前后选到不同的人，报告不可比。
DEFAULT_TEAM: dict[str, list[str]] = {
    "lead": ["L3-001"],
    "strategists": ["L2-001", "L2-002", "L2-003"],
    "executors": ["L1-026", "L1-025", "L1-002"],
}


@lru_cache(maxsize=1)
def _payload() -> dict[str, Any]:
    raw = json.loads(_EXPERTS_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("experts"), list):
        raise RuntimeError(
            f"{_EXPERTS_PATH.name} 结构不对：顶层应是含 `experts` 列表的映射。"
            "请运行 python -m scripts.gen_experts 重新生成。"
        )
    return raw


@lru_cache(maxsize=1)
def _by_id() -> dict[str, Expert]:
    return {expert.expert_id: expert for expert in load_experts()}


def reload_experts() -> None:
    """清掉缓存。测试替换 `experts.json` 之后必须调用。"""
    _payload.cache_clear()
    _by_id.cache_clear()


def load_experts() -> list[Expert]:
    """全部 48 人，按层（决策 → 战略 → 执行）排序。"""
    return [Expert.from_row(row) for row in _payload()["experts"]]


def expert_by_id(expert_id: str) -> Expert | None:
    return _by_id().get(expert_id)


def known_ids() -> set[str]:
    """合法专家 id 集合。

    调度阶段用它过滤模型编造的 id。**这一步必须有**：
    模型很自然地会编出一个不存在的 `L2-007`，而如果不校验，
    它会一路走进专家统计表，污染那里的"每位专家参与了多少任务"，
    而且没有任何地方会报错。
    """
    return set(_by_id())


def experts_by_level(level: str) -> list[Expert]:
    return [expert for expert in load_experts() if expert.level == level]


def roster_size() -> int:
    """名册人数。接口与文档用它，避免在多处硬编码 48。"""
    return len(_payload()["experts"])


def distribution() -> dict[str, int]:
    """按"层级·分组"计数。名册页与文档统计表用它。"""
    counts: dict[str, int] = {}
    for expert in load_experts():
        key = f"{expert.level}·{expert.group}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def roster_digest(*, max_chars: int = DIGEST_BUDGET) -> str:
    """喂给调度器的紧凑名册。**必须挤得下全部 48 个 id。**

    两档：先试"id + 岗位 + 上两层的一句话简介"，超预算就退到
    "id + 岗位"。两档都含全部 48 人——**简介是可选部分，人不是**。

    截断分支仍然保留，但它是一道**兜底**：真走到那里说明有人加进了
    长得离谱的岗位名，而那时宁可留下一个显式的"已截断"标记，
    也不要一份看起来完整、实际少了几个人的名册。
    """
    lean: list[str] = []
    rich: list[str] = []
    for level in _LEVEL_ORDER:
        members = experts_by_level(level)
        if not members:
            continue
        label = members[0].level_label or level
        header = f"【{label}】共 {len(members)} 人"
        lean.append(header)
        rich.append(header)
        for expert in members:
            base = f"- {expert.expert_id} {expert.role_title}"
            lean.append(base)
            if level in _DIGEST_DETAIL_LEVELS and expert.one_liner:
                rich.append(f"{base}：{expert.one_liner}")
            else:
                rich.append(base)

    for candidate in (rich, lean):
        text = "\n".join(candidate)
        if len(text) <= max_chars:
            return text

    marker = "\n…（名册已截断）"
    return "\n".join(lean)[: max_chars - len(marker)] + marker
