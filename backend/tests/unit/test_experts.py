"""名册不变量。

这个模块守的是**一份不会报错的错误**：名册少了几个人、某个人被写进了
错的层、id 撞了、`experts.json` 落后于 seed——这些都不会让任何东西崩，
流水线照样跑完，报告照样渲染。只是名册悄悄变得不对，而名册是
"48 专家三层分工"这个产品说法的**唯一载体**。

三类测试，各有各的职责：

1. `TestSeed` —— 对**真实的** `experts_seed.yaml` 断言产品规格
   （48 人、3/9/24/12、层级与分组自洽）。它保护的是内容。
2. `TestGeneratedArtifact` —— 生成是**幂等且已同步**的。
   它保护的是"生成而非手写"这个工程主张站得住。
3. `TestValidateSeedRejects` —— 逐条把 seed 改坏，确认每一道闸门都会响。
   它保护的是闸门本身。**只测"好的能过"是不够的**：一个永远返回
   `None` 的校验函数也能让好数据通过。
"""
from __future__ import annotations

import copy
import json

import pytest

from app.data import (
    DEFAULT_TEAM,
    DIGEST_BUDGET,
    distribution,
    expert_by_id,
    experts_by_level,
    known_ids,
    load_experts,
    roster_digest,
    roster_size,
)
from app.data.loader import _EXPERTS_PATH
from scripts.gen_experts import (
    EXPECTED_TOTAL,
    LEVELS,
    ONE_LINER_MAX,
    SEED_PATH,
    SeedError,
    avatar_color,
    build,
    generate,
    load_seed,
    render,
    validate_seed,
)


@pytest.fixture(scope="module")
def seed_entries() -> list[dict]:
    """真实 seed 的一份深拷贝。改它不会污染别的用例。"""
    return copy.deepcopy(load_seed())


@pytest.fixture
def mutated(seed_entries: list[dict]):
    """返回一个"改坏一处"的函数，每次从干净的 seed 出发。"""

    def _apply(index: int, **fields) -> list[dict]:
        entries = copy.deepcopy(seed_entries)
        entries[index].update(fields)
        return entries

    return _apply


def _index_of(expert_id: str) -> int:
    for index, entry in enumerate(load_seed()):
        if entry["id"] == expert_id:
            return index
    raise AssertionError(f"seed 里没有 {expert_id}")


# ============================================================
# 产品规格：对真实 seed 断言
# ============================================================


class TestSeed:
    def test_seed_is_valid(self, seed_entries: list[dict]) -> None:
        """真实的 seed 必须过闸门。它不过就说明仓库处于一个坏的提交上。"""
        validate_seed(seed_entries)

    def test_there_are_exactly_48(self, seed_entries: list[dict]) -> None:
        assert len(seed_entries) == EXPECTED_TOTAL == 48

    def test_distribution_is_3_9_24_12(self) -> None:
        """三层的人数分布。**这是规格，不是观察值**——改它要先改产品。"""
        assert distribution() == {
            "L3·决策": 3,
            "L2·战略": 9,
            "L1·行业": 24,
            "L1·职能": 12,
        }

    def test_ids_follow_the_locked_layout(self) -> None:
        """id 布局是锁定的：L3-001..003 / L2-001..009 / L1-001..024 行业 / L1-025..036 职能。

        id 一旦写进 `expert_stats` 表和历史报告，改它就是一次数据迁移。
        新增只能追加。
        """
        assert sorted(e for e in known_ids() if e.startswith("L3-")) == [
            f"L3-{n:03d}" for n in range(1, 4)
        ]
        assert sorted(e for e in known_ids() if e.startswith("L2-")) == [
            f"L2-{n:03d}" for n in range(1, 10)
        ]
        assert sorted(e for e in known_ids() if e.startswith("L1-")) == [
            f"L1-{n:03d}" for n in range(1, 37)
        ]

    def test_every_name_is_unique(self) -> None:
        """重名是产品问题不是洁癖：报告里同一段出现两个"沈律"，
        读者分不清这是两个视角还是同一个人的两句话。"""
        names = [expert.name for expert in load_experts()]
        assert len(names) == len(set(names))

    def test_one_liners_stay_within_the_budget(self) -> None:
        over = [
            (expert.expert_id, len(expert.one_liner))
            for expert in load_experts()
            if len(expert.one_liner) > ONE_LINER_MAX
        ]
        assert not over, f"这些人的一句话简介超了 {ONE_LINER_MAX} 字：{over}"

    def test_everyone_has_at_least_three_knowledge_tags(self) -> None:
        thin = [e.expert_id for e in load_experts() if len(e.knowledge_tags) < 3]
        assert not thin, f"知识标签少于 3 个：{thin}"

    def test_level_and_group_agree(self) -> None:
        """层级与分组各自对、合起来错——一种很难看出来的坏法：
        两边计数都还说得过去，但前端会渲染出"战略层的图标是行业"。"""
        wrong = [
            (e.expert_id, e.level, e.group)
            for e in load_experts()
            if e.group not in {"决策", "战略", "行业", "职能"}
            or e.level_label != LEVELS[e.level][0]
        ]
        assert not wrong, f"层级与分组不自洽：{wrong}"


