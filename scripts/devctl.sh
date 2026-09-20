#!/usr/bin/env bash
set -euo pipefail

# Single-machine development process controller.
# It owns only the PIDs recorded under .dev/ and never uses a broad pkill.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${DEV_RUNTIME_DIR:-${ROOT_DIR}/.dev}"
PID_DIR="${RUNTIME_DIR}/pids"
LOG_DIR="${RUNTIME_DIR}/logs"
FRONT_PID_FILE="${PID_DIR}/frontend.pid"
BACK_PID_FILE="${PID_DIR}/backend.pid"
HOST="${CERT_HOST:-${DEV_HOST:-0.0.0.0}}"
PORT="${CERT_PORT:-${DEV_PORT:-8080}}"

mkdir -p "$PID_DIR" "$LOG_DIR"
touch "$LOG_DIR/frontend.log" "$LOG_DIR/backend.log"

say() { printf '[devctl] %s\n' "$*"; }
pid_from() { [ -f "$1" ] || return 1; tr -d '[:space:]' <"$1"; }
running() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
command_line() { ps -p "$1" -o command= 2>/dev/null || true; }

expected_process() {
  local pid="$1" kind="$2" command
  running "$pid" || return 1
  command="$(command_line "$pid")"
  case "$kind" in
    frontend) [[ "$command" == *"scripts/build.ts"* || "$command" == *"bun --watch"* ]] ;;
    backend) [[ "$command" == *"server.py"* ]] ;;
  esac
}

clear_stale() {
  local file kind pid
  for file in "$FRONT_PID_FILE" "$BACK_PID_FILE"; do
    [ -f "$file" ] || continue
    pid="$(pid_from "$file" || true)"
    kind=backend; [ "$file" = "$FRONT_PID_FILE" ] && kind=frontend
    if ! expected_process "$pid" "$kind"; then
      rm -f "$file"
    fi
  done
}

build_frontend() {
  say "building frontend with Bun"
  (cd "$ROOT_DIR" && bun run build >>"$LOG_DIR/frontend.log" 2>&1)
}

spawn_frontend() {
  local pid
  say "starting Bun frontend watcher"
  (
    cd "$ROOT_DIR"
    exec nohup bun --watch scripts/build.ts >>"$LOG_DIR/frontend.log" 2>&1 </dev/null
  ) &
  echo $! >"$FRONT_PID_FILE"
  pid="$(pid_from "$FRONT_PID_FILE")"
  say "frontend watcher started (pid $pid)"
}

spawn_backend() {
  local pid
  say "starting Python API on ${HOST}:${PORT}"
  (
    cd "$ROOT_DIR"
    exec nohup python3 server.py --host "$HOST" --port "$PORT" >>"$LOG_DIR/backend.log" 2>&1 </dev/null
  ) &
  echo $! >"$BACK_PID_FILE"
  pid="$(pid_from "$BACK_PID_FILE")"
  say "backend started (pid $pid)"
}

stop_one() {
  local file="$1" kind="$2" pid
  [ -f "$file" ] || return 0
  pid="$(pid_from "$file" || true)"
  if expected_process "$pid" "$kind"; then
    say "stopping $kind (pid $pid)"
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do
      running "$pid" || break
      sleep 0.1
    done
    if running "$pid"; then
      say "$kind did not stop gracefully; sending SIGKILL"
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$file"
}

status() {
  clear_stale
  local front='stopped' back='stopped' pid
  if pid="$(pid_from "$FRONT_PID_FILE" || true)"; then expected_process "$pid" frontend && front="running (pid $pid)"; fi
  if pid="$(pid_from "$BACK_PID_FILE" || true)"; then expected_process "$pid" backend && back="running (pid $pid)"; fi
  say "frontend: $front"
  say "backend:  $back (http://${HOST}:${PORT})"
  say "logs:     $LOG_DIR"
}

start() {
  clear_stale
  build_frontend
  [ -f "$FRONT_PID_FILE" ] || spawn_frontend
  [ -f "$BACK_PID_FILE" ] || spawn_backend
  sleep 0.25
  status
}

stop() {
  stop_one "$BACK_PID_FILE" backend
  stop_one "$FRONT_PID_FILE" frontend
  say "all development processes stopped"
}

reload() {
  clear_stale
  build_frontend
  stop_one "$BACK_PID_FILE" backend
  spawn_backend
  [ -f "$FRONT_PID_FILE" ] || spawn_frontend
  sleep 0.25
  status
}

logs() {
  # Keep `dev:logs` useful before the first start as well.
  touch "$LOG_DIR/frontend.log" "$LOG_DIR/backend.log"
  tail -n "${DEV_LOG_LINES:-80}" -f "$LOG_DIR/frontend.log" "$LOG_DIR/backend.log"
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  reload) reload ;;
  status) status ;;
  logs) logs ;;
  *) echo "Usage: $0 {start|stop|restart|reload|status|logs}" >&2; exit 2 ;;
esac
