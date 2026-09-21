#!/usr/bin/env bash
# 起本地开发环境：后端 8020 + 前端 3500。
#
# 为什么要有这个脚本，而不是 README 里写两行命令让人自己敲
# ------------------------------------------------------
# 这两行命令在 Windows 的 Git Bash 里**不是** README 里写的那样。这个项目
# 在开发机上实测踩过的坑：
#
#   1. `python3` 在这个环境里是坏的（指向一个装不出依赖的解释器）。
#      必须先找 `.venv/Scripts/python.exe`，找不到才退回 `python3`。
#   2. venv 的位置两个平台不一样：Windows 是 `.venv/Scripts/`，
#      macOS / Linux 是 `.venv/bin/`。写死一个，另一个平台就报
#      "No such file or directory"——而 README 里那句照抄下来是能跑的，
#      所以没人会觉得脚本有问题。
#   3. **绝不用 `uvicorn --reload`。** 本机实测：SQLite 文件在
#      `backend/data/` 下，而 `--reload` 监视的是整个工作目录，
#      于是**每次写库都触发一次重载**。表现是服务在跑任务的过程中
#      反复重启，任务永远是 running——看起来像流水线卡死，
#      实际是开发服务器在自杀。要用热重载就 `WATCH=1`，
#      它会加上 `--reload-dir app`，把 data/ 排除在外。
#
# 用法：
#   bash scripts/dev.sh            # 起两个服务，前台等 Ctrl-C
#   WATCH=1 bash scripts/dev.sh    # 后端带热重载（只监视 app/）
#   bash scripts/dev.sh --no-frontend
#
# 端口被占时**直接报错退出**，不"自动换个端口"：换端口之后前端代理
# 还指着 8020，页面会白屏，而报错信息会说"连接被拒绝"，指不到真正的原因。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_PORT="${BACKEND_PORT:-8020}"
FRONTEND_PORT="${FRONTEND_PORT:-3500}"
PID_DIR="$ROOT/.dev"
LOG_DIR="$ROOT/.dev"

START_FRONTEND=1
[ "${1:-}" = "--no-frontend" ] && START_FRONTEND=0

mkdir -p "$PID_DIR" "$LOG_DIR"

say()  { printf '\033[36m[dev]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[dev]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 找解释器 ----------
# 顺序是有意的：venv 优先，系统的兜底。Windows 的 `python` 是 py launcher，
# 而 `python3` 在这台机器上是坏的——所以两者都试，谁先能用用谁。
find_python() {
  for candidate in \
    "$ROOT/backend/.venv/Scripts/python.exe" \
    "$ROOT/backend/.venv/bin/python" \
    "python3" "python"
  do
    if command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ]; then
      if "$candidate" -c "import fastapi" >/dev/null 2>&1; then
        echo "$candidate"; return 0
      fi
    fi
  done
  return 1
}

# ---------- 端口占用检查 ----------
# 用 curl 探而不是看进程表：端口可能被**另一个进程**占着，
# 而那个进程未必叫 uvicorn。
port_busy() {
  curl -s -o /dev/null -m 2 "http://127.0.0.1:$1/health" 2>/dev/null && return 0
  curl -s -o /dev/null -m 2 "http://127.0.0.1:$1/" 2>/dev/null && return 0
  return 1
}

wait_health() {
  local port="$1" waited=0
  while [ "$waited" -lt 30 ]; do
    if curl -s -o /dev/null -m 2 "http://127.0.0.1:$port/health" 2>/dev/null; then
      return 0
    fi
    sleep 1; waited=$((waited + 1))
  done
  return 1
}

# ---------- 后端 ----------
if port_busy "$BACKEND_PORT"; then
  fail "端口 $BACKEND_PORT 上已经有服务在响应了。
      要么它就是你之前起的（先跑 bash scripts/stop.sh），
      要么是别的程序——那要换端口的话，前端的代理也要跟着改。
      这个脚本不替你换端口：换完之后前端还指着 $BACKEND_PORT，页面会白屏。"
fi

PY="$(find_python)" || fail "找不到可用的解释器。
      先建好环境：cd backend && python -m venv .venv && .venv/Scripts/python.exe -m pip install -e \".[dev]\""

say "后端解释器：$PY"
say "日志：$LOG_DIR/backend.log"

RELOAD_ARGS=()
if [ "${WATCH:-0}" = "1" ]; then
  RELOAD_ARGS=(--reload --reload-dir app)
  say "热重载已开，**只监视 app/**（监视整目录会让每次写库都重启，见脚本头部注释）"
fi

(
  cd "$ROOT/backend" || exit 1
  PYTHONIOENCODING=utf-8 exec "$PY" -m uvicorn app.main:app \
    --host 127.0.0.1 --port "$BACKEND_PORT" "${RELOAD_ARGS[@]+"${RELOAD_ARGS[@]}"}"
) >"$LOG_DIR/backend.log" 2>&1 &
echo $! >"$PID_DIR/backend.pid"

if ! wait_health "$BACKEND_PORT"; then
  fail "后端 30 秒内没有起来。日志最后 20 行：
$(tail -20 "$LOG_DIR/backend.log" 2>/dev/null)"
fi
say "后端就绪：http://127.0.0.1:$BACKEND_PORT （/health 有响应）"

# ---------- 前端 ----------
if [ "$START_FRONTEND" = "1" ]; then
  if port_busy "$FRONTEND_PORT"; then
    say "端口 $FRONTEND_PORT 上已经有东西（大概是上一次留下的 vite），跳过启动"
  else
    command -v npm >/dev/null 2>&1 || fail "找不到 npm。装 Node ≥ 20 之后再来。"
    if [ ! -d "$ROOT/frontend/node_modules" ]; then
      say "node_modules 不存在，先跑一次 npm install（可能要几分钟）"
      (cd "$ROOT/frontend" && npm install) || fail "npm install 失败"
    fi
    (
      cd "$ROOT/frontend" || exit 1
      exec npm run dev
    ) >"$LOG_DIR/frontend.log" 2>&1 &
    echo $! >"$PID_DIR/frontend.pid"
    say "前端日志：$LOG_DIR/frontend.log"
    # vite 就绪没有可靠的探针（根路径返回的是 HTML，不是 JSON），
    # 所以这里只等端口连得上，不假装知道它"编译完成了"。
    waited=0
    while [ "$waited" -lt 30 ]; do
      curl -s -o /dev/null -m 2 "http://127.0.0.1:$FRONTEND_PORT/" 2>/dev/null && break
      sleep 1; waited=$((waited + 1))
    done
  fi
  say "前端：http://localhost:$FRONTEND_PORT"
fi

echo
say "两个服务都在后台跑。Ctrl-C 只停掉这个脚本，服务还在——"
say "停服务请跑：bash scripts/stop.sh"
echo

# 前台等，这样 Ctrl-C 有反馈（并且提示用户服务并没有被停掉）。
trap 'echo; say "脚本退出，服务仍在后台。停止请跑 bash scripts/stop.sh"; exit 0' INT TERM
while true; do sleep 3600; done