# ============================================================
# 生成物：幂等、已同步、自解释
# ============================================================


class TestGeneratedArtifact:
    def test_regeneration_is_byte_identical(self, seed_entries: list[dict]) -> None:
        """**"生成而非手写"要成立，就得先证明生成是可复现的。**

        否则这套做法只是把人手的不确定性挪到了机器上：同一份 seed
        跑两次得到两份不同的 JSON，diff 里全是噪音，评审者无从判断
        那次改动到底是语义变了还是取色顺序变了。
        """
        assert render(generate(seed_entries)) == render(generate(seed_entries))

    def test_committed_json_matches_the_seed(self) -> None:
        """提交入库的 `experts.json` 必须与 seed 一致。

        它抓两种漂移：改了 YAML 忘了重新生成，以及有人手工改了 JSON。
        后者会在下一次生成时被静默覆盖，所以必须在这里红一次。
        """
        assert _EXPERTS_PATH.exists(), "experts.json 不存在，请运行 python -m scripts.gen_experts"
        assert build() == json.loads(_EXPERTS_PATH.read_text(encoding="utf-8"))

    def test_json_carries_its_own_count_and_distribution(self) -> None:
        """产物是**自解释**的：不跑 Python 也能数出 48 个人和分布。

        这是把表现层字段落进 JSON 换来的好处之一——评审者读文件就够了。
        """
        payload = build()
        assert payload["count"] == 48
        assert payload["distribution"] == {
            "L3·决策": 3,
            "L2·战略": 9,
            "L1·职能": 12,
            "L1·行业": 24,
        }

    def test_generated_fields_are_present_on_everyone(self) -> None:
        """生成字段一个都不能空。空字符串在界面上是"没有颜色"，
        而那和"这一层就是这个颜色"长得一模一样。"""
        incomplete = [
            expert.expert_id
            for expert in load_experts()
            if not (expert.avatar_color and expert.badge_color and expert.domain_icon)
        ]
        assert not incomplete

    def test_avatar_colour_does_not_depend_on_roster_order(self, seed_entries: list[dict]) -> None:
        """颜色只由 id 决定，**与在名册里的位置无关**。

        这一条是拿"把 seed 倒过来生成"来测的。如果取色按名册顺序发，
        那么**新增一个人就会让后面所有人的颜色集体右移**——历史报告里
        "那个蓝色的人"换人了，而我们没有任何地方会记录那次颜色变化。

        10 色配 48 人，撞色是必然的、也是可接受的：颜色在这里是装饰，
        层级由徽章底色承载。所以这里断言的是**顺序无关**，不是"互不相同"。
        """
        forward = {row["id"]: row["avatarColor"] for row in generate(seed_entries)["experts"]}
        backward = {
            row["id"]: row["avatarColor"] for row in generate(list(reversed(seed_entries)))["experts"]
        }
        assert forward == backward
        assert len(set(forward.values())) > 1, "所有头像同一个颜色，取色逻辑坏了"

    def test_avatar_colour_is_a_pure_function_of_the_id(self) -> None:
        attr = {expert.expert_id: expert.avatar_color for expert in load_experts()}
        assert all(attr[eid] == avatar_color(eid) for eid in known_ids())

    def test_seed_stats_say_they_are_not_measured(self) -> None:
        """初始统计是 0，并且**明说自己是 seed**。

        0 在这里不是占位符，是"还没有任何任务跑过"这个正确事实。
        `source` 让"这个数是被量出来的还是被生成的"成为可判断的——
        参考实现把未标注的假数据填进面板，没人分得清哪一栏是真的。
        """
        for expert in load_experts():
            assert expert.stats["source"] == "seed"
            assert expert.stats["tasks"] == 0


