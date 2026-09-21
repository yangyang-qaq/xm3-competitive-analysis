"""专家队形校验。

`validate_team()` 是"专家 id 必须存在"和"三层分工"这两条不变式的
**唯一执行点**。它原先一条测试都没有——这是这个模块存在的理由。

两道关，各自对应的失败模式完全不同：

- **id 存在性**：模型编了一个不存在的号。名册从 15 人扩到 48 人之后
  这种事会变少，但不会消失。
- **层级相符**：号是真的，人被放错了层。原先没有这道关，于是一个战略层
  的人可以被指派成组长而无人反对——而"三层分工"是这个产品对外的核心说法。
  名册扩容**让这个漏洞更容易被踩到**：48 人里 36 个是执行层，
  "模型挑了个执行层来当组长"从一个边角情况变成了一件常事。

还有一条测试守的是**文案**（`describe()`）：三种问题原先被压成一个数字
"N 个专家 id 不存在于名册"，而其中只有第一种是真的不存在。观测通道说的
每一句话都必须是真的，否则"全程可观测"就只剩一个好看的界面。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.pipeline.dispatch import LevelMismatch, _extract_id, validate_team
from app.core.schemas.base import as_list, as_str, pick
from app.data import DEFAULT_TEAM, expert_by_id


def _payload(**overrides) -> dict:
    """一份各组都合法的队伍，按需覆盖其中一类。"""
    base = {
        "lead": "L3-001",
        "strategists": ["L2-001", "L2-002"],
        "executors": ["L1-001", "L1-026"],
    }
    base.update(overrides)
    return base


# ============================================================
# 干净的队伍
# ============================================================


def test_a_clean_team_passes_untouched() -> None:
    outcome = validate_team(_payload())

    assert outcome.team == {
        "lead": ["L3-001"],
        "strategists": ["L2-001", "L2-002"],
        "executors": ["L1-001", "L1-026"],
    }
    assert not outcome.has_problems
    assert outcome.phantom == []
    assert outcome.over_limit == []
    assert outcome.wrong_level == []
    assert outcome.defaulted_roles == []


def test_describe_says_nothing_when_clean() -> None:
    """干净时返回空串，调用方用 `has_problems` 决定要不要发降级。

    返回 `"无"` 之类的占位文字很危险：调用方一旦直接把它当横幅发出去，
    工作台上就会出现一条"专家调度：无"的降级提示。
    """
    assert validate_team(_payload()).describe() == ""


def test_the_roles_keep_their_own_aliases() -> None:
    """模型会把字段名写成中文、驼峰或下划线，三种都要读得出来。"""
    outcome = validate_team({"组长": "L3-002", "战略层": ["L2-003"], "执行层": ["L1-005"]})

    assert outcome.team["lead"] == ["L3-002"]
    assert outcome.team["strategists"] == ["L2-003"]
    assert outcome.team["executors"] == ["L1-005"]
    assert not outcome.has_problems


def test_a_single_string_is_read_as_a_one_element_team() -> None:
    """`lead` 是单值而其余是列表（见提示词里的输出格式）。
    模型把一个列表写成裸字符串时不该整类丢掉。"""
    assert validate_team(_payload(executors="L1-001")).team["executors"] == ["L1-001"]


# ============================================================
# 编造的 id
# ============================================================


def test_phantom_ids_are_dropped_and_reported() -> None:
    outcome = validate_team(_payload(executors=["L1-001", "L1-099"]))

    assert outcome.team["executors"] == ["L1-001"]
    assert outcome.phantom == ["L1-099"]


def test_a_whole_role_of_phantoms_falls_back_to_the_default_team() -> None:
    """**绝不因为一次调度失败就让整个任务失败。**

    用默认队伍跑出来的报告，比一个"调度失败"的错误页有用得多。
    """
    outcome = validate_team(_payload(strategists=["L2-101", "L2-102"]))

    assert outcome.team["strategists"] == DEFAULT_TEAM["strategists"]
    assert outcome.phantom == ["L2-101", "L2-102"]
    assert outcome.defaulted_roles == ["战略层"]


def test_missing_keys_fall_back_entirely() -> None:
    """整个响应是空的时候，三个角色都退回默认，而不是抛错。"""
    outcome = validate_team({})

    assert outcome.team == {role: list(ids) for role, ids in DEFAULT_TEAM.items()}
    assert outcome.defaulted_roles == ["组长", "战略层", "执行层"]


def test_blank_entries_are_ignored_not_reported() -> None:
    """空串是"模型没填"，不是"模型编了个 id"。

    把它记成幻觉会让降级横幅报出一个不存在的数量，
    而那个数字会被写进报告。
    """
    outcome = validate_team(_payload(executors=["", "  ", "L1-001"]))

    assert outcome.team["executors"] == ["L1-001"]
    assert outcome.phantom == []
    assert not outcome.has_problems


# ============================================================
# 层级错配（原先完全没有这道关）
# ============================================================


@pytest.mark.parametrize(
    ("role", "wrong_id", "expected_level"),
    [
        ("lead", "L2-001", "L3"),
        ("lead", "L1-026", "L3"),
        ("strategists", "L3-002", "L2"),
        ("strategists", "L1-026", "L2"),
        ("executors", "L3-002", "L1"),
        ("executors", "L2-001", "L1"),
    ],
)
def test_an_id_from_the_wrong_layer_is_rejected(role: str, wrong_id: str, expected_level: str) -> None:
    outcome = validate_team(_payload(**{role: [wrong_id]}))

    assert outcome.team[role] == DEFAULT_TEAM[role], "错层的人必须被挡掉并整类回退"
    assert len(outcome.wrong_level) == 1
    mismatch = outcome.wrong_level[0]
    assert isinstance(mismatch, LevelMismatch)
    assert mismatch.expert_id == wrong_id
    assert mismatch.expected == expected_level


def test_a_wrong_layer_id_is_not_reported_as_a_phantom() -> None:
    """**这是对 `describe()` 那句话说错了的回归测试。**

    `L2-007` 是真实存在的（品牌与传播分析师，宋允）。把它当组长时，
    它的问题是"层级不对"，不是"不存在"。原先的文案会把这件事说成
    "有 1 个专家 id 不存在于名册"——一句听起来很确定、但完全不对的话。
    实测那次报告里的"有 11 个专家 id 不存在于名册"，里面就混着这种。
    """
    outcome = validate_team(_payload(lead="L2-007"))

    assert outcome.phantom == [], "存在的 id 不能被报成不存在"
    assert [m.expert_id for m in outcome.wrong_level] == ["L2-007"]
    assert "不在名册" not in outcome.describe()
    assert "层级不符" in outcome.describe()


def test_describe_names_the_role_and_both_levels() -> None:
    """横幅要能回答"谁、期望哪层、实际哪层"，否则读到它的人还得去查名册。"""
    text = validate_team(_payload(lead="L2-007")).describe()

    assert "组长" in text
    assert "L3" in text
    assert "L2-007" in text


# ============================================================
# 上限与去重
# ============================================================


def test_over_limit_ids_are_cut_but_not_called_invalid() -> None:
    """超上限是**我们的取舍**，不是模型的错。两者混为一谈会让降级横幅撒谎。

    战略层上限 3 人：给 5 个合法的，前 3 个留下，后 2 个记进 `over_limit`。
    """
    given = ["L2-001", "L2-002", "L2-003", "L2-004", "L2-005"]
    outcome = validate_team(_payload(strategists=given))

    assert outcome.team["strategists"] == given[:3]
    assert outcome.over_limit == given[3:]
    assert outcome.phantom == []
    assert "上限" in outcome.describe()


def test_the_lead_is_capped_at_one() -> None:
    """组长只能有一个。多给的人被截掉，而不是让报告出现两个定调的人。"""
    outcome = validate_team(_payload(lead=["L3-001", "L3-002"]))

    assert outcome.team["lead"] == ["L3-001"]
    assert outcome.over_limit == ["L3-002"]


def test_a_repeated_id_does_not_consume_a_slot() -> None:
    """模型把同一个人列两次是常事（它以为在强调）。

    去重发生在切片之前，所以重复项既不占名额也不进 `over_limit`——
    否则"列了两次"会挤掉一个本来能进来的合法人选。
    """
    outcome = validate_team(_payload(strategists=["L2-001", "L2-001", "L2-002"]))

    assert outcome.team["strategists"] == ["L2-001", "L2-002"]
    assert outcome.over_limit == []
    assert not outcome.has_problems


def test_the_default_team_satisfies_its_own_contract() -> None:
    """把兜底队伍本身喂给校验，必须零问题。

    它是一份硬编码的 id 列表，只在"调度已经失败"时才被用到，
    所以它坏了也没人会看见——除非在这里守一道。
    """
    outcome = validate_team({role: list(ids) for role, ids in DEFAULT_TEAM.items()})

    assert not outcome.has_problems
    assert outcome.team == {role: list(ids) for role, ids in DEFAULT_TEAM.items()}


# ============================================================
# 模型把名册整行抄回来（实测的形态，不是想象出来的）
# ============================================================
#
# 上面所有测试都喂**裸 id**，而这正是不变量里唯一没被量过的那个假设：
# 名册是 `- L3-002 首席分析官` 这个形状喂给模型的，模型很自然地整行抄回来。
# 录制下来的 6 条真实调度响应里有 3 条就是长格式——**一半**。
#
# 那时 `validate_team` 拿"L3-002 首席分析官"整串去查字典，查不到，
# 就记进 `phantom`；三个角色全空，于是整类退回默认队伍。报告照常产出，
# 横幅上写着"N 个 id 不在名册"——**把模型挑对的人说成了它在编**。
# 48 人动态组队这个对外的主打能力，在 5 条真实 query 里有 3 条没生效。


def test_模型抄回名册整行时仍然认得出来() -> None:
    """最短的那个回归：长格式必须解析出 id。"""
    outcome = validate_team(
        _payload(lead="L3-002 首席分析官", strategists=["L2-002 定价与商业模式分析师"])
    )

    assert outcome.team["lead"] == ["L3-002"]
    assert outcome.team["strategists"] == ["L2-002"]
    assert outcome.phantom == [], "真实存在的人不能被报成不存在"
    assert "不在名册" not in outcome.describe()


def test_整队都是长格式时不会退回默认队伍() -> None:
    """这条才是那个 bug 的完整形状：三类都是长格式 → 三类曾经全部失效。"""
    outcome = validate_team(
        {
            "lead": "L3-002 首席分析官",
            "strategists": ["L2-002 定价与商业模式分析师", "L2-004 技术架构分析师"],
            "executors": ["L1-025 舆情分析师", "L1-026 数据采集工程师"],
        }
    )

    assert outcome.team == {
        "lead": ["L3-002"],
        "strategists": ["L2-002", "L2-004"],
        "executors": ["L1-025", "L1-026"],
    }
    assert outcome.defaulted_roles == [], "一条都不该退回默认队伍"
    assert not outcome.has_problems


def test_长格式里编造的号仍然被认出来是编造的() -> None:
    """归一化**不能宽到把编造的号也认下来**。

    这是上面两条的对侧：如果解析放宽到"模糊匹配名册里的某个人"，
    `phantom` 就永远为空了——而它是这套机制唯一的报警器。
    所以这里只认形状：`L2-099` 形状是对的、人是不存在的，仍然记 phantom，
    只是记的号本身，不是那一整串（横幅要短）。
    """
    outcome = validate_team(_payload(lead="L2-099 财务分析师"))

    assert outcome.phantom == ["L2-099"]
    assert outcome.defaulted_roles == ["组长"]
    assert "不在名册" in outcome.describe()


def test_连号都没有的一串文字算认不出来() -> None:
    """只写了岗位名、没写号：模型想指定一个人，但我们不知道是谁。

    这种情况**必须留痕**——静默跳过它，表现就是"这一类悄悄退回默认队伍
    而横幅什么都不说"，那是这套机制最坏的失败方式。
    """
    outcome = validate_team(_payload(executors=["舆情分析师", "L1-025"]))

    assert outcome.phantom == ["舆情分析师"]
    assert outcome.team["executors"] == ["L1-025"]


# ============================================================
# 拿**真实录制**跑一遍：不变量要对着真数据成立，不是对着夹具
# ============================================================

CASSETTE = Path(__file__).resolve().parents[1] / "fixtures" / "cassettes" / "llm.deepseek.jsonl"


def _recorded_dispatches() -> list[dict]:
    """从 cassette 里取出所有真实的调度响应。

    读文件而不是"再手写一份像真的 JSON"：这两者的区别就是这整个 bug
    被漏掉的原因——手写的夹具里 id 永远干干净净。
    """
    if not CASSETTE.exists():
        pytest.fail(f"找不到 {CASSETTE}，这条测试的意义就是对着真实录制跑")
    out = []
    for line in CASSETTE.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if (record.get("meta") or {}).get("purpose") != "dispatch":
            continue
        try:
            out.append(json.loads(record["value"]["text"]))
        except (KeyError, json.JSONDecodeError):
            continue  # 录制里也有模型没吐 JSON 的次品，那不是这条测试要管的
    return out


def test_真实录制里的调度响应一条都不许把真专家报成幻觉() -> None:
    """**核心不变量**：我们从不把名册里真实存在的人说成"不存在"。

    这条断言只指向一件事，而它正是 `phantom` 这个字段存在的意义：
    它是对模型的一项**指控**（"你在编 id"）。只要有一份真实响应里的 id
    其实在名册里，这个指控就是假的——观测通道说了假话，
    而"全程可观测"就只剩一个好看的界面。

    用真实录制而不是夹具，是因为夹具不会犯错，而模型会：
    这批录制里就同时存在裸 id 和长格式两种写法。
    """
    payloads = _recorded_dispatches()
    assert payloads, "cassette 里一条 dispatch 都没有，这条测试失去意义"

    for index, payload in enumerate(payloads):
        outcome = validate_team(payload)
        raws: list[str] = []
        for role in ("lead", "strategists", "executors"):
            value = pick(payload, role)
            raws.extend(t for t in (as_str(v) for v in as_list(value)) if t)

        for text in raws:
            resolved = _extract_id(text)
            if not resolved or expert_by_id(resolved) is None:
                continue  # 这段里没有真实存在的号，报成 phantom 是对的
            offenders = [p for p in outcome.phantom if resolved in p]
            assert not offenders, (
                f"第 {index} 条真实调度响应里，`{text}` 里的 {resolved} "
                f"是名册里真实存在的人，却被报成了不存在：{offenders}。"
                "`phantom` 是对模型的指控，它必须只包含真的编造的号。"
            )

