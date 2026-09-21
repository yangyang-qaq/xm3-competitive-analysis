"""录制脚本自己的逻辑。

record → replay 的往返在 `tests/integration/test_cassette_offline.py` 里已经测了
（用 mock provider 走真实的那套录制/回放代码），所以这里不重复。

这里测的是**脚本这层**：黄金集读得对不对、估算算得对不对、会不会在该拒绝的时候
拒绝。这些东西的错误都不会当场报错，只会让一次几十块钱的录制跑出不该有的结果：

- 黄金集里写错一个 mode 名 → `get_mode()` 会**静默退回 deep 档**，于是每条
  都按 deep 跑，成本翻倍而没有任何提示。
- 重名的条目 → `--only` 选中两条，报告与 MANIFEST 的归属对不上。
- 估算把 deep 算得比 quick 便宜 → 确认那一步就失去意义了。
- 在 mock 上录制 → 录出一份"mock 说过什么"，回放永远绿，且什么都没证明。

最后一条是这组测试里最重要的：它是唯一一个**防住"看起来成功的假录制"**的守卫。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.modes import MODE_CONFIG
from scripts.record_cassettes import (
    DEFAULT_GOLDEN,
    GoldenQuery,
    estimate,
    load_golden,
)

GOLDEN = Path(DEFAULT_GOLDEN)


class _FakeLLM:
    """只实现估算用到的那两个方法。"""

    def __init__(self, prices: dict[str, float] | None = None) -> None:
        self._prices = prices or {}

    def resolve_model(self, tier: str) -> str:
        return f"m-{tier}"

    def pricing(self):
        from app.providers.base import ModelPricing

        return {
            f"m-{tier}": ModelPricing(
                input_per_mtok_usd=1.0, output_per_mtok_usd=price
            )
            for tier, price in self._prices.items()
        }


def _query(mode: str = "quick", name: str = "x") -> GoldenQuery:
    return GoldenQuery(name=name, query="对比 A 与 B", mode=mode)


# ============================================================
# 黄金集本身
# ============================================================


class TestGoldenSet:
    """这一组是对**仓库里那份真实文件**的断言，不是对临时构造的数据。"""

    def test_it_parses_and_has_no_duplicate_names(self) -> None:
        items = load_golden(GOLDEN)

        assert items, "黄金集是空的"
        names = [i.name for i in items]
        assert len(names) == len(set(names))

    def test_every_mode_is_a_real_mode_key(self) -> None:
        """写错 mode 名不会报错，只会退回默认档。

        `get_mode()` 对未知 key 故意退回 `DEFAULT_MODE`（档位名是用户输入，
        拼错一个不该让任务失败）。但在黄金集里这个宽容是**有害的**：
        把 `expert` 写成 `expet` 会让它按 deep 跑，成本差一个档，
        而整个过程一声不响。
        """
        unknown = [i.name for i in load_golden(GOLDEN) if i.mode not in MODE_CONFIG]

        assert unknown == []

    def test_all_three_modes_are_represented(self) -> None:
        """三档模式都得有用例。

        某一档没有 cassette，就等于那一档没有任何回归保护——而"没被回放过"
        和"回放通过"在报告里长得一模一样。
        """
        used = {i.mode for i in load_golden(GOLDEN)}

        assert used == set(MODE_CONFIG)

    def test_every_entry_says_what_it_tests(self) -> None:
        """`note` 是这条用例存在的理由。

        一条不测任何东西的 query 只会稀释指标：它跑绿了不能说明任何事，
        跑红了也不知道该去看哪一块。
        """
        missing = [i.name for i in load_golden(GOLDEN) if not i.note.strip()]

        assert missing == []

    def test_the_ambiguity_case_is_flagged(self) -> None:
        """歧义用例要走澄清分支，脚本靠这个标记把它和普通对比区分开。"""
        items = load_golden(GOLDEN)

        assert [i.name for i in items if i.ambiguous] == ["vague-coffee-market"]

    def test_a_missing_file_fails_loudly(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="找不到黄金集"):
            load_golden(tmp_path / "nope.yaml")

    def test_duplicate_names_are_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "golden.yaml"
        path.write_text(
            "queries:\n"
            "  - {name: a, query: '对比 A 与 B', mode: quick}\n"
            "  - {name: a, query: '对比 C 与 D', mode: deep}\n",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit, match="重名"):
            load_golden(path)

    def test_queries_and_ambiguity_are_merged(self, tmp_path: Path) -> None:
        path = tmp_path / "golden.yaml"
        path.write_text(
            "queries:\n"
            "  - {name: a, query: '对比 A 与 B', mode: quick}\n"
            "ambiguity:\n"
            "  - {name: b, query: 看看市场, mode: quick}\n",
            encoding="utf-8",
        )

        items = load_golden(path)

        assert [i.name for i in items] == ["a", "b"]
        assert [i.ambiguous for i in items] == [False, True]


# ============================================================
# 估算
# ============================================================


class TestEstimate:
    def test_quick_matches_the_measured_baseline(self) -> None:
        """基准档的估算必须**恰好等于**实测值。

        差一点就说明折算公式被改动了——而基准档的估算是唯一一个有实测值的那个，
        它偏了，其它档的估算就都没有锚点了。
        """
        result = estimate(_query("quick"), _FakeLLM())

        assert result["llmCalls"] == 13
        assert result["costUsd"] == 0.0184  # 0.01836 四舍五入到 4 位

    def test_a_bigger_mode_estimates_more(self) -> None:
        fake = _FakeLLM()
        quick = estimate(_query("quick"), fake)
        deep = estimate(_query("deep"), fake)
        expert = estimate(_query("expert"), fake)

        assert quick["llmCalls"] < deep["llmCalls"] < expert["llmCalls"]
        assert quick["costUsd"] < deep["costUsd"] < expert["costUsd"]

    def test_search_and_fetch_are_reported_as_ceilings_not_estimates(self) -> None:
        """这两个数是档位里的**硬上限**，不是估算——所以它们必须直接来自档位。

        混进"估算"里会让人以为它们是预期值：实际搜索次数通常远低于上限，
        而抓取受预算截断，两者都不该按上限去做预算。
        """
        for key in MODE_CONFIG:
            result = estimate(_query(key), _FakeLLM())
            mode = MODE_CONFIG[key]

            assert result["searchCallsMax"] == mode.max_search_calls
            assert result["fetchesMax"] == mode.max_fetches

    def test_a_pricier_tier_raises_the_estimate(self) -> None:
        """写作档位贵 10 倍，估算要跟着涨 10 倍。

        deep 与 quick 的章节数相同（deep 用的是 `_DEFAULT_SECTIONS` 那一份），
        所以这一条只在档位价格上不同——正好把价格折算单独隔离出来测。
        """
        cheap = estimate(_query("deep"), _FakeLLM({"fast": 1.0, "aux": 1.0}))
        pricey = estimate(_query("deep"), _FakeLLM({"fast": 1.0, "aux": 10.0}))

        assert pricey["costUsd"] == pytest.approx(cheap["costUsd"] * 10, rel=0.02)
        assert pricey["tierNote"] == ""

    def test_unpriced_tiers_say_so_instead_of_guessing(self) -> None:
        """取不到定价时返回 1.0，并**说明**没折算。

        静默按 1.0 算的话，一个用着贵模型的档位会和便宜档估出一样的钱，
        而确认那一步看到的数字就失去了意义。
        """
        result = estimate(_query("deep"), _FakeLLM({"fast": 1.0}))  # 缺 aux

        assert result["tierNote"]
        assert "未折算" in result["tierNote"]

    def test_a_broken_pricing_table_does_not_stop_the_recording(self) -> None:
        """定价拿不到是常事（比如只配了搜索的 key），不该让录制跑不起来。"""

        class _Exploding(_FakeLLM):
            def pricing(self):
                raise RuntimeError("定价表炸了")

        result = estimate(_query("quick"), _Exploding())

        assert result["costUsd"] > 0
        assert "取定价失败" in result["tierNote"]


# ============================================================
# 守卫
# ============================================================


class TestMockGuard:
    """最重要的一条：不让 mock 被录进 cassette。"""

    def test_recording_with_mock_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.record_cassettes import _guard_against_mock

        for key in ("LLM_PROVIDER", "SEARCH_PROVIDER", "FETCH_PROVIDER"):
            monkeypatch.setenv(key, "mock")

        with pytest.raises(SystemExit, match="拒绝录制"):
            _guard_against_mock()

    def test_a_single_mocked_kind_is_enough_to_refuse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """只要有一类是 mock 就拒绝。

        混录更隐蔽：cassette 里大部分记录是真的、少数是编的，
        而回放时分不出来哪些是编的——那份录制就不能作为"真实响应结构"的证据了。
        """
        from scripts.record_cassettes import _guard_against_mock

        monkeypatch.setenv("LLM_PROVIDER", "deepseek")
        monkeypatch.setenv("SEARCH_PROVIDER", "mock")
        monkeypatch.setenv("FETCH_PROVIDER", "http")

        with pytest.raises(SystemExit, match="搜索"):
            _guard_against_mock()