# ============================================================
# 加载器
# ============================================================


class TestLoader:
    def test_loads_all_48_in_layer_order(self) -> None:
        """顺序是决策 → 战略 → 执行，与名册的叙事顺序一致。
        按 id 字典序排会把 36 个执行层放在最前面。"""
        levels = [expert.level for expert in load_experts()]
        assert levels == ["L3"] * 3 + ["L2"] * 9 + ["L1"] * 36

    def test_experts_by_level_counts_match_the_distribution(self) -> None:
        assert len(experts_by_level("L3")) == 3
        assert len(experts_by_level("L2")) == 9
        assert len(experts_by_level("L1")) == 36
        assert experts_by_level("L9") == []

    def test_known_ids_is_exactly_the_roster(self) -> None:
        assert len(known_ids()) == roster_size() == 48
        assert known_ids() == {expert.expert_id for expert in load_experts()}

    def test_expert_by_id_hits_and_misses(self) -> None:
        assert expert_by_id("L3-001").name == "沈衡"
        assert expert_by_id("L3-001").role_title == "调研总监"
        # 名册边界外的号：L2 到 009 为止、L1 到 036 为止。
        # 这两个曾经是**真实存在过的幻觉 id**——15 人名册时代模型就编出过 L2-007。
        assert expert_by_id("L2-010") is None
        assert expert_by_id("L1-037") is None
        assert expert_by_id("") is None
        assert expert_by_id("l3-001") is None, "id 大小写敏感，不做模糊匹配"

    def test_legacy_ids_keep_their_identity(self) -> None:
        """阶段 2 那 15 个人的 id **一个都没变**。

        这不是怀旧：id 已经被写进 `expert_stats` 表和历史报告，
        改名换号就是一次数据迁移。名册扩容是**超集**，不是替换。
        """
        legacy = {
            "L3-001": ("沈衡", "调研总监"),
            "L3-002": ("陆远", "首席分析官"),
            "L3-003": ("纪岚", "质检总监"),
            "L2-001": ("方叙", "竞争战略分析师"),
            "L2-002": ("沈律", "定价与商业模式分析师"),
            "L2-003": ("温以宁", "用户研究分析师"),
            "L2-004": ("贺明", "技术架构分析师"),
            "L1-001": ("池越", "AI 编程工具行业专家"),
            "L1-002": ("苏澜", "知识管理行业专家"),
            "L1-003": ("郑野", "新能源汽车行业专家"),
            "L1-004": ("林澈", "跨境电商行业专家"),
            "L1-025": ("白鹭", "舆情分析师"),
            "L1-026": ("程野", "数据采集工程师"),
            "L1-027": ("闻笛", "财报与公开信息解读"),
            "L1-028": ("裴青", "渠道与价格监测"),
        }
        for expert_id, (name, role_title) in legacy.items():
            expert = expert_by_id(expert_id)
            assert expert is not None, f"{expert_id} 在扩容中丢了"
            assert (expert.name, expert.role_title) == (name, role_title)

    def test_default_team_ids_all_exist(self) -> None:
        """兜底队伍里的人必须还在名册里。

        它是一份**硬编码的 id 列表**，名册改动时最容易漏掉它——
        而它恰恰只在"调度已经失败"的时候才被用到，所以坏了也不会有人发现。
        """
        for role, ids in DEFAULT_TEAM.items():
            for expert_id in ids:
                assert expert_by_id(expert_id) is not None, f"默认队伍 {role} 里的 {expert_id} 不在名册"

    def test_default_team_levels_match_their_roles(self) -> None:
        """兜底队伍本身也要符合三层结构，否则"回退"会把一个不合规的队伍
        写进报告——比不回退更糟，因为它看起来是经过校验的。"""
        assert [expert_by_id(i).level for i in DEFAULT_TEAM["lead"]] == ["L3"]
        assert {expert_by_id(i).level for i in DEFAULT_TEAM["strategists"]} == {"L2"}
        assert {expert_by_id(i).level for i in DEFAULT_TEAM["executors"]} == {"L1"}

    def test_to_dict_keys_match_the_json_keys(self) -> None:
        """接口形状与落盘形状**逐键相同**。

        它俩是从同一个源读的，但一个是 `to_dict()` 手写的、一个是 JSON 的键。
        漏掉一个键的后果是：接口不返回它，前端静默拿不到，
        而刷新页面走另一条路时它又出现了。
        """
        payload_keys = set(json.loads(_EXPERTS_PATH.read_text(encoding="utf-8"))["experts"][0])
        dict_keys = set(load_experts()[0].to_dict())
        # JSON 用 `id`，接口用 `expertId`；其余键名一一对应。
        assert dict_keys - {"expertId"} == payload_keys - {"id"}


