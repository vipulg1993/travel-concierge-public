#!/usr/bin/env bash
# dev-local-stop.sh — tear down the processes started by dev-local.sh.
#
# Reads every .local-logs/*.pid, sends SIGTERM, waits briefly, then SIGKILL
# anything still breathing. Also sweeps port-bound uvicorn/streamlit stragglers
# so a stuck process doesn't block the next `dev-local.sh` run.

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_ROOT/.local-logs"

PORTS=(8080 8081 8082 8083 8084 8085)

kill_pid() {
  local pid="$1" sig="${2:-TERM}"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "-$sig" "$pid" 2>/dev/null || true
  fi
}

# 1) PID files first (clean path)
if [ -d "$LOG_DIR" ]; then
  for pidfile in "$LOG_DIR"/*.pid; do
    [ -e "$pidfile" ] || continue
    pid=$(cat "$pidfile" 2>/dev/null || true)
    name=$(basename "$pidfile" .pid)
    if [ -n "$pid" ]; then
      echo "  SIGTERM $name (pid $pid)"
      kill_pid "$pid" TERM
    fi
  done

  # Wait up to 5s for graceful exit
  for _ in $(seq 1 10); do
    any_alive=0
    for pidfile in "$LOG_DIR"/*.pid; do
      [ -e "$pidfile" ] || continue
      pid=$(cat "$pidfile" 2>/dev/null || true)
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        any_alive=1
      fi
    done
    if [ "$any_alive" = 0 ]; then break; fi
    sleep 0.5
  done

  # Hard kill anything still running
  for pidfile in "$LOG_DIR"/*.pid; do
    [ -e "$pidfile" ] || continue
    pid=$(cat "$pidfile" 2>/dev/null || true)
    name=$(basename "$pidfile" .pid)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "  SIGKILL $name (pid $pid)"
      kill_pid "$pid" KILL
    fi
    rm -f "$pidfile"
  done
fi

# 2) Any straggler bound to our known ports (e.g. a prior interactive run
# whose pidfile is gone)
for port in "${PORTS[@]}"; do
  while read -r pid; do
    [ -n "$pid" ] || continue
    cmd=$(ps -p "$pid" -o command= 2>/dev/null | head -c 120)
    echo "  SIGTERM straggler on :$port — pid $pid ($cmd)"
    kill_pid "$pid" TERM
  done < <(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null)
done

sleep 1

for port in "${PORTS[@]}"; do
  while read -r pid; do
    [ -n "$pid" ] || continue
    echo "  SIGKILL straggler on :$port — pid $pid"
    kill_pid "$pid" KILL
  done < <(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null)
done

echo "stopped."
