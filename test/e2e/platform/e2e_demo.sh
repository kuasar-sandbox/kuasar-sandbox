#!/usr/bin/env bash
#
# Exercise the documented Demo entry points on the real Integration E2E host.
# The first run covers the complete COPY, template fan-out, and migration flow;
# the second run covers the shorter Quick Start and repeated-use cleanup.

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
DEMO_DIR="$RELEASE_ROOT/test/demo"
BIN="${BIN:-$RELEASE_ROOT/bin}"
: "${ZOT_BIN:?ZOT_BIN must point to the platform-provided registry}"
: "${VGW_BIN:?VGW_BIN must point to the platform-provided object gateway}"

if [ "$(id -u)" -ne 0 ]; then
    exec sudo -nE "$0" "$@"
fi

for path in "$DEMO_DIR/demo_prep.sh" "$DEMO_DIR/demo_e2b.sh" \
    "$DEMO_DIR/requirements.txt"; do
    [ -f "$path" ] || {
        echo "missing Demo release input: $path" >&2
        exit 1
    }
done

RUN_KEY="$(tr -d '-' </proc/sys/kernel/random/uuid | cut -c1-10)"
DEMO_DATA_DIR="/var/lib/kuasar-demo-ci-$RUN_KEY"
CONFLICT_DATA_DIR="/var/lib/kuasar-demo-conflict-$RUN_KEY"
TMP_DIR="$(mktemp -d /tmp/kuasar-demo-e2e.XXXXXX)"
PYTHON_BIN=""
LISTENER_PID=""

cleanup() {
    local status=$? cleanup_failed=0
    trap - EXIT INT TERM
    set +e
    if [ -n "$LISTENER_PID" ] && kill -0 "$LISTENER_PID" 2>/dev/null; then
        kill "$LISTENER_PID" 2>/dev/null
        wait "$LISTENER_PID" 2>/dev/null
    fi
    if [ -f "$CONFLICT_DATA_DIR/.kuasar-demo-owner" ]; then
        DEMO_DATA_DIR="$CONFLICT_DATA_DIR" bash "$DEMO_DIR/demo_prep.sh" reset \
            || cleanup_failed=1
    fi
    if [ -f "$DEMO_DATA_DIR/.kuasar-demo-owner" ]; then
        DEMO_DATA_DIR="$DEMO_DATA_DIR" bash "$DEMO_DIR/demo_prep.sh" reset \
            || cleanup_failed=1
    fi
    case "$TMP_DIR" in /tmp/kuasar-demo-e2e.*) rm -rf -- "$TMP_DIR" ;; *) cleanup_failed=1 ;; esac
    if [ "$status" -eq 0 ] && [ "$cleanup_failed" -ne 0 ]; then status=1; fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# Hold a real foreign listener and prove preparation refuses it without
# signalling the process. The test owns and removes the listener afterwards.
python3 - "$TMP_DIR/foreign-port" <<'PY' &
import pathlib
import socket
import sys
import time

sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", 0))
sock.listen()
pathlib.Path(sys.argv[1]).write_text(str(sock.getsockname()[1]), encoding="ascii")
while True:
    time.sleep(60)
PY
LISTENER_PID=$!
for _ in $(seq 1 50); do [ -s "$TMP_DIR/foreign-port" ] && break; sleep 0.1; done
[ -s "$TMP_DIR/foreign-port" ] || {
    echo "foreign-listener fixture did not become ready" >&2
    exit 1
}
FOREIGN_PORT="$(<"$TMP_DIR/foreign-port")"
if DEMO_DATA_DIR="$CONFLICT_DATA_DIR" BIN="$BIN" ZOT_BIN="$ZOT_BIN" \
    VGW_BIN="$VGW_BIN" ZOT_PORT="$FOREIGN_PORT" \
    bash "$DEMO_DIR/demo_prep.sh"; then
    echo "Demo preparation adopted a foreign listener" >&2
    exit 1
fi
kill -0 "$LISTENER_PID" 2>/dev/null || {
    echo "Demo preparation signalled the foreign listener" >&2
    exit 1
}
kill "$LISTENER_PID"
wait "$LISTENER_PID" 2>/dev/null || true
LISTENER_PID=""
DEMO_DATA_DIR="$CONFLICT_DATA_DIR" bash "$DEMO_DIR/demo_prep.sh" reset

# Start the persistent tier twice to exercise idempotence, then use only the
# exact SDK requirement shipped beside these scripts.
DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" ZOT_BIN="$ZOT_BIN" VGW_BIN="$VGW_BIN" \
    bash "$DEMO_DIR/demo_prep.sh"
DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" ZOT_BIN="$ZOT_BIN" VGW_BIN="$VGW_BIN" \
    bash "$DEMO_DIR/demo_prep.sh"
test "$(stat -c '%u:%a' "$DEMO_DATA_DIR")" = "0:700"
test "$(stat -c '%u:%a' "$DEMO_DATA_DIR/prep.env")" = "0:600"

python3 -m venv "$DEMO_DATA_DIR/sdk-venv"
PYTHON_BIN="$DEMO_DATA_DIR/sdk-venv/bin/python3"
"$PYTHON_BIN" -m pip install --disable-pip-version-check \
    --requirement "$DEMO_DIR/requirements.txt"

DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" PYTHON_BIN="$PYTHON_BIN" \
    bash "$DEMO_DIR/demo_e2b.sh"
DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" PYTHON_BIN="$PYTHON_BIN" \
    DEMO_QUICKSTART=1 bash "$DEMO_DIR/demo_e2b.sh"

# Stop/start validates the durable preparation contract and stale-socket
# handling; the EXIT trap then performs the exact-marker reset.
DEMO_DATA_DIR="$DEMO_DATA_DIR" bash "$DEMO_DIR/demo_prep.sh" stop
DEMO_DATA_DIR="$DEMO_DATA_DIR" BIN="$BIN" ZOT_BIN="$ZOT_BIN" VGW_BIN="$VGW_BIN" \
    bash "$DEMO_DIR/demo_prep.sh"

echo "PASS: documented full Demo, repeated Quick Start, and owned cleanup"
