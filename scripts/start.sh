#!/usr/bin/env bash
# 一键启动 / 停止本地开发环境（redis + worker + beat + api）。
#
# 用法：
#   ./scripts/start.sh up      # 后台起全部，日志在 scripts/.logs/
#   ./scripts/start.sh down    # 杀掉全部
#   ./scripts/start.sh status  # 查看进程状态
#
# 需要已安装：项目依赖（uvicorn/celery）与 redis-server。
#   conda install -n <env> -c conda-forge redis-server
#   # 或 brew install redis
#
# Playwright 在 macOS 上必须用 --pool=solo（prefork 子进程不能继承浏览器实例）。
#
# 可用环境变量覆盖：PYTHON（解释器路径）、REDIS_BIN（redis-server 路径）。

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"
REDIS_BIN="${REDIS_BIN:-$(command -v redis-server || true)}"
if [[ -z "$PYTHON" ]]; then
  echo "ERROR: python3 not found；可用 PYTHON=/path/to/python 指定解释器"
  exit 1
fi
export NO_PROXY="${NO_PROXY:-*}" no_proxy="${no_proxy:-*}"

LOG_DIR="$ROOT/scripts/.logs"
RUN_DIR="$ROOT/scripts/.run"
mkdir -p "$LOG_DIR" "$RUN_DIR"

_pid() { cat "$RUN_DIR/$1.pid" 2>/dev/null || true; }

_is_alive() {
  local pid=$(_pid "$1")
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

# redis-cli 与 redis-server 同目录；PATH 里通常没有，需从 REDIS_BIN 推导
_redis_cli() {
  if [[ -n "${REDIS_BIN:-}" && -x "$REDIS_BIN" ]]; then
    local dir
    dir="$(dirname "$REDIS_BIN")"
    if [[ -x "$dir/redis-cli" ]]; then
      echo "$dir/redis-cli"
      return 0
    fi
  fi
  command -v redis-cli 2>/dev/null || true
}

_redis_ping() {
  local cli
  cli="$(_redis_cli)"
  [[ -n "$cli" ]] || return 1
  "$cli" -p 6379 ping 2>/dev/null | grep -q PONG
}

# 按端口查真正在监听的进程（pid 文件可能与现实脱节）
# lsof 无匹配时返回 1，需吞掉以免 set -e 中断
_port_pid() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1 || true
}

# ------------------------------------------------------------------- up

_do_up() {
  # 1. redis —— 用端口探测判断，而不是只看 pid 文件
  if _redis_ping; then
    local real_pid
    real_pid="$(_port_pid 6379)"
    echo "[redis] already listening on 6379 (pid ${real_pid:-?})"
    if [[ -n "$real_pid" && "$real_pid" != "$(_pid redis)" ]]; then
      echo "        ⚠️ 该实例不是本脚本启动的；down 时会按端口一并关闭"
      echo "$real_pid" > "$RUN_DIR/redis.pid"
    fi
  else
    if [[ -z "$REDIS_BIN" || ! -x "$REDIS_BIN" ]]; then
      echo "ERROR: redis-server not found${REDIS_BIN:+ at $REDIS_BIN}"
      echo "       conda install -n <env> -c conda-forge redis-server"
      echo "       或用 REDIS_BIN=/path/to/redis-server ./scripts/start.sh up"
      exit 1
    fi
    $REDIS_BIN --daemonize no --port 6379 --dir "$RUN_DIR" --logfile "$LOG_DIR/redis.log" \
      >/dev/null 2>&1 &
    local rpid=$!
    echo "$rpid" > "$RUN_DIR/redis.pid"
    local ok=0
    for _ in $(seq 1 20); do
      if _redis_ping; then ok=1; break; fi
      sleep 0.5
    done
    if [[ "$ok" -ne 1 ]]; then
      echo "ERROR: redis 启动失败（pid $rpid），最近 5 行日志："
      tail -5 "$LOG_DIR/redis.log" 2>/dev/null | sed 's/^/       /'
      exit 1
    fi
    echo "[redis] started (pid $rpid)"
  fi

  # 2. api
  if _is_alive api; then
    echo "[api] already running (pid $(_pid api))"
  else
    nohup $PYTHON -m uvicorn competitor_intel.adapters.http.api:app --host 127.0.0.1 --port 8000 \
      > "$LOG_DIR/api.log" 2>&1 &
    echo $! > "$RUN_DIR/api.pid"
    echo "[api] started (pid $!)"
  fi

  # 3. worker（solo pool 避免 macOS Playwright 子进程问题）
  if _is_alive worker; then
    echo "[worker] already running (pid $(_pid worker))"
  else
    nohup $PYTHON -m celery -A competitor_intel.adapters.schedule.tasks:celery_app worker \
      --loglevel=INFO --pool=solo --concurrency=1 \
      > "$LOG_DIR/worker.log" 2>&1 &
    echo $! > "$RUN_DIR/worker.pid"
    echo "[worker] started (pid $!)"
  fi

  # 4. beat（定时触发 daily_run）
  if _is_alive beat; then
    echo "[beat] already running (pid $(_pid beat))"
  else
    nohup $PYTHON -m celery -A competitor_intel.adapters.schedule.tasks:celery_app beat --loglevel=INFO \
      > "$LOG_DIR/beat.log" 2>&1 &
    echo $! > "$RUN_DIR/beat.pid"
    echo "[beat] started (pid $!)"
  fi

  # 等 api 就绪
  for i in $(seq 1 20); do
    if curl -s -m 2 http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
      echo ""
      echo "✅ 全部就绪：API http://127.0.0.1:8000"
      echo "   POST /runs               手动触发"
      echo "   GET  /runs/{id}          查询状态"
      echo "   GET  /reports/latest     报告"
      echo ""
      echo "日志: $LOG_DIR/"
      exit 0
    fi
    sleep 1
  done
  echo "⚠️ API 启动超时，日志见 $LOG_DIR/api.log"
}

