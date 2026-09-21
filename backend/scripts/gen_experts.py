"""名册生成器：`experts_seed.yaml` → `experts.json`。

它生成什么，不生成什么
----------------------
生成的是**能从已有字段确定地推出来的**那些：头像配色（由 id 摘要取色）、
徽章底色（由层级查表）、分组图标（由分组查表）、初始统计行。

不生成 `status`（"空闲/工作中"）。那是一个**运行时事实**，在构建期
无从得知。把它写成静态的 `"idle"` 会得到一份"任何人一跑任务就立刻变错"
的文件，而它看起来和真的一样。

为什么不手写 JSON
-----------------
48 人 × 十几个字段 = 700 多行。手写会引入四类错误，而它们**全都不会报错**：
错别字、重复 id、层级与分组漂移、必填字段漏填。拆成"人写语义内核 +
机器生成机械字段"之后，语义部分只有 48×8 行、可以一口气读完，
机械部分由这里保证一致，语义部分的错误则由 `validate_seed()` 挡在生成之前。

`validate_seed()` 刻意做成**纯函数**并且抛 `SeedError` 而不是
`assert`：`assert` 在 `-O` 下会被去掉，而这是一道必须永远生效的闸门。
它同时被 `tests/unit/test_experts.py` 直接调用，所以"生成时校验"
和"测试里校验"是同一份逻辑，不会漂。

用法
----
    python -m scripts.gen_experts            # 生成并写出 experts.json
    python -m scripts.gen_experts --check    # 只校验，不写（CI / 测试用）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

_HERE = Path(__file__).resolve()
#: `backend/app/data/`。生成物写在这里，`loader.py` 从同一目录读它。
DATA_DIR = _HERE.parent.parent / "app" / "data"
SEED_PATH = DATA_DIR / "experts_seed.yaml"
OUT_PATH = DATA_DIR / "experts.json"

#: 名册规模与分布。**这三个数字是锁定的**，改动它们意味着产品规格变了，
#: 而不是"顺手多加了两个人"。所以写成常量并由测试断言。
EXPECTED_TOTAL = 48
#: (层级, 分组) → 人数。
EXPECTED_DISTRIBUTION: dict[tuple[str, str], int] = {
    ("L3", "决策"): 3,
    ("L2", "战略"): 9,
    ("L1", "行业"): 24,
    ("L1", "职能"): 12,
}

#: 层级 → 允许的分组。**这一条挡住的是"层级和分组各自对、合起来错"**：
#: 一个 `L2` 的人被写进"行业"组，两边的计数看起来都还说得过去，
#: 但前端会把它渲染成一个层级是战略层、图标是行业图标的怪东西。
LEVEL_GROUPS: dict[str, frozenset[str]] = {
    "L3": frozenset({"决策"}),
    "L2": frozenset({"战略"}),
    "L1": frozenset({"行业", "职能"}),
}

#: L1 层的 id 分区：行业占前 24 号，职能从 025 起。
#: 与 `roster.py` 时期定下的布局一致，**不许改**——id 已经被写进
#: `expert_stats` 表和历史报告，改号就是一次数据迁移。
L1_INDUSTRY_MAX = 24

#: id 形状。
ID_RE = re.compile(r"^L[123]-\d{3}$")

#: `one_liner` 的长度上限（字符数）。
#: 不是排版洁癖：`roster_digest()` 要把 48 人压进一次调度调用的 prompt，
#: 每人一行就是预算。超限的人会把别人挤出上下文。
ONE_LINER_MAX = 40

#: 语义内核里**必须由人写**的字段。少一个就说明这份 seed 不完整。
REQUIRED_TEXT_FIELDS = ("id", "level", "group", "name", "role_title", "one_liner", "knowledge_base")
#: 列表型必填字段及其最少条数。
REQUIRED_LIST_FIELDS = {"skills": 2, "knowledge_tags": 3}

#: 头像配色。`sha256(id) % len(palette)` 稳定取色——同一个专家在任何一次
#: 渲染里都是同一个颜色，不需要存进报告，也不依赖生成顺序。
PALETTE = (
    "#5B8FF9", "#5AD8A6", "#5D7092", "#F6BD16", "#E8684A",
    "#6DC8EC", "#9270CA", "#FF9D4D", "#269A99", "#FF99C3",
)

#: 层级 → (中文名, 徽章底色)。
LEVELS: dict[str, tuple[str, str]] = {
    "L3": ("决策层", "#E8684A"),
    "L2": ("战略层", "#F6BD16"),
    "L1": ("执行层", "#5B8FF9"),
}

#: 分组 → 图标标识。前端按它取图标，不做字符串判断。
GROUP_ICONS: dict[str, str] = {
    "决策": "compass",
    "战略": "map",
    "行业": "cube",
    "职能": "wrench",
}

#: 层级的排序权重。JSON 里按"决策 → 战略 → 执行"排，与名册的叙事顺序一致；
#: 直接按 id 排会把 36 个执行层放在最前面。
_LEVEL_RANK = {"L3": 0, "L2": 1, "L1": 2}

#: 生成字段的版本。改了取色方式或图标表就递增它，
#: 让 `experts.json` 的 diff 里能看出来"这次变化是生成器改的"。
GENERATOR_VERSION = 1


class SeedError(RuntimeError):
    """seed 不合规。

    用异常而不是 `assert`：`assert` 在 `-O` 下会被整个去掉，
    而这是一道必须在任何运行模式下都生效的闸门。
    """


# ============================================================
# 校验
# ============================================================


def _fail(problems: list[str]) -> None:
    """把问题一次性报完，不是遇到第一个就停。

    一次报一条会让人改一轮跑一轮；48 人的名册那样改要跑十几轮。
    """
    if problems:
        listed = "\n".join(f"  - {p}" for p in problems)
        raise SeedError(f"名册 seed 有 {len(problems)} 处不合规：\n{listed}")


def _id_number(expert_id: str) -> int:
    return int(expert_id.split("-")[1])


def validate_seed(entries: list[dict[str, Any]]) -> None:
    """校验语义内核。**全部问题一次报完。**

    守的不变量（每一条都对应一种"不会报错但会悄悄变坏"的失败）：

    1. 人数恰好 48；(层级, 分组) 分布恰好 3/9/24/12
    2. id 唯一且匹配 `^L[123]-\\d{3}$`
    3. 层级与分组自洽（L2 不能出现在"行业"组）
    4. L1 的 id 分区：行业 ≤ 024，职能 ≥ 025
    5. 必填字段非空；`skills` ≥ 2、`knowledge_tags` ≥ 3
    6. `one_liner` ≤ 40 字
    7. 名字不重复

    第 7 条看起来像洁癖，其实是产品问题：报告里同一段话出现两个"沈律"，
    读者无法判断这是两个不同视角还是同一个人的两句话。
    """
    problems: list[str] = []

    if len(entries) != EXPECTED_TOTAL:
        problems.append(f"人数是 {len(entries)}，应为 {EXPECTED_TOTAL}")

    seen_ids: dict[str, int] = {}
    seen_names: dict[str, str] = {}
    distribution: dict[tuple[str, str], int] = {}

    for index, entry in enumerate(entries, start=1):
        where = entry.get("id") or f"第 {index} 条"

        for field in REQUIRED_TEXT_FIELDS:
            if not str(entry.get(field) or "").strip():
                problems.append(f"{where}：必填字段 {field} 为空")

        for field, minimum in REQUIRED_LIST_FIELDS.items():
            value = entry.get(field)
            if not isinstance(value, list) or len(value) < minimum:
                got = len(value) if isinstance(value, list) else type(value).__name__
                problems.append(f"{where}：{field} 至少 {minimum} 条，实际 {got}")
                continue
            if any(not str(item).strip() for item in value):
                problems.append(f"{where}：{field} 里有空条目")

        expert_id = str(entry.get("id") or "")
        if expert_id:
            if not ID_RE.match(expert_id):
                problems.append(f"{where}：id 形状不合规，应形如 L2-007")
            if expert_id in seen_ids:
                problems.append(f"{where}：id 与第 {seen_ids[expert_id]} 条重复")
            seen_ids[expert_id] = index

        name = str(entry.get("name") or "")
        if name:
            if name in seen_names:
                problems.append(f"{where}：名字 {name} 与 {seen_names[name]} 重复")
            seen_names[name] = expert_id or where

        level = str(entry.get("level") or "")
        group = str(entry.get("group") or "")
        if level and group:
            allowed = LEVEL_GROUPS.get(level)
            if allowed is None:
                problems.append(f"{where}：层级 {level} 不是 L1/L2/L3")
            elif group not in allowed:
                problems.append(
                    f"{where}：{level} 的分组只能是 {'/'.join(sorted(allowed))}，写的是 {group}"
                )
            else:
                distribution[(level, group)] = distribution.get((level, group), 0) + 1

            if level == "L1" and group == "行业" and expert_id and _id_number(expert_id) > L1_INDUSTRY_MAX:
                problems.append(
                    f"{where}：行业组只占 L1-001..{L1_INDUSTRY_MAX:03d}，"
                    f"{expert_id} 越界了"
                )
            if level == "L1" and group == "职能" and expert_id and _id_number(expert_id) <= L1_INDUSTRY_MAX:
                problems.append(
                    f"{where}：职能组从 L1-{L1_INDUSTRY_MAX + 1:03d} 起，"
                    f"{expert_id} 越界了"
                )

        one_liner = str(entry.get("one_liner") or "")
        if len(one_liner) > ONE_LINER_MAX:
            problems.append(
                f"{where}：one_liner 有 {len(one_liner)} 字，上限 {ONE_LINER_MAX}"
            )

    if len(entries) == EXPECTED_TOTAL:
        for key, expected in EXPECTED_DISTRIBUTION.items():
            actual = distribution.get(key, 0)
            if actual != expected:
                problems.append(
                    f"分布不对：{key[0]}·{key[1]} 有 {actual} 人，应为 {expected} 人"
                )

    _fail(problems)


# ============================================================
# 生成
# ============================================================


def avatar_color(expert_id: str) -> str:
    """由 id 摘要取色。**同一个 id 永远同一个颜色**，与生成顺序无关。"""
    digest = hashlib.sha256(expert_id.encode("utf-8")).hexdigest()
    return PALETTE[int(digest[:4], 16) % len(PALETTE)]


def seed_stats() -> dict[str, Any]:
    """初始统计行。

    全是 0，并且带 `source: "seed"`——这不是"占位数字"，0 是**此刻正确**
    的值：还没有任何任务跑过，参与度就是 0。`source` 让"这个数是被量出来的
    还是被生成的"成为可判断的事实，接口和前端据此决定要不要显示它。

    参考实现的反面教材：把未标注的假数据填进面板，报告照样印出来，
    没人分得清哪一栏是真的。
    """
    return {
        "tasks": 0,
        "thoughts": 0,
        "evidences": 0,
        "costUsd": 0.0,
        "source": "seed",
    }


def generate(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """语义内核 + 生成字段 → 完整的 `experts.json` 结构。

    纯函数：同样的输入必然得到逐字节相同的输出。这一条由测试守着——
    "生成而非手写"要成立，就得先证明生成是**可复现**的，
    否则它只是把不确定性从人手挪到了机器。
    """
    validate_seed(entries)

    experts: list[dict[str, Any]] = []
    for entry in entries:
        expert_id = str(entry["id"])
        level = str(entry["level"])
        group = str(entry["group"])
        experts.append(
            {
                "id": expert_id,
                "level": level,
                "levelLabel": LEVELS[level][0],
                "group": group,
                # ---- 手写：语义 ----
                "name": str(entry["name"]),
                "roleTitle": str(entry["role_title"]),
                "oneLiner": str(entry["one_liner"]),
                "skills": [str(item) for item in entry["skills"]],
                "knowledgeBase": str(entry["knowledge_base"]),
                "knowledgeTags": [str(item) for item in entry["knowledge_tags"]],
                # ---- 生成：机械 ----
                "avatarColor": avatar_color(expert_id),
                "badgeColor": LEVELS[level][1],
                "domainIcon": GROUP_ICONS[group],
                "stats": seed_stats(),
            }
        )

    experts.sort(key=lambda item: (_LEVEL_RANK[item["level"]], item["id"]))

    return {
        "version": 1,
        "generatorVersion": GENERATOR_VERSION,
        # 生成器版本进文件，是为了让"这份 JSON 落后于生成器"可被检测。
        # 只比人数是不够的：改了取色方式而人数不变的情况也发生过。
        "count": len(experts),
        "distribution": {
            f"{level}·{group}": count
            for (level, group), count in sorted(
                EXPECTED_DISTRIBUTION.items(), key=lambda kv: (_LEVEL_RANK[kv[0][0]], kv[0][1])
            )
        },
        "experts": experts,
    }


def render(payload: dict[str, Any]) -> str:
    """序列化。**缩进与换行是固定的**，否则"字节一致"无从谈起。"""
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def load_seed(path: Path = SEED_PATH) -> list[dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("experts"), list):
        raise SeedError(f"{path.name} 的结构不对：顶层应是含 `experts` 列表的映射")
    return list(raw["experts"])


def build(seed_path: Path = SEED_PATH) -> dict[str, Any]:
    return generate(load_seed(seed_path))


# ============================================================
# 入口
# ============================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="由 experts_seed.yaml 生成 experts.json")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验并比对已生成的文件，不写盘。不一致时返回 1。",
    )
    parser.add_argument("--seed", type=Path, default=SEED_PATH)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args(argv)

    try:
        payload = build(args.seed)
    except SeedError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    text = render(payload)

    if args.check:
        if not args.out.exists():
            print(f"{args.out} 不存在，请先运行 python -m scripts.gen_experts", file=sys.stderr)
            return 1
        existing = args.out.read_text(encoding="utf-8")
        if existing != text:
            print(
                f"{args.out} 与 seed 不一致。请重新生成并提交生成物——"
                "手改 JSON 会在下次生成时被覆盖。",
                file=sys.stderr,
            )
            return 1
        print(f"OK：{payload['count']} 人，与 {args.out.name} 一致")
        return 0

    args.out.write_text(text, encoding="utf-8")
    dist = "、".join(f"{key} {value} 人" for key, value in payload["distribution"].items())
    print(f"已写出 {args.out}：{payload['count']} 人（{dist}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
