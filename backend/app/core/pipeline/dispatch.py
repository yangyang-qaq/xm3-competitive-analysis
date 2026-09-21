"""阶段 2：专家调度与调研规划。

两件事放在一个阶段里：**组队**和**定维度**。它们本来就是一个决定的两面——
"这次调研需要什么专业视角"和"要调研哪些维度"在真实项目里是同一个
判断的产物。分开会让模型在两次调用之间失去彼此的信息。

编造的 id 必须被过滤
--------------------
模型很自然地会写出一个不存在的号（名册只有 15 人时它编出过 `L2-007`，
那时这个号确实是空的）。不校验的话它会一路走进 `expert_stats` 表，
污染"每位专家参与了多少任务"，而且**没有任何地方会报错**——
统计页上的数字看起来完全正常，只是有一行属于一个不存在的专家。

所以流程是：逐个 id 查名册 → 查不到的丢掉并记录 → 某一类全部丢掉时
用确定性默认队伍补上。**绝不因为一次调度失败就让整个任务失败**：
用默认队伍跑出来的报告，比一个"调度失败"的错误页有用得多。

两道关，不只是"id 存在"
-----------------------
id 存在还不够。48 人名册里有 36 个执行层，"号是真的但人被放错了层"
因此比"号是编的"更常见——一个战略层的人被指派成组长，在数据上
和一次正常调度长得一模一样。`_ROLE_SPECS` 里的层级要求是第二道关。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.modes import plan_dimensions
from app.core.pipeline.calls import chat_json
from app.core.pipeline.context import PipelineContext
from app.core.pipeline.prompts import dispatch_prompt, plan_prompt
from app.core.schemas.base import as_list, as_str, pick
from app.data import DEFAULT_TEAM, expert_by_id, roster_digest

#: 各类角色：角色名、**要求的层级**、数量上限、可接受的字段别名。
#:
#: 上限存在是因为"人多不等于分析好"——每位专家都要在提示词里占位置，
#: 人多会把真正需要的材料挤掉。
#:
#: **层级这一项原先漏了，那是一个真的漏洞。** 校验只查"这个 id 在不在名册里"，
#: 不查"它在哪一层"，于是一个战略层的 id 可以被指派成组长而无人反对——
#: 而三层分工是这个产品对外的核心说法。名册从 15 人扩到 48 人之后这个漏洞
#: 只会更容易被踩到：48 人里有 36 个执行层，"模型挑了个执行层当组长"
#: 从一个边角情况变成了一件常事。
_ROLE_SPECS: tuple[tuple[str, str, int, tuple[str, ...]], ...] = (
    ("lead", "L3", 1, ("lead", "lead_expert", "组长", "负责人")),
    ("strategists", "L2", 3, ("strategists", "strategist", "战略", "战略层")),
    ("executors", "L1", 4, ("executors", "executor", "执行", "执行层")),
)

#: 角色 → 中文名。只用于**给人看的文案**，不参与任何判断。
#: 单独列一份而不是从 `_ROLE_SPECS` 的别名里取：别名列表是"模型可能怎么写"
#: 的清单，会随着模型换写法而增删，而文案要的是稳定的中文标签。
_ROLE_LABELS = {"lead": "组长", "strategists": "战略层", "executors": "执行层"}

#: 名册 id 的形状。比 `tests/unit/test_experts.py` 守护的 `^L[123]-\d{3}$` 宽：
#: 那里问的是"名册里的 id 合不合格"，这里问的是"一段文本里有没有一个号"。
_ID_IN_TEXT = re.compile(r"L[123]-\d{3}", re.IGNORECASE)


def _extract_id(raw: str) -> str:
    """从模型写的一段文本里取出专家 id。取不到返回空串。

    **为什么需要这一步**：名册是 `- L3-002 首席分析官` 这个形状喂进去的，
    而模型很自然地把它整行抄回来，于是 `lead` 字段收到的是
    `"L3-002 首席分析官"` 而不是 `"L3-002"`。原先是拿这一整串去做字典查找，
    查不到就记进 `phantom`——**而 phantom 的语义是"模型在编 id"**。
    于是校验器把一个真实存在的人报成了不存在，观测通道说了假话，
    并且三个角色全类回退默认队伍：48 人动态组队这个核心能力，
    在 6 条真实调度响应里有 4 条根本没生效，而报告里那句话读起来像是模型的错。

    这段代码是照着**录制下来的真实响应**写的，不是照着想象中模型该有的
    输出格式写的——那 6 条现在有一条测试直接读它们。

    精确命中优先：`raw` 本身就是个 id 时原样返回，行为与加这层之前一致。
    否则取第一个 id 片段，并转成名册里的大小写。

    为什么不做得更宽松（比如模糊匹配编辑距离）：宽松到一定程度之后，
    "模型编的号"会被匹配成"名册里某个号"，那时 phantom 永远为空——
    而 phantom 是这套机制**唯一**的报警器。所以这里只认形状，
    不做猜测；抄回来的一整行认得出来，编出来的号仍然认不出来。
    """
    text = raw.strip()
    if expert_by_id(text) is not None:
        return text
    match = _ID_IN_TEXT.search(text)
    return match.group(0).upper() if match else ""


@dataclass(frozen=True)
class LevelMismatch:
    """一个 id 存在，但层级与角色要求不符。

    记成一个结构而不是一句拼好的话，是为了让测试能断言
    "哪个 id、期望哪层、实际哪层"，而不是去正则匹配一段文案。
    """

    expert_id: str
    role: str
    expected: str
    actual: str


@dataclass(frozen=True)
class TeamOutcome:
    """`validate_team()` 的结果。

    **四种问题分开记**，而不是合成一个 `dropped` 列表。原先的写法把
    "模型编了一个不存在的 id""某一类整类回退到默认""合法 id 超出上限被截掉"
    三种情况通通描述成"N 个专家 id 不存在于名册"，而其中只有第一种是真的
    不存在。实测那次报告里的"有 11 个专家 id 不存在于名册"，里面就混着后两种。

    **观测通道说的每一句话都必须是真的**，否则"全程可观测"这条铁律就只剩
    一个好看的界面。所以这里宁可多几个字段，也不把四类压成一个数。
    """

    team: dict[str, list[str]]
    #: 名册里查不到的 id。**这一类才是"不存在"**，通常意味着模型在编。
    #:
    #: 「查不到」的口径由 `_extract_id` 决定：它先剥掉模型抄回来的岗位名。
    #: 所以这里记的是**真的认不出是谁**——一个真实存在的专家被写成了
    #: `"L3-002 首席分析官"` 不会进这个列表。这个字段是对外的指控
    #: （"模型在编 id"），它必须只包含真的编造的号。
    phantom: list[str] = field(default_factory=list)
    #: 合法但超出该类人数上限、被截掉的 id。这是**我们的取舍**，不是模型的错。
    over_limit: list[str] = field(default_factory=list)
    #: 存在但层级不对的 id。与上一类的区别是：模型没编，它只是没读层级要求。
    wrong_level: list[LevelMismatch] = field(default_factory=list)
    #: 整类回退到默认队伍的角色（中文名）。
    defaulted_roles: list[str] = field(default_factory=list)

    @property
    def has_problems(self) -> bool:
        return bool(
            self.phantom or self.over_limit or self.wrong_level or self.defaulted_roles
        )

    def describe(self) -> str:
        """降级横幅的一句话。每一类都说清楚，且不夸大。"""
        parts: list[str] = []
        if self.phantom:
            shown = "、".join(self.phantom[:3])
            more = "…" if len(self.phantom) > 3 else ""
            parts.append(f"{len(self.phantom)} 个 id 不在名册（{shown}{more}）")
        if self.wrong_level:
            first = self.wrong_level[0]
            parts.append(
                f"{len(self.wrong_level)} 个 id 层级不符"
                f"（{first.role}需要 {first.expected}，拿到 {first.expert_id} 是 {first.actual}）"
            )
        if self.over_limit:
            parts.append(f"{len(self.over_limit)} 个合法 id 超出该类上限被舍去")
        if self.defaulted_roles:
            parts.append(f"{'、'.join(self.defaulted_roles)}整类退回默认队伍")
        return "；".join(parts)


def validate_team(payload: dict) -> TeamOutcome:
    """校验模型给的队伍。

    两道关：**id 存在**，且**层级与角色相符**。第二道原先没有，
    于是"三层分工"这个核心说法在数据上是不设防的。

    纯函数、可单测。**48 人名册让编造的 id 变少了，但不会让它消失**，
    所以这一步不是名册扩容之后就能拆掉的脚手架。
    """
    outcome = TeamOutcome(team={})

    for role, level, limit, aliases in _ROLE_SPECS:
        raw = as_list(pick(payload, *aliases))
        resolved: list[str] = []
        for item in raw:
            text = as_str(item)
            if not text:
                continue
            expert_id = _extract_id(text)
            expert = expert_by_id(expert_id) if expert_id else None
            if expert is None:
                # 记 `expert_id or text`：能取到号就记号（短、可读），
                # 取不到才记原文。两种情况都是"认不出这是谁"。
                outcome.phantom.append(expert_id or text)
                continue
            if expert.level != level:
                outcome.wrong_level.append(
                    LevelMismatch(
                        expert_id=expert_id,
                        role=_ROLE_LABELS.get(role, role),
                        expected=level,
                        actual=expert.level,
                    )
                )
                continue
            if expert_id not in resolved:
                resolved.append(expert_id)
        outcome.team[role] = resolved[:limit]
        outcome.over_limit.extend(resolved[limit:])

    # 任何一类为空就整类退回默认。混着来会让"这次用了默认队伍"
    # 这件事变得难以说明；整类回退时报告里能明确写出是哪一类。
    for role, fallback in DEFAULT_TEAM.items():
        if not outcome.team.get(role):
            outcome.team[role] = list(fallback)
            outcome.defaulted_roles.append(_ROLE_LABELS.get(role, role))

    return outcome


def _clean_dimensions(raw: object, mode, fallback: list[str]) -> list[str]:
    dimensions: list[str] = []
    for item in as_list(raw):
        name = as_str(item)
        if name and name not in dimensions:
            dimensions.append(name)
    if not dimensions:
        dimensions = fallback
    return plan_dimensions(mode, dimensions)


async def run(ctx: PipelineContext) -> None:
    ctx.begin_stage("orchestrator")
    lead = ctx.lead_expert

    scope = {
        "subject": ctx.subject,
        "domain": ctx.domain,
        "candidateBrands": ctx.candidate_brands,
    }
    payload, report = chat_json(
        ctx,
        plan_prompt(ctx.query, scope, ctx.clarify_answers, roster_digest()),
        tier=ctx.mode.analysis_tier,
        purpose="plan",
        max_tokens=1536,
    )
    ctx.coercion.merge(report)

    if not payload:
        raise RuntimeError("专家调度阶段没有拿到可解析的规划结果")

    planned_brands = [
        as_str(item) for item in as_list(pick(payload, "brands", "品牌")) if as_str(item)
    ]
    if planned_brands:
        # 用规划结果覆盖 intake 的候选，但保留调研对象在首位。
        merged = [ctx.subject] if ctx.subject else []
        for brand in planned_brands:
            if brand not in merged:
                merged.append(brand)
        ctx.brands = merged[: ctx.mode.max_brands]

    ctx.dimensions = _clean_dimensions(
        pick(payload, "dimensions", "维度"), ctx.mode, ["功能覆盖", "定价策略", "用户口碑"]
    )
    angles = [
        as_str(item) for item in as_list(pick(payload, "searchAngles", "search_angles", "搜索角度"))
        if as_str(item)
    ]
    ctx.search_angles = angles or ["官方介绍", "定价页", "第三方评测", "用户讨论"]
    ctx.plan_rationale = as_str(pick(payload, "rationale", "理由"))

    ctx.thought(
        lead,
        f"确定 {len(ctx.dimensions)} 个调研维度：{'、'.join(ctx.dimensions)}。"
        f"搜索角度 {len(ctx.search_angles)} 个。{ctx.plan_rationale}",
        stage="orchestrator",
    )

    # ---- 组队 ----
    team_payload, team_report = chat_json(
        ctx,
        dispatch_prompt(ctx.query, ctx.dimensions, roster_digest()),
        tier="fast",
        purpose="dispatch",
        max_tokens=768,
        required=False,
    )
    ctx.coercion.merge(team_report)

    outcome = validate_team(team_payload)
    ctx.team = outcome.team
    if outcome.has_problems:
        # 记录**结构化**的原始信息（供 trace 与调试），横幅只放一句人话。
        # 两者不合并：横幅要短，而调查"这次到底丢了哪些 id"需要全量。
        ctx.coercion.note(
            "调度阶段整理了队伍："
            f"编造 {outcome.phantom or '无'}；"
            f"层级不符 {[m.expert_id for m in outcome.wrong_level] or '无'}；"
            f"超上限 {outcome.over_limit or '无'}；"
            f"整类回退 {outcome.defaulted_roles or '无'}"
        )
        ctx.degrade("专家调度", outcome.describe())

    def names_of(role: str) -> str:
        members = [expert_by_id(eid) for eid in ctx.team.get(role, [])]
        return "、".join(member.name for member in members if member) or "（默认）"

    ctx.thought(
        lead,
        f"组建团队：组长 {names_of('lead')}；"
        f"战略 {names_of('strategists')}；"
        f"执行层 {len(ctx.team.get('executors', []))} 人。",
        stage="orchestrator",
    )

    for strategist in ctx.team.get("strategists", [])[:2]:
        ctx.thought(
            strategist,
            f"从我的角度看，{'、'.join(ctx.dimensions[:3])} 是这次最该先钉死的维度。",
            stage="orchestrator",
        )

    ctx.finish_stage(
        "orchestrator",
        detail={
            "dimensions": ctx.dimensions,
            "brands": ctx.brands,
            "team": ctx.team,
        },
    )
