"""CLI 事件渲染。

`render_event()` 是 `run_pipeline_cli.py` 唯一的"用户界面"，而这个 CLI
是"核心闭环"那一步的验收工具。它的 docstring 里原本写着一句

    "渲染逻辑单独成函数并被测试覆盖"

——**那是假的**，一条测试都没有。这个文件补上，并且顺带修掉了那句话
掩盖住的四个缺陷：

1. 六种**嵌套**载荷的事件（thought / message / evidence / chart / trace / image）
   被一律按"字段在顶层"读，于是全部渲染成空行：思维流印成 `· ：`、
   trace 印成 `⏱ 0ms`、消息印成 `→  ⇒ ：`。跑得通、不报错，
   只是整个叙事输出是空白的。
2. `node_update` 读 `data["node"]`——**这个键不存在**，契约里叫 `stage`。
   它没暴露是因为兜底读了 `label`，而 `label` 恰好是必填的。
   `tests/contract/test_sse_contract.py` 的 docstring 恰好把这个改名
   当成例子写过（它防的是前端读 `e.node`）——CLI 是第三个消费者，没人查。
3. `done` 读顶层的 `elapsedMs` / `reworkRounds`，而这两个键**已经在契约里
   被移进 `metrics` 了**。读不到就落回默认值，于是终端上永远印
   "耗时 0ms，返工 0 轮"——两个看起来像真实测量结果的 0。
4. `report_ready` 读不存在的 `quality`，读不到就当"未通过、完整度 0%"。
   于是一次**质量门通过**的报告在终端上印着"质量门=未通过"，
   而同一段输出里审计专家刚刚说过"质量门结论：通过"。

第 3、4 条有共同的性质：**缺字段时给了一个默认判断，而不是不说。**
报告缺一个键是常态（契约本来就允许），但"未通过"是一句有内容的话。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_pipeline_cli import NESTED_PAYLOADS, parse_sse_frame, render_event

#: `backend/tests/unit/` → 仓库根
CONTRACT_PATH = Path(__file__).resolve().parents[3] / "contracts" / "sse_events.json"


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["events"]


def _payload_of(name: str) -> dict:
    """契约里事件节点**本身就是载荷描述**，没有再套一层 `payload`。"""
    return _contract()[name]


def render(event_type: str, data: dict, *, verbose: bool = True) -> str:
    return render_event(event_type, data, verbose=verbose)


# ============================================================
# 与契约对齐
# ============================================================


class TestAgainstTheContract:
    def test_nested_payload_map_matches_the_contract(self) -> None:
        """`NESTED_PAYLOADS` 必须与契约的 `shape` / `required` 一致。

        这是**第三份**独立的声明（契约一份、契约测试里的 `CARRIED_KEY` 一份、
        这里一份），三份必须对上。刻意不把契约读进来当唯一真相源：
        读进来就失去了交叉验证——渲染器写错了会跟着一起错。
        """
        from_contract = {
            name: payload["required"][0]
            for name, payload in _contract().items()
            if payload.get("shape") == "nested"
        }
        flat = {
            name for name, payload in _contract().items() if payload.get("shape") == "flat"
        }

        assert from_contract, "契约里一个 nested 事件都没有，解析方式可能变了"
        assert from_contract == NESTED_PAYLOADS
        assert not (flat & set(NESTED_PAYLOADS)), "flat 事件不该出现在嵌套表里"

    def test_every_contract_event_type_is_rendered(self) -> None:
        """契约里声明的每种事件都要有一个分支。

        新增一种事件却忘了给渲染器加分支，表现是那一行**静默变成空白**——
        CLI 照常跑完，只是少了一种信息。这条让它变成一次测试失败。
        """
        # 每种事件给一份最小的、必填键齐全的载荷
        samples = {
            name: dict.fromkeys(_payload_of(name).get("required", []))
            for name in _contract()
        }
        # 嵌套类要装一个 dict，否则解不开；值给 None 是为了只验证"有分支"
        for name, key in NESTED_PAYLOADS.items():
            samples[name][key] = {}

        unrendered = [name for name, data in samples.items() if not render(name, data)]
        assert not unrendered, f"这些契约事件没有渲染分支：{unrendered}"

    def test_an_unknown_event_type_renders_nothing(self) -> None:
        """未知类型返回空串而不是抛错：事件流将来会加类型，
        而一个旧版本的 CLI 不该因此在半路崩掉。"""
        assert render("brand_new_thing", {"x": 1}) == ""


# ============================================================
# 嵌套载荷
# ============================================================


class TestNestedPayloads:
    def test_a_thought_renders_its_expert_and_text(self) -> None:
        """回归：这条从前印出来是 `      · ：`（两个空字段加一个冒号）。"""
        line = render(
            "thought",
            {"thought": {"expertName": "沈衡", "text": "先确认调研对象和范围。", "level": "L3"}},
        )

        assert "沈衡" in line
        assert "先确认调研对象和范围。" in line
        assert line.strip() != "· ："

    def test_a_trace_renders_its_span_fields(self) -> None:
        """回归：这条从前印出来是 `⏱      0ms`。

        `durationMs` / `kind` / `name` 都在 `span` 下面，而 `detail` 也在。
        注意外层键是 **`span`**（`TraceSpan` 的缩略），不是 `trace`——
        我写这个文件的第一版就在这里写成了 `{"trace": {...}}`，
        正是这条 bug 的同一个形状：一个嵌套载荷挂在一个"看起来对"的键上。
        """
        line = render(
            "trace",
            {"span": {"kind": "search", "name": "示例品牌 定价策略", "durationMs": 812, "detail": {"hits": 9}}},
        )

        assert "search" in line
        assert "示例品牌 定价策略" in line
        assert "812" in line
        assert "hits" in line

    def test_a_message_renders_both_ends(self) -> None:
        """回归：这条从前印出来是 `→  ⇒ ：`。"""
        line = render(
            "message",
            {"message": {"from": "L3-001", "to": "L2-001", "summary": "队伍已就位", "issueCount": 0}},
        )

        assert "L3-001" in line
        assert "L2-001" in line
        assert "队伍已就位" in line

    def test_an_evidence_renders_its_score_and_title(self) -> None:
        line = render(
            "evidence",
            {"evidence": {"evidenceId": "EV-abc", "credibility": 67.0, "title": "少数派体验报告", "degraded": False}},
        )

        assert "EV-abc" in line
        assert "67.0" in line
        assert "少数派体验报告" in line

    def test_a_degraded_evidence_is_marked(self) -> None:
        """降级的证据要能一眼看出来：它是"这条只有摘要、没有正文"的标记。"""
        marked = render("evidence", {"evidence": {"evidenceId": "EV-1", "degraded": True}})
        clean = render("evidence", {"evidence": {"evidenceId": "EV-1", "degraded": False}})

        assert marked.split("]")[0] != clean.split("]")[0]

    @pytest.mark.parametrize("event_type", sorted(NESTED_PAYLOADS))
    def test_a_missing_nested_payload_does_not_crash(self, event_type: str) -> None:
        """载荷缺失或类型不对时照常产出那一行，不抛异常。

        事件流里出现一个形状不对的载荷时，正确的反应是"这一行渲染得很难看"，
        而不是让整个 CLI 在跑到一半时崩掉——那时的现场只剩一个 traceback，
        而本来要看的那条信息已经没了。
        """
        for bad in ({}, {"x": 1}, {NESTED_PAYLOADS[event_type]: None}, {NESTED_PAYLOADS[event_type]: "字符串"}):
            render(event_type, bad)  # 不该抛

    def test_a_flat_payload_for_a_nested_type_does_not_crash(self) -> None:
        """万一有人按摊平的方式发（契约改了但发布是分先后的），也不该崩。"""
        render("thought", {"expertName": "沈衡", "text": "摊平发的"})  # 不该抛


# ============================================================
# node_update
# ============================================================


class TestNodeUpdate:
    def test_it_reads_stage_and_label(self) -> None:
        line = render(
            "node_update",
            {"stage": "collect", "label": "联网采集", "status": "running", "progress": 0.14, "round": 0},
        )

        assert "联网采集" in line
        assert "14.0%" in line
        assert ">" in line  # running 的记号

    def test_it_falls_back_to_stage_when_label_is_absent(self) -> None:
        line = render("node_update", {"stage": "collect", "status": "done", "progress": 1.0})

        assert "collect" in line

    def test_it_does_not_read_the_obsolete_node_key(self) -> None:
        """**这条是那个改名的回归测试。**

        契约里这个键叫 `stage`。从前渲染器读的是 `node`——一个不存在的键。
        它没被发现，是因为兜底读了 `label`，而 `label` 恰好是必填字段。
        所以这里给一个"只有旧键"的载荷：如果渲染器还认 `node`，它会印出来。
        """
        line = render("node_update", {"node": "这是一个已废弃的键", "status": "done", "progress": 1.0})

        assert "这是一个已废弃的键" not in line

    def test_the_round_suffix_appears_only_for_reworks(self) -> None:
        """返工轮次要显出来（"采集（第 2 轮）"），但第 0 轮不该加个尾巴。"""
        first = render("node_update", {"stage": "collect", "label": "联网采集", "status": "running", "progress": 0.1, "round": 0})
        second = render("node_update", {"stage": "collect", "label": "联网采集", "status": "running", "progress": 0.1, "round": 2})

        assert "第" not in first
        assert "第 2 轮" in second

    def test_a_missing_progress_leaves_the_column_blank(self) -> None:
        """没有进度时留白，不印 `0.0%`——那会让"还没开始"看起来像"刚开始"。"""
        line = render("node_update", {"stage": "collect", "label": "联网采集", "status": "pending"})

        assert "0.0%" not in line


# ============================================================
# report_ready：不编造判断
# ============================================================


class TestReportReady:
    def test_it_does_not_invent_a_quality_verdict(self) -> None:
        """**这一条守的是"缺字段时不说话"。**

        契约里 `report_ready` 没有 `quality`（标题与质量门要到报告页阶段
        才进契约）。从前这里读不到就默认"未通过、完整度 0%"，
        于是终端上印着"质量门=未通过"，而同一段输出里审计专家刚说过
        "质量门结论：通过"。自相矛盾的两行字，读者只会相信错的那一行。
        """
        line = render(
            "report_ready",
            {"reportId": "RP-1", "sectionCount": 4, "evidenceCount": 216, "problems": [], "degraded": []},
        )

        assert "未通过" not in line
        assert "质量门" not in line
        assert "完整度" not in line

    def test_it_shows_counts_and_flags(self) -> None:
        line = render(
            "report_ready",
            {
                "reportId": "RP-1",
                "sectionCount": 4,
                "evidenceCount": 216,
                "problems": ["出库校验：缺 glossary"],
                "degraded": ["舆情：平台上没有可用评论"],
            },
        )

        assert "RP-1" in line
        assert "章节=4" in line
        assert "证据=216" in line
        assert "问题=1" in line
        assert "降级=1" in line

    def test_a_clean_report_says_so_without_a_count(self) -> None:
        line = render("report_ready", {"reportId": "RP-1", "problems": [], "degraded": []})

        assert "无问题" in line
        assert "降级" not in line


# ============================================================
# done：从 metrics 里读，不读顶层
# ============================================================


class TestDone:
    def test_it_reads_the_numbers_from_metrics(self) -> None:
        """**回归：这三个数在 `metrics` 里，不在顶层。**

        契约明确写了"elapsedMs / reworkRounds / qualityPassed 不再单独带 ——
        三者都在 metrics 里"。从前读顶层得到三个默认值 0，
        于是终端上永远印"耗时 0ms，返工 0 轮"，而那个 0 看起来是个测量结果。
        """
        line = render(
            "done",
            {
                "reportId": "RP-1",
                "metrics": {"durationMs": 12345, "reworkRounds": 2, "qualityGatePassed": True},
                "degraded": [],
                "problems": [],
            },
        )

        assert "12345" in line
        assert "2 轮" in line
        assert "质量门通过" in line

    def test_a_top_level_elapsed_ms_is_ignored(self) -> None:
        """万一有人把旧字段加回顶层，`metrics` 仍是唯一被读的那一份。

        两个描述同一件事的字段迟早会不一致，而"不一致时读哪一个"
        必须是一个已经决定好的答案，不能随缘。
        """
        line = render(
            "done",
            {"reportId": "RP-1", "elapsedMs": 999999, "metrics": {"durationMs": 12345}},
        )

        assert "12345" in line
        assert "999999" not in line

    def test_a_failed_gate_is_shown(self) -> None:
        line = render("done", {"metrics": {"qualityGatePassed": False}})

        assert "质量门未通过" in line

    def test_degraded_blocks_are_counted(self) -> None:
        line = render("done", {"metrics": {}, "degraded": ["舆情：无可用评论", "抓取：超时"]})

        assert "降级 2 处" in line

    def test_a_done_without_metrics_still_renders(self) -> None:
        """`metrics` 缺失时印 0 是**可以**的：这一条事件里本来就该有它，
        缺了说明上游出了问题，而 `[完成] 耗时 0ms` 在一个坏了的事件流里
        也不算撒谎。与 `report_ready` 的区别在于契约有没有承诺那个键。"""
        line = render("done", {"reportId": "RP-1"})

        assert "[完成]" in line


# ============================================================
# SSE 帧解析
# ============================================================


class TestParseSseFrame:
    def test_it_splits_type_and_payload(self) -> None:
        frame = 'id: 7\nevent: thought\ndata: {"thought": {"text": "你好"}}\n\n'

        event_type, payload = parse_sse_frame(frame)

        assert event_type == "thought"
        assert payload["thought"]["text"] == "你好"

    def test_it_keeps_the_envelope_out_of_the_payload(self) -> None:
        """`id:` 那一行是信封，不进载荷。

        `seq` 的唯一来源是 SSE 的 `id:` 字段——它在载荷里再出现一次的话，
        同一件事就有了两个真相源，而它们迟早会不一致。
        """
        frame = 'id: 7\nevent: done\ndata: {"reportId": "RP-1"}\n\n'

        _, payload = parse_sse_frame(frame)

        assert "seq" not in payload
        assert payload == {"reportId": "RP-1"}

    def test_a_malformed_data_line_yields_an_empty_payload(self) -> None:
        event_type, payload = parse_sse_frame("event: done\ndata: {不是 JSON\n\n")

        assert event_type == "done"
        assert payload == {}

    def test_a_comment_or_padding_frame_yields_nothing(self) -> None:
        """心跳/注释帧不该被当成事件。"""
        assert parse_sse_frame(": keep-alive\n\n") == ("", {})
