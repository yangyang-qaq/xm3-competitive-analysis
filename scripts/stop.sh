#!/usr/bin/env bash
# 停掉 scripts/dev.sh 起的两个服务。
#
# 为什么不能只 `kill $(cat .dev/backend.pid)`
# ------------------------------------------
# 本机实测过一个真事故：PID 文件是**上一次**留下的，那个进程早就死了，
# 而操作系统把同一个 PID 号**分配给了别的程序**。于是 `kill` 一个"看起来
# 是我们自己的"号，杀掉的是一个完全无关的进程。
#
# PID 是会被复用的，**光凭一个数字不足以说明它属于谁**。所以这里分两步：
#
#   1. 先按 PID 文件杀（快路径，同一个 shell 会话里够用）。
#   2. 杀完**回头探端口**。端口还在响应，说明第 1 步没杀对
#      （PID 文件过期了，或者 Git Bash 的 MSYS PID 和 Windows 的 PID
#      不是一套编号，跨会话时 `kill` 打不中），
#      这时才按"谁在占这个端口"去杀。
#
# 第 2 步是 Windows 上真正管用的那条路：`netstat -ano` 给出的是
# Windows 自己的 PID，喂给 `taskkill` 才作数。直接 `kill` 一个 MSYS 号
# 在别的会话里是静默失败的——**脚本会打印"已停止"，而服务还在跑**。
#
# 所以最后的验收是**端口**，不是"kill 命令没报错"。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_DIR="$ROOT/.dev"
BACKEND_PORT="${BACKEND_PORT:-8020}"
FRONTEND_PORT="${FRONTEND_PORT:-3500}"

say() { printf '\033[36m[stop]\033[0m %s\n' "$*"; }

# 端口还活着吗。**只认 HTTP 有响应**，不认"端口被占用"：
# TIME_WAIT 状态也会让 netstat 报占用，那时候服务其实已经停了，
# 而按占用去杀会去杀一个不存在的 PID。
port_alive() {
  curl -s -o /dev/null -m 2 "http://127.0.0.1:$1/" 2>/dev/null && return 0
  curl -s -o /dev/null -m 2 "http://127.0.0.1:$1/health" 2>/dev/null && return 0
  return 1
}

# 按 PID 文件杀。返回 0 表示"杀过"，不代表"杀干净了"。
kill_pidfile() {
  local pidfile="$1" name="$2"
  [ -f "$pidfile" ] || { say "$name：没有 PID 文件，跳过"; return 1; }
  local pid; pid="$(cat "$pidfile" 2>/dev/null)"
  if [ -z "$pid" ]; then
    say "$name：PID 文件是空的，跳过"; rm -f "$pidfile"; return 1
  fi
  if kill -0 "$pid" 2>/dev/null; then
    say "$name：kill $pid"
    kill "$pid" 2>/dev/null
    sleep 1
    kill -9 "$pid" 2>/dev/null
  else
    say "$name：PID $pid 早就不在了（PID 文件过期）"
  fi
  rm -f "$pidfile"
  return 0
}

# 按端口找出真正的占用者并杀掉。这是 Windows 上唯一可靠的路径。
kill_by_port() {
  local port="$1" name="$2" pids=""

  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
      # netstat 的最后一列是 **Windows** 的 PID，不是 MSYS 的。
      pids="$(netstat -ano 2>/dev/null | grep -E "LISTENING" | grep -E "[:.]$port[[:space:]]" | awk '{print $NF}' | sort -u)"
      if [ -z "$pids" ]; then
        say "$name：netstat 说端口 $port 上没有 LISTENING 的进程"
        return 1
      fi
      for pid in $pids; do
        say "$name：端口 $port 被 Windows PID $pid 占着，taskkill"
        taskkill //F //PID "$pid" >/dev/null 2>&1 \
          || say "$name：taskkill $pid 失败（可能已经退了）"
      done
      ;;
    *)
      if command -v lsof >/dev/null 2>&1; then
        pids="$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null)"
      fi
      if [ -z "$pids" ]; then
        say "$name：找不到占用端口 $port 的进程（没装 lsof？）"
        return 1
      fi
      for pid in $pids; do
        say "$name：端口 $port 被 PID $pid 占着，kill"
        kill "$pid" 2>/dev/null; sleep 1; kill -9 "$pid" 2>/dev/null
      done
      ;;
  esac
  return 0
}

# 一个服务：先按 PID 杀，再回头探端口，还在就按端口杀，最后如实报告。
stop_one() {
  local port="$1" name="$2" pidfile="$PID_DIR/$3"

  if ! port_alive "$port"; then
    say "$name：端口 $port 上没有服务在跑"
    rm -f "$pidfile"
    return 0
  fi

  kill_pidfile "$pidfile" "$name"
  sleep 1

  if port_alive "$port"; then
    say "$name：PID 文件没解决问题，改按端口找"
    kill_by_port "$port" "$name"
    sleep 1
  fi

  if port_alive "$port"; then
    say "$name：**端口 $port 仍然有响应**，没能停掉。"
    say "     手动看一眼：netstat -ano | grep :$port"
    return 1
  fi

  say "$name：已停止（端口 $port 无响应）"
  return 0
}

say "停 xm3 的服务（后端 $BACKEND_PORT / 前端 $FRONTEND_PORT）"
rc=0
stop_one "$FRONTEND_PORT" "前端" "frontend.pid" || rc=1
stop_one "$BACKEND_PORT"  "后端" "backend.pid"  || rc=1

# `.dev/` 本身**不删**：里面的 backend.log 是刚才那次运行的现场，
# 服务起不来的时候要回头看它。它已经在 .gitignore 里，留着不碍事。

[ "$rc" = "0" ] && say "都停了" || say "有服务没停掉，见上面的提示"
exit "$rc"
