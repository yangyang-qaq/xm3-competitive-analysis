"""从报告正文里读指标，再跨 query 聚合。

**这里一个指标都不重算。**
--------------------------
`backend/app/core/analysis/metrics.py` 的 `compute_metrics(ctx)` 已经算过一遍，
结果跟着报告落库在 `body["metrics"]`。评测层若再算一遍，就会有**两份
"维度覆盖率"的定义**，然后它们在某个边界上分叉——这在 xm3 里已经
发生过两次（见 `问题记录.md` 问题 37：`buildGraph` 与 `pickEvidences`
各自实现了一遍"哪条证据进图"）。所以这里的规矩是硬的：
**能读的就读，读不到就报缺，绝不退化成"自己算一个差不多的"。**

读不到就报缺
------------
早期报告（或在 `compute_metrics` 加字段之前落库的）可能没有某个键。
此时 `None` 会一路传下去，由 `run_eval` 的闸门判成"无法判定"并拒绝，
而不是当成 0。**把"没测到"当成"测出来是 0"是这个项目反复在防的那类错**
（专家名册的 `stats` 就是这个形状：那些 0 是"还没被量过"）。

聚合为什么给中位数与极值，却不给均值
------------------------------------
闸门要抓的是回归，而回归往往只出现在**一条** query 上：某次改动让
"几乎没有公开信息"那一类（黄金集里的 `vague-coffee-market`）覆盖率掉到 0.2，
其余 21 条纹丝不动。均值会被摊薄到看不出，中位数更是完全不动。
所以：

- 「至少要到 X」的指标看 **min**（最差的那条有没有跌破）
- 「至多花 Y」的指标看 **max**（最贵的那条有没有超支）
- 中位数只用来在文档里描述"典型情况"

这也是 `--repeat N` 存在的原因：判官指标有采样噪声，单次跑出来的极值
可能只是运气。确定性指标不需要 repeat，一次就是定论。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

#: 闸门会看的数值指标。其余键（`platforms` / `byPurpose` 之类）只随报告展示，
#: 不进聚合——它们不是标量，聚起来没有意义。
NUMERIC_KEYS: tuple[str, ...] = (
    # 铁律一：引用强制
    "claims",
    "verifiedClaims",
    "unsupportedClaimRate",
    "hallucinationRate",
    "phantomCitations",
    # 铁律二：交叉验证
    "crossValidatedClaims",
    "crossValidationRate",
    "independentDomains",
    # 证据与维度
    "evidences",
    "degradedEvidences",
    "degradedRate",
    "platformCount",
    "dimensionsPlanned",
    "dimensionsCovered",
    "dimensionCoverage",
    # 采集过程
    "searchCalls",
    "searchErrors",
    "rawHits",
    "filteredHits",
    "fetchedOk",
    "fetchedDegraded",
    # 成本与耗时
    "llmCalls",
    "llmOptionalFailures",
    "totalCostUsd",
    "totalTokens",
    "durationMs",
    "firstEvidenceMs",
    # 返工
    "reworkRounds",
    "issues",
)

#: 「越小越好」的指标。闸门对它们看 max，其余看 min。
#: 写在这里而不是每个阈值各写一遍方向：方向搞反的闸门**永远是绿的**，
#: 而它看起来和正常闸门一模一样。
#:
#: `llmCalls` 在里面，是为了给成本设一个上界。它不是"调用越少越好"，
#: 而是"闸门关心的是它有没有涨上去"。不写进来的话，`thresholds.yaml` 里
#: 那条 `max: 40` 会因为方向解析成 `min` 而找不到界——闸门会**报配置错误**
#: 而不是静默通过（第一版实测就是这样抓到的，见下面 `_pooled`）。
LOWER_IS_BETTER: frozenset[str] = frozenset(
    {
        "unsupportedClaimRate",
        "hallucinationRate",
        "phantomCitations",
        "degradedRate",
        "degradedEvidences",
        "searchErrors",
        "llmOptionalFailures",
        "totalCostUsd",
        "durationMs",
        "firstEvidenceMs",
        "llmCalls",
    }
)

#: 比率类指标 → `(分子键, 分母键, 分子要反过来算吗)`。
#: `invert=True` 表示分子是 `分母 − 分子键`（报告里存的是"已验证"，
#: 而这个比率要的是"没验证的"那份）。
#:
#: 这些指标**不能按 min/max 聚合**，这是第一版实测暴露出来的：
#: `crossValidationRate` 在黄金集上算出来 `min = 0`，闸门因此红了——
#: 但那个 0 来自一份只有 3 条论点的报告（3 条都没被交叉验证）。
#: 一份报告 3 条论点，这个比率只能取 0 / 0.33 / 0.67 / 1，
#: 取"22 份里的最小值"等于在问"有没有哪一份碰巧是 0"，
#: 而这是个噪声问题，不是质量问题。
#:
#: 比率的正确答案是**合并**：`Σ分子 / Σ分母`。它回答的是
#: "这批报告整体上有多大比例的论点被交叉验证了"，那才是这个数的意思。
#: min/max 仍然打印出来给人看（它们能暴露单条的异常），只是不参与判定。
RATE_PAIRS: dict[str, tuple[str, str, bool]] = {
    "crossValidationRate": ("crossValidatedClaims", "verifiedClaims", False),
    # 分母是**全部论点**，不是已验证的论点：`compute_metrics` 里是 `1 - verified/claims`。
    "unsupportedClaimRate": ("verifiedClaims", "claims", True),
    "degradedRate": ("degradedEvidences", "evidences", False),
    "dimensionCoverage": ("dimensionsCovered", "dimensionsPlanned", False),
    # `hallucinationRate` **故意不在这里**：它的分母是"模型输出过的引用编号数"，
    # 而那不是一个能被存下来的标量（`phantomCitations` 是绝对条数，不是分母）。
    # 硬凑一个分母去合并会比 min/max 更错，所以它保持 min/max。
}


class MissingMetrics(KeyError):
    """报告里压根没有 `metrics`。

    单独一个异常类型，是因为它和"某个键是 0"必须能被区分开：
    见到它说明这是一份不该进评测的报告（回放产物、或评测层加字段之前落的库），
    见到 0 说明这次真的跑出来是 0。
    """


@dataclass(frozen=True)
class ReportMetrics:
    """一份报告的指标快照，外加它是谁跑出来的。"""

    name: str
    mode: str
    values: dict[str, float]
    #: 报告正文里声明自己降级过的模块（`body["degraded"]`）。
    #: 闸门不看它，但 `run_eval` 会把它印出来——一份"指标漂亮但降级了三处"
    #: 的报告不该被当成好结果。
    degraded: tuple[str, ...] = ()
    #: `body["metrics"]["rework"]`，只有真触发过返工才有。
    rework: dict[str, Any] | None = None
    qualityGatePassed: bool | None = None

    def get(self, key: str) -> float | None:
        return self.values.get(key)


def read_report(name: str, body: dict[str, Any]) -> ReportMetrics:
    """从一份报告正文里取出指标。

    `name` 不由正文推导：报告正文里没有"我是黄金集里哪一条"这个字段，
    而按 `query` 反查会在两条 query 文本相同时出错。调用方知道自己跑的是谁。
    """
    raw = body.get("metrics")
    if not isinstance(raw, dict) or not raw:
        raise MissingMetrics(
            f"{name}：正文里没有 metrics。这份报告不是 run_pipeline 产出的，"
            "或者早于评测层加字段——它不该进评测。"
        )

    values: dict[str, float] = {}
    for key in NUMERIC_KEYS:
        value = raw.get(key)
        if isinstance(value, bool):  # bool 是 int 的子类，得先挡掉
            continue
        if isinstance(value, (int, float)):
            values[key] = float(value)

    rework = raw.get("rework")
    return ReportMetrics(
        name=name,
        mode=_mode_key(body.get("mode")),
        values=values,
        degraded=tuple(str(item) for item in (body.get("degraded") or ())),
        rework=rework if isinstance(rework, dict) else None,
        qualityGatePassed=(
            raw.get("qualityGatePassed")
            if isinstance(raw.get("qualityGatePassed"), bool)
            else None
        ),
    )


def _mode_key(raw: Any) -> str:
    """报告正文里的 `mode` 是**整个档位配置对象**（`{key, label, description}`），
    不是字符串。取 `key` 而不是 `str(...)`：后者会得到一整段字典字面量，
    在概况里打印成一行乱七八糟的东西，而它看起来"有值"，所以不会被发现。
    """
    if isinstance(raw, dict):
        return str(raw.get("key") or "")
    return str(raw or "")


def _summarize(series: list[float]) -> dict[str, float]:
    return {
        "min": round(min(series), 6),
        "median": round(statistics.median(series), 6),
        "max": round(max(series), 6),
    }


def _pooled(
    reports: list[ReportMetrics], numerator: str, denominator: str, *, invert: bool
) -> float | None:
    """合并成一个总比率：`Σ分子 / Σ分母`。

    分母合计为 0 时返回 `None`（不是 0）：`None` 会让闸门说"无法判定"，
    而 0 会让它说"通过了"——**"没测到"和"测出来是 0"必须区分开**，
    这是这个项目反复在防的那类错（专家名册的 `stats` 就是这个形状）。

    某条报告缺分子或分母时**整条跳过**，不拿 0 顶：拿 0 顶等于把那一条
    当成"什么都没发生"，会把这个比率往好看的方向拉。
    """
    total_num = 0.0
    total_den = 0.0
    for r in reports:
        den = r.values.get(denominator)
        num = r.values.get(numerator)
        if den is None or num is None:
            continue
        total_den += den
        total_num += (den - num) if invert else num
    if total_den <= 0:
        return None
    return round(total_num / total_den, 4)


def aggregate(reports: list[ReportMetrics]) -> dict[str, dict[str, float]]:
    """逐指标汇总成 `{指标: {min, median, max[, pooled]}}`。

    `min/median/max` 是**给人看的**：它们能暴露单条的异常（比如某一条
    覆盖率掉到 0.2）。判定用的是 `pooled`（比率类才有）或 `min`/`max`
    （计数类），理由见 `RATE_PAIRS`。

    某条报告缺某个键时按"缺"处理，并在 `reports` 里如实记下参与合并的条数——
    免得"22 条里只有 3 条算出了这个数"被当成"22 条都算出来了"。
    """
    out: dict[str, dict[str, float]] = {}
    for key in NUMERIC_KEYS:
        series = [r.values[key] for r in reports if key in r.values]
        if not series:
            continue
        summary = _summarize(series)
        summary["reports"] = float(len(series))
        pair = RATE_PAIRS.get(key)
        if pair:
            pooled = _pooled(reports, pair[0], pair[1], invert=pair[2])
            if pooled is not None:
                summary["pooled"] = pooled
        out[key] = summary
    return out


def gate_direction(key: str) -> str:
    """返回 `"max"` 或 `"min"`：这条阈值该拿哪个极值去比。"""
    return "max" if key in LOWER_IS_BETTER else "min"


def overall(reports: list[ReportMetrics]) -> dict[str, Any]:
    """整批跑的概况。闸门之外的东西都在这里。"""
    return {
        "reports": len(reports),
        "modes": sorted({r.mode for r in reports if r.mode}),
        "degraded": {
            r.name: list(r.degraded) for r in reports if r.degraded
        },
        "reworked": [r.name for r in reports if (r.rework or {}).get("improved")],
        "qualityGateFailed": [
            r.name for r in reports if r.qualityGatePassed is False
        ],
        "totalCostUsd": round(
            sum(r.values.get("totalCostUsd", 0.0) for r in reports), 6
        ),
    }
