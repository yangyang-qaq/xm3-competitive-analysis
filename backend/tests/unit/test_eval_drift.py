"""漂移比对器本身的行为。

`diff_baseline` 是**唯一**告诉人"这次比上次差了"的地方。它写错的后果
和别的函数不一样：它错成"永远说没变差"，那么基线这个机制就整个不存在，
而输出看起来一切正常——这正是 `问题记录.md` 里 39 那条的同一类错误
（闸门绿着，而它早就没在测东西了）。

**绝对增量与相对变化并存**是这里最容易写错的一处（下面有几条就是在钉它），
原文见 `diff_baseline`：`floor = max(abs(was), 1.0)`，所以小量级的比率
（0.1 → 0.2）要按**绝对** 0.1 判，而不是相对 100% 判。
"""
from __future__ import annotations

from eval.metrics import NUMERIC_KEYS
from eval.run_eval import DRIFT_EXCLUDED, diff_baseline


def _agg(**values: float) -> dict[str, dict[str, float]]:
    """造一份最小聚合：min/median/max 三个统计量都填同一个值。

    只关心 `gate_direction` 那一侧，三个填一样最省事，
    也不会让测试在"到底比的是哪一侧"这件事上产生歧义。
    """
    return {key: {"min": v, "median": v, "max": v} for key, v in values.items()}


def _baseline(**values: float) -> dict:
    return {"aggregate": _agg(**values)}


# ---------------------------------------------------------------
# 该报的
# ---------------------------------------------------------------


def test_越小的指标涨了要报() -> None:
    """`hallucinationRate` 属于 `LOWER_IS_BETTER`，涨上去就是变差。

    取 0.10 → 0.30 而不是 0.10 → 0.20：`floor` 下限是 1.0，
    所以小量级指标要涨过 0.15 才越得过容忍线。
    """
    drift = diff_baseline(
        _agg(hallucinationRate=0.30), _baseline(hallucinationRate=0.10), tolerance=0.15
    )
    assert len(drift) == 1
    assert "hallucinationRate" in drift[0]


def test_越大的指标掉了要报() -> None:
    """对侧：`independentDomains` 是"越多越好"，掉下来才是变差。"""
    drift = diff_baseline(
        _agg(independentDomains=5), _baseline(independentDomains=20), tolerance=0.15
    )
    assert len(drift) == 1
    assert "independentDomains" in drift[0]


def test_报出来的行要带方向和幅度() -> None:
    """报一行"变差了"而不说差多少，等于让人自己去翻上一次的 JSON。

    这条把格式钉死，顺便验证了 `floor` 那个下限：0.1 → 0.9 差 0.8，
    除以 `max(0.1, 1.0) = 1.0` 得 +80.0%，**不是** 800%。
    """
    drift = diff_baseline(
        _agg(hallucinationRate=0.9), _baseline(hallucinationRate=0.1), tolerance=0.15
    )
    assert drift == ["hallucinationRate：max 0.1 → 0.9（+80.0%）"]


# ---------------------------------------------------------------
# 不该报的
# ---------------------------------------------------------------


def test_变好不报() -> None:
    """只会朝一个方向报警。**两个方向都报等于没有方向**——那时这行输出
    就只是"数字变了"，而人不会为"变了"去翻记录。"""
    assert diff_baseline(
        _agg(hallucinationRate=0.01), _baseline(hallucinationRate=0.10), tolerance=0.15
    ) == []
    assert diff_baseline(
        _agg(independentDomains=99), _baseline(independentDomains=20), tolerance=0.15
    ) == []


def test_容忍度以内不报() -> None:
    """100 → 110 是 +10%，在 15% 容忍内 → 不报。

    用 `independentDomains` 而不是某个比率：只有量级足够大的指标
    才真正走"相对变化"那条路，小量级指标会先被 `floor` 兜住，
    于是那条测试测的是兜底而不是容忍度。
    """
    assert diff_baseline(
        _agg(independentDomains=110), _baseline(independentDomains=100), tolerance=0.15
    ) == []


def test_计时项不参与漂移比对() -> None:
    """**这条是这组测试存在的理由。**

    实测过：同一条 `--replay` 连跑两遍，`durationMs` 266 → 405、
    `firstEvidenceMs` 94 → 125。两次跑的是同一份录制、同一台机器、
    同一份代码——变的是当时有没有别的进程抢 CPU。拿它做漂移检测，
    结果是每次运行都报两条黄字，把真正该看的那几条淹掉。

    构造的是"涨了一倍"这种**显然该报**的幅度：如果哪天排除被去掉，
    这条会立刻红，而不是等到 CI 里冒出两条谁也解释不清的警告。
    """
    drift = diff_baseline(
        _agg(durationMs=800, firstEvidenceMs=300),
        _baseline(durationMs=266, firstEvidenceMs=94),
        tolerance=0.15,
    )
    assert drift == []


def test_排除项必须是真实存在的指标名() -> None:
    """拼错的排除项会**静默失效**：名字对不上 → 那条指标照比不误，
    黄字照报，而写排除的那个人以为自己已经处理过了。

    这一条把 `DRIFT_EXCLUDED` 钉在 `NUMERIC_KEYS` 上——指标改名时
    这里会红，而不是让排除悄悄变成一句注释。
    """
    unknown = DRIFT_EXCLUDED - set(NUMERIC_KEYS)
    assert not unknown, f"DRIFT_EXCLUDED 里有不存在的指标名：{sorted(unknown)}"


def test_基线里没有的指标跳过而不炸() -> None:
    """基线是旧版本生成的，缺了新指标（或旧指标已被删）是常态。
    缺就跳过——为了一个不存在的键让整个评测崩掉，代价远大于收益。

    同时钉住"跳过"不等于"顺手把别的也跳过"：同一个 aggregate 里
    真正变差的那条必须照报。
    """
    drift = diff_baseline(
        {**_agg(hallucinationRate=0.9), "某个新指标": _agg(某个新指标=1.0)["某个新指标"]},
        _baseline(hallucinationRate=0.1),
        tolerance=0.15,
    )
    assert drift == ["hallucinationRate：max 0.1 → 0.9（+80.0%）"]
