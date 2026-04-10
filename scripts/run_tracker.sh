#!/bin/zsh

if [ -z "${ZSH_VERSION:-}" ]; then
  exec /bin/zsh "$0" "$@"
fi

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

if [ -f ".env" ]; then
  set -a
  . ./.env
  set +a
fi

if [ -f ".venv/bin/activate" ]; then
  . ".venv/bin/activate"
  PYTHON_BIN="python"
else
  PYTHON_BIN="python3"
fi

WATCHLIST_PATH="$REPO_ROOT/watchlist.json"
MAX_RUN_ATTEMPTS="${TRACKER_MAX_RUN_ATTEMPTS:-2}"
RETRY_DELAY_SECONDS="${TRACKER_RETRY_DELAY_SECONDS:-600}"
NOTIFY_DESKTOP="${TRACKER_NOTIFY_DESKTOP:-1}"

notify_desktop() {
  local title="$1"
  local message="$2"

  if [ "$NOTIFY_DESKTOP" != "1" ]; then
    return 0
  fi

  if ! command -v osascript >/dev/null 2>&1; then
    return 0
  fi

  osascript - "$title" "$message" <<'APPLESCRIPT' >/dev/null 2>&1 || true
on run argv
  display notification (item 2 of argv) with title (item 1 of argv)
end run
APPLESCRIPT
}

log_has_dns_failure() {
  local log_path="$1"

  if [ ! -f "$log_path" ]; then
    return 1
  fi

  local pattern
  for pattern in \
    "Failed to resolve" \
    "NameResolutionError" \
    "nodename nor servname provided, or not known" \
    "Temporary failure in name resolution" \
    "Failed to send email: [Errno 8]"
  do
    if grep -Fqi "$pattern" "$log_path"; then
      return 0
    fi
  done

  return 1
}

watchlist_has_dns_unhealthy_reason() {
  WATCHLIST_PATH="$WATCHLIST_PATH" "$PYTHON_BIN" - <<'PY'
import json
import os
import pathlib
import sys

watchlist_path = pathlib.Path(os.environ["WATCHLIST_PATH"])
if not watchlist_path.exists():
    raise SystemExit(1)

try:
    payload = json.loads(watchlist_path.read_text())
except Exception:
    raise SystemExit(1)

run_health = payload.get("run_health") or {}
if run_health.get("status") != "unhealthy":
    raise SystemExit(1)

request_health = run_health.get("request_health") or {}
haystack = " ".join(
    [
        str(run_health.get("reason") or ""),
        str(request_health.get("last_error") or ""),
    ]
).lower()

dns_patterns = (
    "failed to resolve",
    "nameresolutionerror",
    "nodename nor servname provided, or not known",
    "temporary failure in name resolution",
)

raise SystemExit(0 if any(pattern in haystack for pattern in dns_patterns) else 1)
PY
}

should_retry_run() {
  local exit_code="$1"
  local log_path="$2"

  if [ "$exit_code" -eq 0 ]; then
    return 1
  fi

  if watchlist_has_dns_unhealthy_reason; then
    return 0
  fi

  if log_has_dns_failure "$log_path"; then
    return 0
  fi

  return 1
}

run_once() {
  local attempt="$1"
  local log_path="$2"

  echo "[runner] Attempt $attempt/$MAX_RUN_ATTEMPTS starting from $REPO_ROOT"

  set +e
  "$PYTHON_BIN" main.py 2>&1 | tee "$log_path"
  local exit_code=${pipestatus[1]}
  set -e

  echo "[runner] Attempt $attempt finished with exit code $exit_code"
  return "$exit_code"
}

attempt=1
while [ "$attempt" -le "$MAX_RUN_ATTEMPTS" ]; do
  attempt_log="$(mktemp "${TMPDIR:-/tmp}/polymarket-tracker-attempt-${attempt}.XXXXXX")"

  if run_once "$attempt" "$attempt_log"; then
    if [ "$attempt" -gt 1 ]; then
      notify_desktop "Polymarket Tracker" "Retry succeeded and the canonical run completed."
    fi
    rm -f "$attempt_log"
    exit 0
  fi

  if [ "$attempt" -lt "$MAX_RUN_ATTEMPTS" ] && should_retry_run 1 "$attempt_log"; then
    notify_desktop "Polymarket Tracker" "Run hit a DNS-style failure. Retrying once in ${RETRY_DELAY_SECONDS}s."
    echo "[runner] DNS-style failure detected. Retrying once in ${RETRY_DELAY_SECONDS} seconds..."
    rm -f "$attempt_log"
    sleep "$RETRY_DELAY_SECONDS"
    attempt=$((attempt + 1))
    continue
  fi

  notify_desktop "Polymarket Tracker" "Canonical run failed. Check watchlist.json and report.html in the repo."
  rm -f "$attempt_log"
  exit 1
done