# ============================================================
# 调度摘要
# ============================================================


class TestRosterDigest:
    def test_all_48_ids_appear_and_nothing_is_truncated(self) -> None:
        """**这是本文件最重要的一条断言。**

        摘要的唯一使命是告诉调度模型合法 id 空间有多大。名册只有 15 人时
        模型编出了 11 个不存在的 id——不是它不听话，是我们没把能选的告诉它。
        所以"48 个 id 全在里面"就是这件事修好了的证据；
        一旦有人加人加过了头、把执行层挤出上下文，这条会先红。
        """
        digest = roster_digest()
        missing = sorted(expert_id for expert_id in known_ids() if expert_id not in digest)
        assert not missing, f"这些 id 没进调度摘要，模型会把它们当成非法：{missing}"
        assert "截断" not in digest

    def test_digest_fits_the_budget(self) -> None:
        assert len(roster_digest()) <= DIGEST_BUDGET

    def test_digest_names_every_layer_with_its_headcount(self) -> None:
        """摘要要说明每层有多少人，模型才知道"可选空间"有多大。"""
        digest = roster_digest()
        for level, count in (("L3", 3), ("L2", 9), ("L1", 36)):
            assert f"共 {count} 人" in digest
            assert LEVELS[level][0] in digest

    def test_upper_layers_get_their_one_liner_executors_do_not(self) -> None:
        """两档式：**简介是可选部分，人不是。**

        决策层与战略层的价值在立场上，那必须写出来；执行层的岗位名
        （"舆情分析师"）本身已经说明了专长，再加一句简介只是花掉预算。
        """
        digest = roster_digest()
        line_of = {line.split()[1]: line for line in digest.splitlines() if line.startswith("- ")}

        assert "定调研边界" in line_of["L3-001"]
        assert "拆解竞品的战略选择" in line_of["L2-001"]
        assert "熟悉 AI 编程赛道" not in line_of["L1-001"]
        assert line_of["L1-001"].rstrip().endswith("AI 编程工具行业专家")

    def test_truncation_falls_back_to_ids_only_and_says_so(self) -> None:
        """预算被压到不可能时，退到"只有 id 和岗位"那一档，
        并把截断**明写出来**——一份看起来完整、实际少了几个人的名册，
        比一份标着"已截断"的更危险。
        """
        digest = roster_digest(max_chars=200)
        assert "截断" in digest
        assert len(digest) <= 200


# ============================================================
# 闸门本身：逐条把 seed 改坏
# ============================================================


