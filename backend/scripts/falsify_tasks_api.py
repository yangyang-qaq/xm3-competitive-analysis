"""反证 `test_tasks_api.py` 的守卫：把缺陷放回去，看测试是否真的红。

一条抓不住 bug 的测试比没有测试更坏——它给出的是"这里有人看着"的错觉。
所以每条守卫都要反证一次：**把漏洞放回去，测试必须失败**。

这个脚本比 `falsify_cli_render.py` 多一件事：它跨三个文件做变异，
所以原文件按路径存一份备份，结束时逐字节比对还原。
`问题记录.md` 问题 10 记过"注释改了、代码没改，比不改更坏"——
还原不彻底会留下一个看起来验证过的现场，所以这一步是自断言的。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

PY = str(pathlib.Path(".venv/Scripts/python.exe").resolve())
TESTS = "tests/integration/test_tasks_api.py"

RUNNER = "app/core/pipeline/runner.py"
ROUTES = "app/api/routes_tasks.py"
ORCH = "app/core/pipeline/orchestrator.py"
MAIN = "app/main.py"

#: (名字, 文件, 原文, 替换成, 至少应失败的测试名片段)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "M1 让 GET /stream 顺手把任务启动起来（参考实现的 bug）",
        ROUTES,
        '    log.info("任务 %s 不在内存中，改为回放库里的历史事件", task_id)',
        '    log.info("任务 %s 不在内存中，改为回放库里的历史事件", task_id)\n'
        '    ensure_runner(task_id, "顺手启动一下", start=True)',
        "test_a_get_on_a_stale_task_does_not_start_it",
    ),
    (
        "M2 重启后不把 seq 接上库里的最大值",
        RUNNER,
        "    if runner.journal.last_seq == 0:\n"
        "        rows = events_repo.list_since(task_id, 0)",
        "    if False:\n"
        "        rows = events_repo.list_since(task_id, 0)",
        "test_the_journal_seq_continues_instead_of_restarting_at_one",
    ),
    (
        "M3 续传起点让 from_seq 压过 Last-Event-ID",
        ROUTES,
        '    raw = (last_event_id or "").strip()\n    if raw:',
        '    raw = (last_event_id or "").strip()\n    if False:',
        "test_last_event_id_wins_over_from_seq",
    ),
    (
        "M4 awaitingClarify 退化成 needClarify 的同义词",
        RUNNER,
        '            "awaitingClarify": self.state.status == AWAITING_CLARIFY,',
        '            "awaitingClarify": self.state.need_clarify,',
        "test_need_clarify_and_awaiting_clarify_are_different_things",
    ),
    (
        "M5 对不在等澄清的任务也收下答案",
        ROUTES,
        '    elif runner.snapshot()["status"] != AWAITING_CLARIFY:',
        "    elif False:",
        "test_clarifying_a_running_task_is_a_conflict",
    ),
    (
        "M6 纯空白的需求放行",
        ROUTES,
        "    if not query:\n"
        '        raise HTTPException(status_code=422, detail="query 不能是空白")',
        "    if False:\n"
        '        raise HTTPException(status_code=422, detail="query 不能是空白")',
        "test_a_blank_query_is_rejected",
    ),
    (
        "M7 需求含糊也不停下来问",
        ORCH,
        "            if pause_on_clarify and ctx.need_clarify and not ctx.clarify_answers:\n"
        "                paused = True\n"
        "                return None",
        "            if False:\n"
        "                paused = True\n"
        "                return None",
        "test_a_vague_query_stops_and_returns_questions",
    ),
    (
        "M8 启动时不建表（靠「记得先跑迁移」兜着，第一个请求 500）",
        MAIN,
        "    applied = migrate(get_conn())",
        "    applied = []",
        "test_the_tables_exist_after_the_app_starts",
    ),
    (
        "M9 唤醒等待者挪回整条流水线的结尾（POST 要等几分钟才响应）",
        RUNNER,
        "                    on_intake_settled=self._intake_settled,",
        "                    on_intake_settled=None,",
        "test_post_returns_while_the_pipeline_is_still_running",
    ),
]


def run_tests() -> tuple[int, int, list[str]]:
    # `-o addopts=` 清掉 pyproject 里的 `-q -m 'not live'`：那里面已经有一个
    # `-q`，再加一个就成了 `-qq`，而 `-qq` 会把汇总行整个删掉。
    proc = subprocess.run(
        [
            PY, "-m", "pytest", TESTS,
            "-o", "addopts=", "-m", "not live",
            "--tb=no", "-rf", "-p", "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = proc.stdout + proc.stderr
    failed = [
        line[len("FAILED "):].split("::")[-1].split(" ")[0]
        for line in out.splitlines()
        if line.startswith("FAILED ")
    ]
    passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", out)) else 0
    failed_count = int(m.group(1)) if (m := re.search(r"(\d+) failed", out)) else 0
    if passed == 0 and failed_count == 0:
        print("  !! 解析不出汇总行，原始输出尾部：")
        print("     " + out.strip()[-400:].replace("\n", "\n     "))
    return passed, failed_count, failed


def main() -> int:
    targets = sorted({path for _, path, _, _, _ in MUTATIONS})
    originals = {path: pathlib.Path(path).read_text(encoding="utf-8") for path in targets}

    # 这个脚本会改源码。`finally` 能盖住异常，盖不住进程被强杀——
    # 那时现场会留下处于变异状态的文件。把路径打出来，让下一个看到的人
    # 知道该还原哪些。
    print("会被改动的文件（结束时逐字节还原）：")
    for path in targets:
        print(f"  {path}")
    print("=" * 74)

    print("基线（未改动）")
    passed, failed_count, _ = run_tests()
    print(f"  {passed} passed, {failed_count} failed")
    if passed == 0:
        print("  基线就是红的——先修好再谈反证。")
        return 1

    verdicts: list[str] = []
    try:
        for name, path, old, new, witness in MUTATIONS:
            original = originals[path]
            count = original.count(old)
            if count != 1:
                print(f"\n{name}\n  !! 锚点在 {path} 里出现了 {count} 次，无法定向替换")
                verdicts.append(f"  {name:52s} 锚点失效")
                continue

            pathlib.Path(path).write_text(original.replace(old, new), encoding="utf-8")
            p, f, names = run_tests()
            caught = any(witness in n for n in names)
            mark = "抓住" if caught else "**漏掉**"
            print(f"\n{name}\n  {path}\n  {p} passed, {f} failed -> {mark}（证人：{witness}）")
            if names:
                shown = "、".join(names[:6])
                more = f" 等 {len(names)} 条" if len(names) > 6 else ""
                print(f"  失败的测试：{shown}{more}")
            verdicts.append(f"  {mark:8s} {name}")

            pathlib.Path(path).write_text(original, encoding="utf-8")
    finally:
        for path, original in originals.items():
            pathlib.Path(path).write_text(original, encoding="utf-8")

    print("\n" + "=" * 74)
    print("汇总")
    for line in verdicts:
        print(line)

    dirty = [
        path
        for path, original in originals.items()
        if pathlib.Path(path).read_text(encoding="utf-8") != original
    ]
    print(f"\n已还原：{not dirty}" + (f"（没还原的：{dirty}）" if dirty else ""))
    return 0 if not dirty else 1


if __name__ == "__main__":
    sys.exit(main())
