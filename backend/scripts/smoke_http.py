"""真起一个服务，走一遍 HTTP：建任务 → 澄清 → 续传 → 跑完。

为什么需要这个脚本
------------------
测试套件里的 `TestClient` **在同一个进程里**跑，共享已经 import 好的模块、
已经缓存的 Settings、以及夹具自己建好的表。所以有三类问题它结构性地看不见：

1. **启动路径**。`lifespan` 里少了一句建表，测试全绿，而真实的服务器
   在第一个读库的请求上 500——夹具自己调了 `migrate()`，把这条路盖住了。
   这个缺陷就是被这个脚本发现的（见 `问题记录.md`）。
2. **序列化**。中文请求体在 Windows 控制台上会被代码页改掉，
   进程内测试直接传 Python 字符串，不经过任何编码。
3. **SSE 的真实形态**。`id:` 行、空行分隔、心跳注释帧——
   进程内测试拿到的是生成器吐出的字符串，没有经过 HTTP 分帧。

跑法（不需要先起服务，脚本自己起、自己收）：

    python -m scripts.smoke_http --port 8021

**零花费**：三个 provider 全部走 mock，库是一个临时文件，跑完删掉。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]

#: 明显含糊的一句话需求——它必须触发澄清，否则这个脚本的一半流程跑不到。
VAGUE_QUERY = "帮我看看那个笔记软件"

#: 等服务的总时长。冷启动要读 48 人专家名册 + 建表，实测在一秒上下。
STARTUP_TIMEOUT = 30.0

#: 一个"预期不会再有新事件"的流读多久算读完了。
#: 不能设成 0：暂停之后 journal 是**开着**的，流不会自己结束——
#: 这正是要验证的性质之一（暂停不能把 SSE 连接关掉）。
QUIET_SECONDS = 1.5


class Failed(AssertionError):
    """冒烟失败。和普通断言分开，是为了让输出能明确说"这是环境问题还是代码问题"。"""


# ============================================================
# 服务进程
# ============================================================


def _free_port(preferred: int) -> int:
    """优先用指定端口，被占了就换一个。

    不做"占着就报错退出"：本机上 8020 常年挂着一个别的实例，
    开发时手滑跑重很常见，而这件事和被测代码无关。
    """
    for candidate in (preferred, 0):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return probe.getsockname()[1]
    raise Failed("找不到可用端口")


def _start(port: int, db_path: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        # 子进程的输出也要能打中文，否则失败信息本身就是乱码。
        "PYTHONIOENCODING": "utf-8",
        "DB_PATH": str(db_path),
        "LLM_PROVIDER": "mock",
        "SEARCH_PROVIDER": "mock",
        "FETCH_PROVIDER": "mock",
        "CASSETTE_MODE": "off",
    }
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning",
        ],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


async def _wait_ready(base: str, proc: subprocess.Popen) -> dict:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    async with httpx.AsyncClient(base_url=base, timeout=2.0) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise Failed(f"服务进程提前退出（code={proc.returncode}）：\n{_drain(proc)}")
            with contextlib.suppress(httpx.HTTPError):
                response = await client.get("/health")
                if response.status_code == 200:
                    return response.json()
            await asyncio.sleep(0.2)
    raise Failed(f"{STARTUP_TIMEOUT}s 内 /health 没起来：\n{_drain(proc)}")


def _drain(proc: subprocess.Popen) -> str:
    """把子进程的输出收回来。不这么做的话，失败时的报错里只有一句
    "服务起不来"，而真正的原因（端口占用、import 错误）全在被丢弃的管道里。"""
    if proc.stdout is None:
        return "(没有输出)"
    with contextlib.suppress(Exception):
        proc.terminate()
        return proc.communicate(timeout=5)[0] or "(没有输出)"
    return "(收不回来)"


# ============================================================
# SSE
# ============================================================


def parse_frames(text: str) -> list[dict]:
    """把 SSE 报文拆成帧。

    刻意**手写解析**而不是复用后端的 `PipelineEvent.from_row`：
    这个脚本要验证的正是"发到线上的是不是标准 SSE"。
    用后端的解析器会把它自己发的格式当成正确的前提。
    """
    frames = []
    for block in text.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue  # 空块与心跳注释帧
        frame: dict = {"raw": block}
        for line in block.splitlines():
            key, _, value = line.partition(":")
            value = value.lstrip(" ")
            if key == "id":
                frame["id"] = int(value)
            elif key == "event":
                frame["event"] = value
            elif key == "data":
                frame["data"] = value
        frames.append(frame)
    return frames


async def read_stream(
    client: httpx.AsyncClient, task_id: str, *, seconds: float, from_seq: int = 0
) -> tuple[list[dict], str]:
    """读一段流，到时间就断开。

    **主动断开是刻意的**：任务停在澄清处时 journal 是开着的，流永远不会
    自己结束。等它结束就等于等一个超时——而且那样也验证不了
    "断开之后还能按水位续上"。

    返回 (帧, 原始报文)。原始报文留着是为了断言 `id:` 行确实存在。
    """
    chunks: list[str] = []

    async def _pump() -> None:
        headers = {"Last-Event-ID": str(from_seq)} if from_seq else {}
        async with client.stream(
            "GET", f"/api/tasks/{task_id}/stream", headers=headers, timeout=30.0
        ) as response:
            if response.status_code != 200:
                raise Failed(f"stream 返回 {response.status_code}")
            async for chunk in response.aiter_text():
                chunks.append(chunk)

    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(_pump(), seconds)
    raw = "".join(chunks)
    return parse_frames(raw), raw


# ============================================================
# 检查
# ============================================================


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"：{detail}" if detail and not ok else ""))
    if not ok:
        raise Failed(f"{label} —— {detail}")


def check_contiguous(first: list[dict], second: list[dict]) -> None:
    """两次连接的帧合起来必须是 1..N 一条不差。

    这是"断线续传"唯一诚实的验收方式：只看"两边都有东西"会漏掉
    重复（两边都从 1 开始）和缺口（水位算错了），而这两种在界面上
    都表现为"看着差不多"。
    """
    seqs = [frame["id"] for frame in first + second]
    check(f"两次连接共 {len(seqs)} 帧，seq 无缺口无重复",
          seqs == list(range(1, len(seqs) + 1)),
          f"实际 {seqs[:5]}…{seqs[-5:]}" if seqs else "一帧都没有")
    check("第二次连接的帧全在水位之后",
          all(frame["id"] > (first[-1]["id"] if first else 0) for frame in second),
          "水位没起作用，重发了已经看过的内容")


async def run(port: int, db_path: Path) -> None:
    base = f"http://127.0.0.1:{port}"
    proc = _start(port, db_path)
    try:
        health = await _wait_ready(base, proc)
        print(f"\n服务已就绪（port {port}，库 {db_path.name}）")
        print(f"  provider: llm={health.get('llm_provider')} "
              f"search={health.get('search_provider')}")

        async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
            # ---- 1. 建任务：含糊的需求必须停下来问 ----
            print("\n1. POST /api/tasks（含糊需求）")
            created = (
                await client.post(
                    "/api/tasks", json={"query": VAGUE_QUERY, "mode": "quick"}
                )
            ).json()
            task_id = created["taskId"]
            check("返回 201 语义的任务快照", bool(task_id), json.dumps(created)[:200])
            check("停在等澄清", created["status"] == "awaiting_clarify", created["status"])
            check("awaitingClarify 为真", created["awaitingClarify"] is True)
            check("带回了问题", bool(created["clarifyQuestions"]),
                  "停了却没有问题，澄清页无事可做")
            print(f"     任务 {task_id}，问了 {len(created['clarifyQuestions'])} 个问题")

            # ---- 2. 打开流：暂停**不关**连接 ----
            print("\n2. GET /api/tasks/{id}/stream（暂停中）")
            first, raw = await read_stream(client, task_id, seconds=QUIET_SECONDS)
            check("收到了帧", bool(first), "一帧都没有")
            check("报文里带 id: 行（浏览器靠它断线续传）", "\nid:" in "\n" + raw.strip())
            check("没有 done——任务还停着",
                  not any(frame.get("event") == "done" for frame in first))
            check("已经能看到需求理解那一段",
                  any(frame.get("event") == "thought" for frame in first))
            watermark = first[-1]["id"]
            print(f"     读到 {len(first)} 帧，水位 seq={watermark}")

            # ---- 3. 交答案，任务接着跑 ----
            print("\n3. POST /api/tasks/{id}/clarify")
            answered = (
                await client.post(
                    f"/api/tasks/{task_id}/clarify",
                    json={"answers": {q["id"]: q["recommended"] or "没有偏好"
                                      for q in created["clarifyQuestions"]}},
                )
            ).json()
            check("脱离等待状态", answered["status"] not in ("awaiting_clarify", "pending"),
                  answered["status"])
            check("awaitingClarify 转假", answered["awaitingClarify"] is False)

            # ---- 4. 按水位续传，读到跑完 ----
            print("\n4. 带 Last-Event-ID 重连")
            second, _ = await read_stream(client, task_id, seconds=60.0,
                                          from_seq=watermark)
            check("补上了后面的帧", bool(second), "续传之后什么都没有")
            check_contiguous(first, second)
            check("流被 done 正常收尾",
                  any(frame.get("event") == "done" for frame in second),
                  "没有 done，任务可能没跑完")

            # ---- 5. 落库的东西能读回来（刷新页面走的就是这条路）----
            print("\n5. GET /api/tasks/{id}（模拟刷新页面）")
            snapshot = (await client.get(f"/api/tasks/{task_id}")).json()
            check("状态是终态", snapshot["status"] == "done", snapshot["status"])
            check("刷新之后不再是等澄清", snapshot["awaitingClarify"] is False,
                  "刷新会把跑完的任务拉回澄清页")

            trace = (await client.get(f"/api/tasks/{task_id}/trace")).json()
            check("埋点落了库", trace["summary"]["spanCount"] > 0,
                  "一条 span 都没有——tracer 没绑进上下文")

        print("\n全部通过。" + "=" * 50)
    finally:
        if proc.poll() is None:
            proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8021,
                        help="优先使用的端口；被占用时自动换一个")
    parser.add_argument("--keep-db", action="store_true",
                        help="保留临时库，方便事后翻数据")
    args = parser.parse_args()

    port = _free_port(args.port)
    with tempfile.TemporaryDirectory(prefix="xm3-smoke-") as tmp:
        db_path = Path(tmp) / "smoke.db"
        try:
            asyncio.run(run(port, db_path))
        except Failed as exc:
            print(f"\n冒烟失败：{exc}", file=sys.stderr)
            return 1
        if args.keep_db:
            kept = REPO / "data" / "smoke.db"
            kept.parent.mkdir(parents=True, exist_ok=True)
            # 连 -wal / -shm 一起搬走：只搬主文件的话，WAL 里还没落盘的那些
            # 事件会消失，看起来像"库是空的"。
            for suffix in ("", "-wal", "-shm"):
                side = Path(str(db_path) + suffix)
                if side.exists():
                    side.replace(Path(str(kept) + suffix))
            print(f"库已保留在 {kept}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
