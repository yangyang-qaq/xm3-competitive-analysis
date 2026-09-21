"""反证 `test_cli_render.py` 的守卫：把渲染器改回坏的样子，看测试是否真的红。

一条抓不住 bug 的测试比没有测试更坏——它给出的是"这里有人看着"的错觉。
所以每条守卫都要反证一次：**把漏洞放回去，测试必须失败**。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

PY = str(pathlib.Path(".venv/Scripts/python.exe").resolve())
TARGET = pathlib.Path("scripts/run_pipeline_cli.py")
BACKUP = pathlib.Path(".run_pipeline_cli.falsify.bak")
TESTS = "tests/unit/test_cli_render.py"

#: (名字, 原文, 替换成, 至少应失败的测试名片段)
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "M1 嵌套载荷按顶层读（原本的 bug）",
        "    value = data.get(NESTED_PAYLOADS[event_type])\n"
        "    return value if isinstance(value, dict) else {}",
        "    return data",
        "test_a_thought_renders_its_expert_and_text",
    ),
    (
        "M2 node_update 读已废弃的 `node`",
        '        label = data.get("label") or data.get("stage", "")',
        '        label = data.get("node", "")',
        "test_it_does_not_read_the_obsolete_node_key",
    ),
    (
        "M3 done 读顶层而不是 metrics",
        '        metrics = data.get("metrics") or {}',
        "        metrics = data",
        "test_it_reads_the_numbers_from_metrics",
    ),
    (
        "M4 report_ready 编造质量门结论",
        '        flags = f"问题={len(problems)}" if problems else "无问题"',
        '        flags = "质量门=未通过 完整度=0%"',
        "test_it_does_not_invent_a_quality_verdict",
    ),
    (
        "M5 NESTED_PAYLOADS 与契约漂移",
        '    "trace": "span",',
        '    "trace": "trace",',
        "test_nested_payload_map_matches_the_contract",
    ),
]


def run_tests() -> tuple[int, int, list[str]]:
    # `-o addopts=` 清掉 pyproject 里的 `-q -m 'not live'`：那里面已经有一个
    # `-q`，我再加一个就成了 `-qq`，而 `-qq` 会把汇总行整个删掉——
    # 于是这个脚本读到 0 passed，把一次成功的基线误判成"红的"。
    # 清掉之后 `not live` 要自己带上，否则 live 测试会真的去联网。
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
    # 用正则而不是 `endswith("passed")`：汇总行是
    # `===== 32 passed in 0.14s =====`，以等号结尾，后缀判断恒不成立。
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
    original = TARGET.read_text(encoding="utf-8")
    BACKUP.write_text(original, encoding="utf-8")

    # 这个脚本会改源码。`finally` 能盖住异常，但盖不住进程被强杀——
    # 那时现场会留下一个 `.bak` 和一个处于变异状态的目标文件。
    # 把路径打出来，是为了让下一个看到它的人知道那是什么、能还原回去。
    print(f"目标：{TARGET}（改前备份在 {BACKUP}，正常结束时自动删除）")
    print("=" * 74)
    print("基线（未改动）")
    passed, failed_count, _ = run_tests()
    print(f"  {passed} passed, {failed_count} failed")
    if passed == 0:
        print("  基线就是红的——先修好再谈反证。")
        # 这条提前返回在 `try` 之外，所以备份得自己收拾。
        # 此时目标文件还没被改过（这是基线那一次），只是别留下垃圾。
        BACKUP.unlink(missing_ok=True)
        return 1

    verdicts: list[str] = []
    try:
        for name, old, new, witness in MUTATIONS:
            if original.count(old) != 1:
                print(f"\n{name}\n  !! 锚点出现了 {original.count(old)} 次，无法定向替换")
                verdicts.append(f"  {name:44s} 锚点失效")
                continue
            TARGET.write_text(original.replace(old, new), encoding="utf-8")
            p, f, names = run_tests()
            caught = any(witness in n for n in names)
            mark = "抓住" if caught else "**漏掉**"
            print(f"\n{name}\n  {p} passed, {f} failed -> {mark}（证人：{witness}）")
            if names:
                shown = "、".join(names[:6])
                more = f" 等 {len(names)} 条" if len(names) > 6 else ""
                print(f"  失败的测试：{shown}{more}")
            verdicts.append(f"  {mark:8s} {name}")
    finally:
        TARGET.write_text(original, encoding="utf-8")
        BACKUP.unlink(missing_ok=True)

    print("\n" + "=" * 74)
    print("汇总")
    for line in verdicts:
        print(line)

    restored = TARGET.read_text(encoding="utf-8") == original
    print(f"\n已还原：{restored}")
    return 0 if restored else 1


if __name__ == "__main__":
    sys.exit(main())