# ------------------------------------------------------------------- down

_do_down() {
  for name in beat worker api redis; do
    local pid=$(_pid "$name")
    if _is_alive "$name"; then
      echo "stopping $name (pid $pid)"
      kill "$pid" 2>/dev/null || true
      sleep 0.5
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$RUN_DIR/$name.pid"
  done
  # redis：pid 可能已失效或指向外部实例，统一按端口兜底
  local rcli
  rcli="$(_redis_cli)"
  if [[ -n "$rcli" ]]; then
    "$rcli" -p 6379 shutdown nosave 2>/dev/null || true
  fi
  local rpid
  rpid="$(_port_pid 6379)"
  if [[ -n "$rpid" ]]; then
    echo "stopping redis (pid $rpid, by port)"
    kill "$rpid" 2>/dev/null || true
    sleep 0.5
    kill -9 "$rpid" 2>/dev/null || true
  fi
  # beat 调度文件
  rm -f "$ROOT/celerybeat-schedule.db" "$ROOT/celerybeat-schedule.db.bak"
  echo "全部停止"
}

# ------------------------------------------------------------------- status

_do_status() {
  echo "  NAME       PID       STATUS"
  echo "  ----       ---       ------"

  # redis 以端口探测为准：pid 文件可能指向已退出的进程
  local rpid
  rpid="$(_port_pid 6379)"
  if _redis_ping; then
    if [[ -n "$rpid" && "$rpid" == "$(_pid redis)" ]]; then
      printf "  %-10s %-9s ✅ managed\n" redis "$rpid"
    else
      printf "  %-10s %-9s ✅ external（非本脚本启动）\n" redis "${rpid:-?}"
    fi
  else
    printf "  %-10s %-9s ❌ 6379 无监听\n" redis "-"
  fi

  for name in api worker beat; do
    local pid=$(_pid "$name")
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      printf "  %-10s %-9s ✅\n" "$name" "$pid"
    else
      printf "  %-10s %-9s ❌\n" "$name" "${pid:--}"
    fi
  done

  # 进程活着不代表能干活：broker 断了 worker/beat 会一直重试
  if ! _redis_ping; then
    echo ""
    echo "  ⚠️ broker 不可达（redis 未运行）：worker/beat 会反复 Connection refused，"
    echo "     POST /runs 仍返回 202，但任务无法派发"
    echo "     解决：./scripts/start.sh down && ./scripts/start.sh up"
  fi
}

# ------------------------------------------------------------------- main

case "${1:-up}" in
  up|start)   _do_up ;;
  down|stop)  _do_down ;;
  status|ps)  _do_status ;;
  restart)    _do_down; _do_up ;;
  *) echo "usage: $0 {up|down|status|restart}"; exit 1 ;;
esac
