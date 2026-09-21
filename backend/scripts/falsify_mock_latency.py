"""反证 `test_mock_latency.py`：把 `MOCK_LATENCY_MS` 的接线拆掉，测试必须红。

一条抓不住 bug 的测试比没有测试更坏。`MOCK_LATENCY_MS` 尤其需要反证：
它是一条**行为**开关（让 mock 慢下来），而"开关没接上"的表现与"接上了但没人
设这个值"完全一样——任何现有的测试都不会红，唯一的证据就是把它拆掉看红不红。

为什么这个脚本要按**行**操作，而不是按字符串拼接
--------------------------------------------------
第一版用 `CALL.join(pieces[:1] + pieces[2:])` 删第一处调用，意图是"去掉一个
`_sleep_like_a_real_provider` 调用"。实际效果是**删掉了两个分隔符之间的 88 行
代码**（`pieces[1]` 就是那段），于是 `app.providers.mock` 里不再有 `MockFetcher`，
测试报的是收集期 `ImportError`。

而这个"红"是**假的**：任何一个 import 这个模块的测试都会红，和被测的那条守卫
没有半点关系。它不是"抓住了缺陷"，它是"把程序弄坏了"。所以这里的判据是两条：

  1. 每一处编辑都**只动它该动的那几行**（改完核对行数增删）
  2. 出红的必须是**点名的**那条测试（和 `falsify_cli_render.py` 同一条规矩）

收集期错误（`ERROR ` 开头的行）一律不算抓住——宁可报"漏掉"再去查，
也不能把一个语法错误当成守卫有效的证据。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

PY = str(pathlib.Path(".venv/Scripts/python.exe").resolve())
BACKEND = pathlib.Path(".")
MOCK = "app/providers/mock.py"
CONFIG = "app/core/config.py"
TESTS = "tests/unit/providers/test_mock_latency.py"

SLEEP = "        _sleep_like_a_real_provider(self._settings)"
SEARCH_FAIL = (
    "        if query.text in self.fail_queries:\n"
    '            raise Transient("mock：这条 query 被配置为失败", provider=self.name)\n'
)
CHAT_FAIL = (
    "        if purpose in self.fail_purposes:\n"
    '            raise Transient(f"mock：purpose={purpose!r} 被配置为失败", provider=self.name)\n'
)

#: (名字, 目标文件, [(原文, 替换成), ...], 至少应失败的测试名片段)
MUTATIONS: list[tuple[str, str, list[tuple[str, str]], str]] = [
    (
        "M1 删掉 search 里的等待（搜索不再受开关控制）",
        MOCK,
        [(f"{SLEEP}\n{SEARCH_FAIL}", SEARCH_FAIL)],
        "test_搜索走这个开关",
    ),
    (
        "M2 等待挪到 search 的失败判断之后（失败路径瞬回）",
        MOCK,
        [(f"{SLEEP}\n{SEARCH_FAIL}", f"{SEARCH_FAIL}{SLEEP}\n")],
        "test_搜索失败的那次调用也一样等待",
    ),
    (
        "M3 删掉 LLM 的等待（补全不再受开关控制）",
        MOCK,
        [(f"{SLEEP}\n        model = self.resolve_model(tier)", "        model = self.resolve_model(tier)")],
        "test_LLM_补全走这个开关",
    ),
    (
        "M4 等待挪到 LLM 的失败判断之后",
        MOCK,
        [
            (f"{SLEEP}\n        model = self.resolve_model(tier)", "        model = self.resolve_model(tier)"),
            (CHAT_FAIL, f"{CHAT_FAIL}{SLEEP}\n"),
        ],
        "test_LLM_失败的那次调用也一样等待",
    ),
    (
        "M5 删掉 fetch 里的等待（抓取不再受开关控制）",
        MOCK,
        [(f"{SLEEP}\n        digest = _digest(url)", "        digest = _digest(url)")],
        "test_抓取走这个开关",
    ),
    (
        "M6 默认值改成 100（整个测试套件陪着等）",
        CONFIG,
        [("    mock_latency_ms: int = 0", "    mock_latency_ms: int = 100")],
        "test_默认不等待",
    ),
]


def run_tests() -> tuple[int, int, list[str], bool]:
    """跑测试。返回 (passed, failed, 失败的测试名, 是否有收集期错误)。

    `-o addopts=` 清掉 pyproject 里的 `-q -m 'not live'`：那里面已经有一个
    `-q`，再加一个成了 `-qq`，而 `-qq` 会把汇总行整个删掉——于是脚本读到
    0 passed，把一次成功的基线误判成红的。清掉之后 `not live` 要自己带上。
    """
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
    # 收集期错误：模块导不进来、夹具炸了。这**不是**证据，见文件头的说明。
    collect_error = any(line.startswith("ERROR ") for line in out.splitlines())
    passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", out)) else 0
    failed_count = int(m.group(1)) if (m := re.search(r"(\d+) failed", out)) else 0
    if passed == 0 and failed_count == 0:
        print("  !! 解析不出汇总行，原始输出尾部：")
        print("     " + out.strip()[-400:].replace("\n", "\n     "))
    return passed, failed_count, failed, collect_error


def main() -> int:
    paths = {MOCK: BACKEND / MOCK, CONFIG: BACKEND / CONFIG}
    originals = {rel: path.read_text(encoding="utf-8") for rel, path in paths.items()}

    print(f"目标：{MOCK}、{CONFIG}（正常结束时自动还原）")
    print("=" * 74)
    print("基线（未改动）")
    passed, failed_count, _, _ = run_tests()
    print(f"  {passed} passed, {failed_count} failed")
    if passed == 0:
        print("  基线就是红的——先修好再谈反证。")
        return 1

    verdicts: list[str] = []
    try:
        for name, rel, edits, witness in MUTATIONS:
            original = originals[rel]
            broken = False
            for old, new in edits:
                if original.count(old) != 1:
                    print(f"\n{name}\n  !! 锚点出现了 {original.count(old)} 次，无法定向替换")
                    broken = True
                    break
                original = original.replace(old, new)
            if broken:
                verdicts.append(f"  锚点失效   {name}")
                continue

            delta = original.count("\n") - originals[rel].count("\n")
            print(f"\n{name}\n  {rel}  行数变化 {delta:+d}")
            paths[rel].write_text(original, encoding="utf-8")
            p, f, names, collect_error = run_tests()
            paths[rel].write_text(originals[rel], encoding="utf-8")

            if collect_error and not names:
                mark, note = "**无效**", "（收集期就炸了，这不算证据）"
            else:
                mark = "抓住" if any(witness in n for n in names) else "**漏掉**"
                note = ""
            print(f"  {p} passed, {f} failed -> {mark}（证人：{witness}）{note}")
            if names:
                shown = "、".join(names[:6])
                more = f" 等 {len(names)} 条" if len(names) > 6 else ""
                print(f"  失败的测试：{shown}{more}")
            verdicts.append(f"  {mark:8s} {name}")
    finally:
        for rel, path in paths.items():
            path.write_text(originals[rel], encoding="utf-8")

    print("\n" + "=" * 74)
    print("汇总")
    for line in verdicts:
        print(line)

    restored = all(
        path.read_text(encoding="utf-8") == originals[rel] for rel, path in paths.items()
    )
    print(f"\n已还原：{restored}")
    return 0 if restored else 1


if __name__ == "__main__":
    sys.exit(main())
