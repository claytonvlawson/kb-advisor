#!/bin/sh
# Readiness gate for kb-advisor.service (ExecStartPost).
#
# systemd cannot know when a Type=exec service is actually serving, and this app
# has no sd_notify, so the unit polls /health itself. Without it the unit reports
# "started" while the index is still loading (~5 s for 8k chunks) and a restart
# looks successful even when the vault or index is missing.
#
# Exits 0 once /health answers 200, non-zero after the timeout so the start is
# marked failed and the restart policy takes over.
#
# Uses python3 rather than curl: the deploy host is not guaranteed to have curl.
set -eu

PORT="${ADVISOR_PORT:-8001}"
HOST="${ADVISOR_HEALTH_HOST:-127.0.0.1}"
TRIES="${ADVISOR_HEALTH_TRIES:-40}"
SLEEP_S="${ADVISOR_HEALTH_SLEEP:-0.5}"

exec /usr/bin/python3 - "$HOST" "$PORT" "$TRIES" "$SLEEP_S" <<'PY'
import sys
import time
import urllib.request

host, port, tries, sleep_s = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
url = "http://%s:%s/health" % (host, port)
deadline = time.monotonic() + tries * sleep_s

while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            if resp.status == 200:
                sys.exit(0)
    except Exception:
        pass
    time.sleep(sleep_s)

print("kb-advisor did not answer %s within %.1fs" % (url, tries * sleep_s),
      file=sys.stderr)
sys.exit(1)
PY
