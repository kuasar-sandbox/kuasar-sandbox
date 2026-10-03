#!/usr/bin/env bash
# The documented Demo against prepared products, image and Python SDK.
set -euo pipefail
umask 077
source "${E2E_LIB:?}/common.sh"
require_root
require_kvm
for tool in python3 docker curl openssl flock timeout setsid stat; do require_command "$tool"; done
for product in node-ctl cluster-ctl e2b-key-ctl sandbox-ctl sandbox-init \
    cloud-hypervisor flatten-ctl manifest-ctl store-ctl cache-ctl connector-ctl; do
    require_binary "$product"
done
: "${WORK:?}" "${OUT:?}" "${E2E_WORKSPACE:?}" "${E2E_IMAGE:?prepared image is required}"
: "${ZOT_BIN:?prepared registry is required}" "${VGW_BIN:?prepared object gateway is required}"
[[ "$E2E_IMAGE" == sha256:* ]] || e2e_fail 'Demo requires a prepared image content ID'
[[ -x "$ZOT_BIN" && -x "$VGW_BIN" ]] || e2e_fail 'missing prepared Demo servers'
DEMO_DIR="$E2E_WORKSPACE/test/demo"
SDK="$E2E_WORKSPACE/fixtures/demo-sdk"
for input in demo_common.sh demo_prep.sh demo_e2b.sh prepared.py requirements.txt; do
    [[ -f "$DEMO_DIR/$input" ]] || e2e_fail "missing prepared Demo input: $input"
done
[[ -d "$SDK/e2b" ]] || e2e_fail 'missing prepared Demo SDK'
docker image inspect "$E2E_IMAGE" >"$OUT/image.json"

# The user workflow and the full product case use the same isolated SDK adapter.
# The public case always exercises the full assertions, regardless of the caller.
unset DEMO_QUICKSTART DEMO_NETDIAG DEMO_PAUSE
PYTHON_BIN="$WORK/demo-python"
python3 -B "$DEMO_DIR/prepared.py" python --workdir "$E2E_WORKSPACE" --output "$PYTHON_BIN"

key=$(tr -d '-' </proc/sys/kernel/random/uuid)
DEMO_DATA_DIR="/var/lib/kuasar-demo-ci-${key:0:10}"
CONFLICT_DATA_DIR="${DEMO_DATA_DIR}-conflict"
LISTENER_PID=''
export BIN E2E_IMAGE ZOT_BIN VGW_BIN PYTHON_BIN PYTHONDONTWRITEBYTECODE=1 KUASAR_ARTIFACT_E2E=1
cleanup() {
    local status=$? failed=0 directory
    trap - EXIT INT TERM
    set +e
    if [[ -n "$LISTENER_PID" ]]; then
        kill "$LISTENER_PID" 2>/dev/null
        wait "$LISTENER_PID" 2>/dev/null
    fi
    for directory in "$CONFLICT_DATA_DIR" "$DEMO_DATA_DIR"; do
        if [[ -f "$directory/.kuasar-demo-owner" ]]; then
            DEMO_DATA_DIR="$directory" bash "$DEMO_DIR/demo_prep.sh" reset || failed=1
        fi
    done
    if [[ "$status" == 0 && "$failed" != 0 ]]; then status=1; fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# A foreign listener remains alive when preparation rejects its occupied port.
python3 - "$WORK/foreign-port" <<'PY' &
import pathlib, socket, sys, time
sock = socket.socket()
sock.bind(('127.0.0.1', 0))
sock.listen()
pathlib.Path(sys.argv[1]).write_text(str(sock.getsockname()[1]))
while True:
    time.sleep(60)
PY
LISTENER_PID=$!
for _ in $(seq 1 50); do [[ -s "$WORK/foreign-port" ]] && break; sleep 0.1; done
[[ -s "$WORK/foreign-port" ]] || e2e_fail 'foreign listener did not become ready'
if DEMO_DATA_DIR="$CONFLICT_DATA_DIR" ZOT_PORT="$(cat "$WORK/foreign-port")" \
    bash "$DEMO_DIR/demo_prep.sh" >"$OUT/foreign-listener.log" 2>&1; then
    e2e_fail 'Demo preparation adopted a foreign listener'
fi
assert_contains "$OUT/foreign-listener.log" 'already in use by a process not owned by this Demo'
kill -0 "$LISTENER_PID" || e2e_fail 'Demo preparation signalled a foreign listener'
kill "$LISTENER_PID"
wait "$LISTENER_PID" 2>/dev/null || true
LISTENER_PID=''
DEMO_DATA_DIR="$CONFLICT_DATA_DIR" bash "$DEMO_DIR/demo_prep.sh" reset

# Idempotent preparation retains private, root-owned durable state.
export DEMO_DATA_DIR
bash "$DEMO_DIR/demo_prep.sh"
bash "$DEMO_DIR/demo_prep.sh"
[[ "$(stat -c '%u:%a' "$DEMO_DATA_DIR")" == 0:700 ]] || e2e_fail 'Demo directory permissions changed'
[[ "$(stat -c '%u:%a' "$DEMO_DATA_DIR/prep.env")" == 0:600 ]] || e2e_fail 'Demo state permissions changed'

# This user entry point performs real Build/COPY, the Quick Start lifecycle,
# SDK exec/files, snapshot template fan-out and migration with data assertions.
bash "$DEMO_DIR/demo_e2b.sh"

# Stop/start preserves durable preparation; cleanup resets only owned state.
bash "$DEMO_DIR/demo_prep.sh" stop
bash "$DEMO_DIR/demo_prep.sh"
echo 'PASS: documented Demo, repeated preparation and owned cleanup'
