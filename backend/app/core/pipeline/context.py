"""流水线上下文：一次运行的全部状态与出口。

为什么是一个可变的大对象而不是"每阶段返回新状态"
------------------------------------------------
纯函数式的状态传递在这个场景下会付出很高代价：阶段之间有 20 多个字段
在流转，每加一个字段就要改所有阶段的签名。而这里不存在并发读写
同一个 ctx 的情况——扇出的是 provider 调用，它们各自读、不写 ctx。

所以选可变对象，但**给每个字段标注它由哪个阶段写入**。这不是装饰性的注释：
它让"谁该负责填这个字段"成为代码里可见的事实，一个字段没人填时
能立刻定位到是哪个阶段漏了。

事件出口统一在 `emit*` 方法里
-----------------------------
阶段代码不应该直接摸 `journal.publish`。统一出口换来两件事：
每个事件自动带上阶段名与时间戳，以及"某类事件忘记带某个字段"
这种错误只会发生一次。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.evidence.fetch import FetchOutcome
from app.core.models import Claim, Envelope, Evidence, Issue
from app.core.modes import ModeConfig
from app.core.observability.events import EventJournal
from app.core.observability.trace import Tracer
from app.core.pipeline.stages import progress_at, stage_label
from app.core.schemas.base import CoercionReport
from app.core.schemas.feature_tree import FeatureTree
from app.core.schemas.persona import PersonaSet
from app.core.schemas.pricing import PricingModel
from app.core.schemas.report import ReportSection
from app.data import Expert, expert_by_id


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class PipelineContext:
    """一次调研运行的全部状态。"""

    # ---- 构造时固定 ----
    task_id: str
    query: str
    mode: ModeConfig
    tracer: Tracer
    journal: EventJournal
    llm: Any
    search: Any
    fetcher: Any
    clarify_answers: dict[str, Any] = field(default_factory=dict)
    #: 澄清信息缺失时，是否自动采用推荐项继续。
    #: CLI 与压测为 True（一条命令跑完），API 为 False（停下来等用户回答）。
    auto_clarify: bool = True

    # ---- intake 写入 ----
    subject: str = ""
    domain: str = ""
    category: str = ""
    brands: list[str] = field(default_factory=list)
    candidate_brands: list[str] = field(default_factory=list)
    need_clarify: bool = False
    clarify_questions: list[dict] = field(default_factory=list)
    scope_rationale: str = ""

    # ---- orchestrator 写入 ----
    team: dict[str, list[str]] = field(default_factory=dict)
    dimensions: list[str] = field(default_factory=list)
    search_angles: list[str] = field(default_factory=list)
    plan_rationale: str = ""

    # ---- collect 写入 ----
    evidences: list[Evidence] = field(default_factory=list)
    evidence_index: dict[str, Evidence] = field(default_factory=dict)
    search_calls_made: int = 0
    #: 返工轮花掉的检索次数，**单独计**。
    #:
    #: 为什么要和上面那个分开：首轮的查询计划是按剩余预算截断的
    #: （`planned[:budget]`），所以首轮跑完时 `search_calls_made` 通常
    #: **正好等于** `max_search_calls`。如果返工也从同一个池子里取，
    #: 它拿到的永远是 0——返工一轮都搜不出去，而失败会伪装成
    #: "补采没有新增证据"，看起来像搜索源的锅。返工有自己的池，
    #: 才谈得上"返工提升 Δ"这个指标。
    rework_search_calls_made: int = 0
    raw_hits: int = 0
    filtered_hits: int = 0
    fetch_outcome: FetchOutcome = field(default_factory=FetchOutcome)
    #: 首条证据出现在任务开始后多少毫秒。这是"多久能开始看到东西"的指标，
    #: 比"总耗时"更贴近人的体感。
    first_evidence_ms: int | None = None

    # ---- analyze 写入 ----
    claims: list[Claim] = field(default_factory=list)
    matrix: dict = field(default_factory=dict)
    market_share: list[dict] = field(default_factory=list)
    five_forces: list[dict] = field(default_factory=list)
    trends: list[dict] = field(default_factory=list)
    feature_trees: list[FeatureTree] = field(default_factory=list)
    pricing_models: list[PricingModel] = field(default_factory=list)
    persona_sets: list[PersonaSet] = field(default_factory=list)
    sentiment: dict = field(default_factory=dict)
    charts: list[dict] = field(default_factory=list)
    citation_report: Any = None

    # ---- audit / rework 写入 ----
    issues: list[Issue] = field(default_factory=list)
    rework_rounds: int = 0
    rework_log: list[dict] = field(default_factory=list)
    #: 返工前的指标快照。`metrics_after - metrics_before` 就是铁律三的
    #: 可量化证据——没有它，"返工有用"只是一句信念。
    metrics_before: dict = field(default_factory=dict)

    # ---- write 写入 ----
    sections: list[ReportSection] = field(default_factory=list)
    #: 装配出来的报告记录。`Any` 是为了不让 context 依赖 db 层——
    #: 流水线应该能在不落库的情况下跑完（导出、评测就用这条路）。
    report: Any = None
    #: 出库合规校验的问题列表
    problems: list[str] = field(default_factory=list)
    #: 本次运行的致命错误。非空表示流水线没跑完。
    error: str = ""

    # ---- 贯穿全程 ----
    coercion: CoercionReport = field(default_factory=CoercionReport)
    messages: list[Envelope] = field(default_factory=list)
    thoughts: list[dict] = field(default_factory=list)
    degraded_blocks: list[str] = field(default_factory=list)
    started_monotonic: float = field(default_factory=time.monotonic)
    current_stage: str = "intake"

    # ============================================================
    # 事件出口
    # ============================================================

    def emit(self, type_: str, data: dict | None = None) -> None:
        self.journal.publish(type_, data)

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_monotonic) * 1000)

    def _node_payload(
        self,
        key: str,
        status: str,
        *,
        progress: float,
        elapsed_ms: int | None = None,
        detail: dict | None = None,
    ) -> dict:
        """`node_update` 的载荷。两个阶段出口共用，免得字段只在其中一处加。

        `detail` 是一个**有界的袋子**而不是摊平到顶层：阶段各自的产出摘要
        （章数、论点数、问题数、错误串）天差地别，摊平意味着任何阶段随便加一个
        键都进入了对外契约。`error` 尤其危险——摊平后它看上去像是这个事件
        自己的错误，而它其实是"某个阶段失败了"。
        """
        payload: dict = {
            "stage": key,
            "label": stage_label(key),
            "status": status,
            "progress": progress,
            # 返工轮次。DAG 上要显示"采集（第 2 轮）"，而轮次只有 ctx 知道。
            "round": self.rework_rounds,
        }
        if elapsed_ms is not None:
            payload["elapsedMs"] = elapsed_ms
        if detail:
            payload["detail"] = dict(detail)
        return payload

    def begin_stage(self, key: str) -> None:
        self.current_stage = key
        self.emit(
            "node_update",
            self._node_payload(key, "running", progress=progress_at(key)),
        )

    def finish_stage(self, key: str, *, status: str = "done", detail: dict | None = None) -> None:
        self.emit(
            "node_update",
            self._node_payload(
                key,
                status,
                progress=progress_at(key, substep=1.0),
                elapsed_ms=self.elapsed_ms(),
                detail=detail,
            ),
        )

    def report_progress(self, stage: str, substep: float, message: str = "") -> None:
        """阶段内进度 + 实时成本。

        token 与成本**随每条进度一起推**，而不是让前端自己按事件里带的用量求和：
        增量求和会被任何一次丢事件永久带偏，而且偏小——看起来只是"这个系统很省钱"。
        累计值直接从 tracer 读，它是所有 LLM 调用唯一的收口，不存在漏记。
        """
        usage = self.tracer.metrics()
        self.emit(
            "progress",
            {
                "stage": stage,
                "progress": progress_at(stage, substep=substep),
                "message": message,
                "tokens": usage["totalTokens"],
                "costUsd": usage["totalCostUsd"],
            },
        )

    def thought(self, expert_id: str, text: str, *, stage: str = "") -> dict:
        """发一条思维流。

        带 `expertId` / `level` / `stage` 而不只是文本：工作台的思维流
        要按专家分组显示、按层级配色、跟 DAG 节点联动，这些都需要
        结构化字段。参考实现只发文本，前端只能靠正则去猜，猜错了就静默显示错。
        """
        expert: Expert | None = expert_by_id(expert_id)
        payload = {
            "id": f"TH-{len(self.thoughts) + 1:04d}",
            "expertId": expert_id,
            "expertName": expert.name if expert else expert_id,
            "roleTitle": expert.role_title if expert else "",
            "level": expert.level if expert else "",
            "stage": stage or self.current_stage,
            "text": text,
            "at": _now_iso(),
        }
        self.thoughts.append(payload)
        # 嵌在 `thought` 下面而不是摊平：这一份**就是**报告 `thoughts[]` 里的
        # 那一份（测试断言过两者逐条相同），而报告里它叫 `Thought`。
        # 摊平会让同一个对象出现两份形状描述，多改一处字段就漂一次。
        self.emit("thought", {"thought": payload})
        return payload

    def send(self, sender: str, recipient: str, payload: dict, *, kind: str = "result") -> None:
        """记录并推送一条专家间消息。

        推的是**投影**不是完整信封：信封的 `payload` 可能是一整包证据，
        而订阅者（工作台的消息流）只需要知道谁交给了谁、大概是什么。
        完整的那一份进 `ctx.messages` 并随报告落库。
        """
        envelope = Envelope(
            sender=sender,
            recipient=recipient,
            kind=kind,  # type: ignore[arg-type]
            payload=payload,
            trace_id=self.task_id,
            created_at=_now_iso(),
        )
        self.messages.append(envelope)
        issues = payload.get("issues")
        self.emit(
            "message",
            {
                "message": {
                    "from": sender,
                    "to": recipient,
                    "kind": kind,
                    "stage": self.current_stage,
                    "round": self.rework_rounds,
                    "summary": str(payload.get("summary", ""))[:200],
                    "issueCount": len(issues) if isinstance(issues, list) else 0,
                }
            },
        )

    def emit_evidence(self, ev: Evidence) -> None:
        if self.first_evidence_ms is None:
            self.first_evidence_ms = self.elapsed_ms()
        self.emit("evidence", {"evidence": ev.to_dict()})

    def emit_chart(self, spec: dict) -> None:
        """推一张图表规格。

        图表随流推而不是等报告落库后一次性拉：写作阶段要跑十几章，
        读者在这期间只能看着 DAG，而矩阵与定价的图**在分析阶段就已经成型**了。
        """
        self.emit("chart", {"chart": spec})

    def degrade(self, block: str, reason: str) -> None:
        """登记一处降级。

        **降级必须留痕。** 静默降级的报告看起来和完整报告一样自信，
        而读者无从知道某一块其实没做成——这比明确报错更危险。

        同一句话只登记一次。返工会重跑采集，于是"12/40 条正文抓取失败"
        这种一模一样的句子会被追加三次——库里那份真报告就是这样
        （见问题 54）。重复的降级说明**让这一块变长，却没有多说出任何事**，
        而"降级多"本身是读者判断这份报告可信度的依据：三条相同的话
        读起来像三处不同的失败。
        """
        line = f"{block}：{reason}"
        if line not in self.degraded_blocks:
            self.degraded_blocks.append(line)

    # ============================================================
    # 便捷属性
    # ============================================================

    @property
    def known_evidence_ids(self) -> set[str]:
        return set(self.evidence_index)

    @property
    def lead_expert(self) -> str:
        leads = self.team.get("lead") or ["L3-001"]
        return leads[0]

    @property
    def senior_expert(self) -> str:
        strategists = self.team.get("strategists") or ["L2-001"]
        return strategists[0]

    @property
    def collector_expert(self) -> str:
        executors = self.team.get("executors") or ["L1-026"]
        return executors[0]

    @property
    def reviewer_expert(self) -> str:
        return "L3-003"

    def evidence_by_dimension(self) -> dict[str, list[Evidence]]:
        """按维度归组证据。

        一条证据命中多个维度时会在多个组里各出现一次。这是刻意的：
        维度覆盖率的分子是"有证据的维度数"，如果一条同时证明两个
        维度的证据只算一次，覆盖率会被无谓地拉低。
        """
        buckets: dict[str, list[Evidence]] = {dimension: [] for dimension in self.dimensions}
        for ev in self.evidences:
            for dimension in ev.matched_dimensions or []:
                buckets.setdefault(dimension, []).append(ev)
        return buckets

    def reindex(self) -> None:
        self.evidence_index = {ev.evidence_id: ev for ev in self.evidences}
