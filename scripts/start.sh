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

# ------------------------------------------------------------------- up

_do_up() {
  # 1. redis
  if _is_alive redis; then
    echo "[redis] already running (pid $(_pid redis))"
  else
    if [[ -z "$REDIS_BIN" || ! -x "$REDIS_BIN" ]]; then
      echo "ERROR: redis-server not found${REDIS_BIN:+ at $REDIS_BIN}"
      echo "       conda install -n <env> -c conda-forge redis-server"
      echo "       或用 REDIS_BIN=/path/to/redis-server ./scripts/start.sh up"
      exit 1
    fi
    $REDIS_BIN --daemonize no --port 6379 --dir "$RUN_DIR" --logfile "$LOG_DIR/redis.log" \
      >/dev/null 2>&1 &
    echo $! > "$RUN_DIR/redis.pid"
    echo "[redis] started (pid $!)"
    sleep 1
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
  # redis-server --daemonize no 被我们直接 fork 了，再保险杀一次按端口
  (command -v redis-cli &>/dev/null && redis-cli -p 6379 shutdown nosave 2>/dev/null) || true
  # beat 调度文件
  rm -f "$ROOT/celerybeat-schedule.db" "$ROOT/celerybeat-schedule.db.bak"
  echo "全部停止"
}

# ------------------------------------------------------------------- status

_do_status() {
  echo "  NAME       PID       STATUS"
  echo "  ----       ---       ------"
  for name in redis api worker beat; do
    local pid=$(_pid "$name")
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      printf "  %-10s %-9s ✅\n" "$name" "$pid"
    else
      printf "  %-10s %-9s ❌\n" "$name" "${pid:-"-"}"
    fi
  done
}

# ------------------------------------------------------------------- main

case "${1:-up}" in
  up|start)   _do_up ;;
  down|stop)  _do_down ;;
  status|ps)  _do_status ;;
  restart)    _do_down; _do_up ;;
  *) echo "usage: $0 {up|down|status|restart}"; exit 1 ;;
esac