class TestValidateSeedRejects:
    """**只测"好的能过"是不够的**——一个永远不报错的校验函数也能做到。

    下面每条用例把 seed 弄坏一处，确认闸门会响。它们保护的是闸门，
    所以将来有人为了"少报点错"而放宽某一条时，会在这里红。
    """

    def test_accepts_the_real_seed(self, seed_entries: list[dict]) -> None:
        validate_seed(seed_entries)

    def test_rejects_a_wrong_headcount(self, seed_entries: list[dict]) -> None:
        with pytest.raises(SeedError, match="人数是 47"):
            validate_seed(seed_entries[:-1])

    def test_rejects_a_wrong_distribution(self, seed_entries: list[dict]) -> None:
        """把行业最后一人改成职能：人数仍是 48，但 24/12 变成了 23/13。

        这条正是"只在总数上做校验"会漏掉的坏法。
        """
        entries = copy.deepcopy(seed_entries)
        entries[_index_of("L1-024")].update(
            {"group": "职能", "id": "L1-037", "role_title": "出海合规专员"}
        )
        with pytest.raises(SeedError, match="分布不对"):
            validate_seed(entries)

    def test_rejects_a_duplicate_id(self, mutated) -> None:
        with pytest.raises(SeedError, match="重复"):
            validate_seed(mutated(_index_of("L1-005"), id="L1-004"))

    def test_rejects_a_malformed_id(self, mutated) -> None:
        with pytest.raises(SeedError, match="形状不合规"):
            validate_seed(mutated(_index_of("L1-005"), id="X1-005"))

    def test_rejects_a_level_group_mismatch(self, mutated) -> None:
        """L2 被写进了"行业"组。两边各自的计数都还说得过去，只有合起来看才不对。"""
        with pytest.raises(SeedError, match="分组只能"):
            validate_seed(mutated(_index_of("L2-001"), group="行业"))

    def test_rejects_an_unknown_level(self, mutated) -> None:
        with pytest.raises(SeedError, match="不是 L1/L2/L3"):
            validate_seed(mutated(_index_of("L2-001"), level="L4"))

    def test_rejects_an_industry_id_in_the_function_band(self, mutated) -> None:
        with pytest.raises(SeedError, match="职能组从"):
            validate_seed(mutated(_index_of("L1-005"), group="职能"))

    def test_rejects_a_function_id_in_the_industry_band(self, mutated) -> None:
        with pytest.raises(SeedError, match="越界"):
            validate_seed(mutated(_index_of("L1-030"), group="行业"))

    def test_rejects_a_duplicate_name(self, mutated) -> None:
        with pytest.raises(SeedError, match="名字"):
            validate_seed(mutated(_index_of("L1-005"), name="林澈"))

    def test_rejects_an_overlong_one_liner(self, mutated) -> None:
        with pytest.raises(SeedError, match="one_liner"):
            validate_seed(mutated(_index_of("L1-005"), one_liner="字" * 41))

    def test_rejects_too_few_knowledge_tags(self, mutated) -> None:
        with pytest.raises(SeedError, match="knowledge_tags"):
            validate_seed(mutated(_index_of("L1-005"), knowledge_tags=["只有一个"]))

    def test_rejects_too_few_skills(self, mutated) -> None:
        with pytest.raises(SeedError, match="skills"):
            validate_seed(mutated(_index_of("L1-005"), skills=["只有一个"]))

    def test_rejects_a_blank_required_field(self, mutated) -> None:
        with pytest.raises(SeedError, match="name 为空"):
            validate_seed(mutated(_index_of("L1-005"), name="   "))

    def test_rejects_a_blank_entry_inside_a_list(self, mutated) -> None:
        with pytest.raises(SeedError, match="空条目"):
            validate_seed(mutated(_index_of("L1-005"), knowledge_tags=["a", "b", " "]))

    def test_reports_every_problem_at_once(self, mutated) -> None:
        """一次报完，不是遇到第一个就停。

        48 人的名册若一次只报一条，改一轮跑一轮要跑十几轮。
        这条断言的是"报了好几条"，不是"报了第一条"。
        """
        entries = mutated(
            _index_of("L1-005"),
            name="   ",
            one_liner="字" * 41,
            knowledge_tags=["只有一个"],
        )
        with pytest.raises(SeedError) as excinfo:
            validate_seed(entries)
        assert str(excinfo.value).count("\n  - ") >= 3


class TestSeedFileShape:
    def test_seed_declares_its_version(self) -> None:
        import yaml

        raw = yaml.safe_load(SEED_PATH.read_text(encoding="utf-8"))
        assert raw["version"] == 1
        assert isinstance(raw["experts"], list)

    def test_a_malformed_seed_file_is_rejected(self, tmp_path) -> None:
        """顶层结构不对时要报错，而不是在别处炸出一个难懂的 KeyError。"""
        bad = tmp_path / "bad.yaml"
        bad.write_text("experts: 这不是列表\n", encoding="utf-8")
        with pytest.raises(SeedError, match="结构不对"):
            load_seed(bad)
