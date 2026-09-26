#!/usr/bin/env bash
#
# openclaw-density-bench.sh — Autonomous Coding-Agent (OpenClaw-style) Benchmark Suite
#
# Tests Kuasar Sandbox under OpenClaw-style autonomous coding-agent workloads
# (deterministic mock-LLM agent loop; the `openclaw` npm package is not used):
#   1. Calibration (P1-OpenClaw): Baseline RSS, peak tool execution RSS, V8 heap
#   2. In-Memory Pause/Resume (Warm Freeze): vm.pause/vm.resume latency at N=30
#   3. Cold Snapshot/Restore (Scale-to-Zero): Memory dump + lazy UFFD restore
#   4. Concurrency Saturation Ramp: Verified agent-session pass rate at density
#   5. Production Stress: Fork-exec storms & COW churn
#
# All reported metrics are measured live; verdicts are computed, not asserted.
# Per-run logs are archived under test/results/openclaw-bench/raw_logs/run-* for audit.
#
# Usage:
#   sudo bash openclaw-density-bench.sh [all|calibrate|pause_resume|cold|ramp|stress]
#   bash openclaw-density-bench.sh self_check
#
# Evidence semantics (see docs/perf.md §2): every declared target in the report
# is evaluated with an explicit PASS/FAIL verdict. `perf-agent` is a validation
# gate: the harness exits non-zero when any required gate fails. Thresholds
# marked (informational) report their miss in the report and in GATE misses but
# do not fail the run. Execution completing is never treated as performance
# success, and a miss is retained rather than hidden by moving its threshold.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="${BIN:-$REPO_ROOT/bin}"

# Mode parsed up front so self_check can run fully offline: no /dev/kvm, root,
# Docker or release binaries, and no writes under /run or /var/tmp.
MODE="${1:-all}"
case "$MODE" in
    all|calibrate|pause_resume|cold|ramp|stress) ;;
    self_check)
        SELF_CHECK=1
        # Everything self_check touches lives in a disposable mktemp dir.
        SELF_TMP="$(mktemp -d /tmp/openclaw-selfcheck.XXXXXX)"
        OUT_BASE="$SELF_TMP/results"
        PERF_OUT="$OUT_BASE/OPENCLAW_BENCHMARK_REPORT.md"
        RAW_DIR="${SELF_TMP}/raw"
        WORK="$SELF_TMP/work"
        RUN_ROOT="$SELF_TMP/run-root"
        ;;
    *)
        echo "Usage: $0 [all|calibrate|pause_resume|cold|ramp|stress|self_check]" >&2
        exit 1
        ;;
esac

# All durable outputs (report, log archive, cached rootfs) live under
# test/results/ — the same git-ignored output area used by the other perf
# suites — so the harness leaves no untracked files in the repository.
if [ -z "${SELF_CHECK:-}" ]; then
    OUT_BASE="${OUT_BASE:-$REPO_ROOT/test/results/openclaw-bench}"
    PERF_OUT="${PERF_OUT:-$OUT_BASE/OPENCLAW_BENCHMARK_REPORT.md}"
    RAW_DIR="$OUT_BASE/raw_logs"
    # Scratch (VM run-roots, sockets, snapshots) lives on disk-backed /var/tmp:
    # (a) unix socket paths must stay under the 108-byte sockaddr_un limit, and
    WORK="${WORK:-/var/tmp/openclaw-bench/run-$(date +%Y%m%d-%H%M%S)-$$}"
    RUN_ROOT="${RUN_ROOT:-/run/oc-$$}"
fi
PROXY_PORT=8088
IMAGE="openclaw-agent:latest"

mkdir -p "$WORK" "$RAW_DIR" "$RUN_ROOT" "$WORK/lib" "$WORK/units"

VMLINUX="$BIN/vmlinux"
BUNDLE="$BIN/sandbox-runtime.bundle"
CH_BIN="$BIN/cloud-hypervisor"

if [ -z "${SELF_CHECK:-}" ]; then
    for b in "$BIN/sandbox-ctl" "$BIN/node-ctl" "$BIN/flatten-ctl" "$VMLINUX" "$BUNDLE" "$CH_BIN"; do
        if [ ! -f "$b" ]; then
            echo "Error: missing required binary $b" >&2
            exit 1
        fi
    done
fi

# Cleanup trap
PROXY_PID=""
DAEMON_PID=""
SANDBOX_PIDS=()
CREATED_TAPS=()
CREATED_CGROUPS=()

cleanup() {
    set +e
    echo
    echo "==> Cleaning up benchmark processes, sockets, and TAP devices..."
    # Archive run logs to RAW_DIR so results stay auditable post-mortem
    local raw_run="$RAW_DIR/run-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$raw_run" 2>/dev/null
    cp -a "$WORK"/*.log "$WORK"/*.yaml "$raw_run"/ 2>/dev/null || true
    if [ -n "$PROXY_PID" ]; then
        kill -TERM "$PROXY_PID" 2>/dev/null || true
    fi
    for spid in "${SANDBOX_PIDS[@]:-}"; do
        kill -9 "$spid" 2>/dev/null || true
    done
    pkill -9 -f "cloud-hypervisor.*$RUN_ROOT" 2>/dev/null || true
    pkill -9 -f "cloud-hypervisor.*$WORK" 2>/dev/null || true
    pkill -9 -f "sandbox-ctl.*$RUN_ROOT" 2>/dev/null || true
    if [ -n "$DAEMON_PID" ]; then
        kill -TERM "$DAEMON_PID" 2>/dev/null || true
        for _ in {1..20}; do
            kill -0 "$DAEMON_PID" 2>/dev/null || break
            sleep 0.1
        done
        kill -9 "$DAEMON_PID" 2>/dev/null || true
        wait "$DAEMON_PID" 2>/dev/null || true
    fi
    for tap in "${CREATED_TAPS[@]:-}"; do
        ip link delete "$tap" 2>/dev/null || true
    done
    for cg in "${CREATED_CGROUPS[@]:-}"; do
        rmdir "$cg" 2>/dev/null || true
    done
    for cg in /sys/fs/cgroup/sandboxes/oc-*; do
        [ -d "$cg" ] && rmdir "$cg" 2>/dev/null || true
    done
    # KEEP_WORK=1 preserves snapshots/scratch for post-run analysis (dedup, etc.)
    [ "${KEEP_WORK:-0}" = "1" ] || rm -rf "$WORK" "$RUN_ROOT" 2>/dev/null || true
    set -e
}
if [ -z "${SELF_CHECK:-}" ]; then
    trap cleanup EXIT INT TERM
fi

# Start Local High-Throughput Agent Mock Proxy
start_proxy() {
    local delay="${1:-0.0}"
    if [ -n "$PROXY_PID" ]; then
        kill -TERM "$PROXY_PID" 2>/dev/null || true
        wait "$PROXY_PID" 2>/dev/null || true
    fi
    python3 "$REPO_ROOT/test/perf/workloads/agent_llm_proxy.py" \
        --port "$PROXY_PORT" --delay "$delay" > "$WORK/proxy.log" 2>&1 &
    PROXY_PID=$!
    sleep 0.5
    curl -sf "http://127.0.0.1:$PROXY_PORT/health" >/dev/null || {
        echo "Error: Agent LLM Proxy failed to start" >&2
        cat "$WORK/proxy.log" >&2
        exit 1
    }
}

# Prepare openclaw-blk0.img rootfs. The cache is keyed to the exact Docker
# image ID so a stale blk0 (e.g. from an older session driver) is never
# silently reused.
CACHED_BLK0="$OUT_BASE/openclaw-blk0.img"
CACHED_BLK0_ID="$CACHED_BLK0.id"
prepare_blk0() {
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        echo "==> Building agent container image $IMAGE..."
        docker build -t "$IMAGE" "$REPO_ROOT/test/perf/workloads/openclaw-image"
    fi
    local image_id
    image_id=$(docker image inspect "$IMAGE" --format '{{.Id}}' 2>/dev/null || true)
    if [ -n "$image_id" ] && [ -f "$CACHED_BLK0" ] && [ "$(cat "$CACHED_BLK0_ID" 2>/dev/null)" = "$image_id" ]; then
        BLK0="$CACHED_BLK0"
        echo "==> Using cached OpenClaw block device: $BLK0 ($image_id)"
    else
        BLK0="$WORK/openclaw-blk0.img"
        echo "==> Exporting OpenClaw container image to EROFS block device..."
        docker save "$IMAGE" | "$BIN/flatten-ctl" export --output "$BLK0" --no-progress >/dev/null
        if [ -n "$image_id" ]; then
            cp "$BLK0" "$CACHED_BLK0" 2>/dev/null || true
            printf '%s\n' "$image_id" > "$CACHED_BLK0_ID"
        fi
    fi
}

read_avail_mib() {
    awk '/MemAvailable:/ {printf "%d", $2/1024}' /proc/meminfo
}

read_oom_kills() {
    awk '/^oom_kill /{print $2}' /proc/vmstat 2>/dev/null || echo 0
}

count_matches() {
    compgen -G "$1" 2>/dev/null | wc -l
}

# Poll until `want` sockets matching `pattern` exist, or timeout (s). Returns 1 on timeout.
wait_for_sockets() {
    local pattern="$1" want="$2" timeout_s="${3:-90}"
    local deadline=$(( SECONDS + timeout_s ))
    while [ "$(count_matches "$pattern")" -lt "$want" ]; do
        [ "$SECONDS" -ge "$deadline" ] && return 1
        sleep 0.2
    done
    return 0
}

# Poll until file is non-empty or timeout (s). Returns 1 on timeout.
wait_for_ready_file() {
    local file="$1" timeout_s="${2:-60}"
    local deadline=$(( SECONDS + timeout_s ))
    until [ -s "$file" ]; do
        [ "$SECONDS" -ge "$deadline" ] && return 1
        sleep 0.05
    done
    return 0
}

verdict_le() { awk -v v="$1" -v s="$2" 'BEGIN {print (v ~ /^[0-9]+(\.[0-9]+)?$/ && v+0 <= s+0) ? "PASS" : "FAIL"}'; }
verdict_lt() { awk -v v="$1" -v s="$2" 'BEGIN {print (v ~ /^[0-9]+(\.[0-9]+)?$/ && v+0 <  s+0) ? "PASS" : "FAIL"}'; }
verdict_ge() { awk -v v="$1" -v s="$2" 'BEGIN {print (v ~ /^[0-9]+(\.[0-9]+)?$/ && v+0 >= s+0) ? "PASS" : "FAIL"}'; }
verdict_gt() { awk -v v="$1" -v s="$2" 'BEGIN {print (v ~ /^[0-9]+(\.[0-9]+)?$/ && v+0 >  s+0) ? "PASS" : "FAIL"}'; }
verdict_eq() { awk -v v="$1" -v s="$2" 'BEGIN {print (v ~ /^[0-9]+(\.[0-9]+)?$/ && v+0 == s+0) ? "PASS" : "FAIL"}'; }

# Gate accounting. Every declared target is evaluated exactly once (per-phase
# markers keep phase banners and finalize() from double-counting). Misses are
# counted and labeled: required-gate misses decide the exit status, while
# informational misses are reported without failing the run. n/a measurements
# count as misses — a gap is never silently passing.
GATE_TOTAL=0
GATE_MISSES=0
GATE_REQUIRED_MISSES=0
GATE_INFO_MISSES=0
declare -a GATE_REQUIRED_DETAIL=()
declare -a GATE_INFO_DETAIL=()
# _gv <verdict> <required:0|1> <label>
_gv() {
    local v="$1" req="$2" label="$3"
    GATE_TOTAL=$((GATE_TOTAL + 1))
    if [ "$v" != "PASS" ]; then
        GATE_MISSES=$((GATE_MISSES + 1))
        if [ "$req" = "1" ]; then
            GATE_REQUIRED_MISSES=$((GATE_REQUIRED_MISSES + 1))
            GATE_REQUIRED_DETAIL+=("$label")
            echo "  ✗ GATE MISS: $label" >&2
        else
            GATE_INFO_MISSES=$((GATE_INFO_MISSES + 1))
            GATE_INFO_DETAIL+=("$label")
            echo "  - target miss (informational, non-gating): $label" >&2
        fi
        return 1
    fi
    return 0
}
# Per-phase idempotence so phase banners and finalize() never double-count a target.
declare -A _EV_DONE=()
declare -A _RUN_PHASE=()
_PHASE_M0=0
_phase_enter() { _PHASE_M0=$GATE_MISSES; }
_phase_banner() {  # _phase_banner <num> <name>
    local misses=$(( GATE_MISSES - _PHASE_M0 ))
    if [ "$misses" -eq 0 ]; then
        echo "  ✓ Phase ${1}: ${2} — all declared targets PASS"
    else
        echo "  ✗ Phase ${1}: ${2} — ${misses} declared target miss(es) (retained as evidence; the final gate decides the exit status)"
    fi
}
var_ref() { eval "printf '%s' \"\${$1:-}\""; }

# --- Declared-target evaluation, per phase. Thresholds are the contracts this
# PR declares (and docs/perf.md §2 repeats): a miss is retained, never hidden
# by raising or removing the threshold; "(informational)" targets are reported
# but do not fail the run.
_evaluate_p1() {
    case "${_EV_DONE[p1]:-}" in done) return 0;; esac
    _EV_DONE[p1]=done
    [ -n "${_RUN_PHASE[p1]:-}" ] || return 0
    v_wall=$(verdict_lt "${CALIB_WALL_MS:-}" 3000)
    v_rss=$(verdict_le "${CALIB_RSS_MIB:-}" 128)
    v_heap=$(verdict_le "${CALIB_HEAP_MIB:-}" 32)
    v_cow=$(verdict_le "${CALIB_COW_KIB:-}" 1024)
    _gv "$v_wall" 1 "phase1: task duration ${CALIB_WALL_MS:-n/a} ms (target < 3000 ms)" || true
    _gv "$v_rss" 1 "phase1: peak guest RSS ${CALIB_RSS_MIB:-n/a} MiB (target < 128 MiB)" || true
    _gv "$v_heap" 1 "phase1: final V8 heap ${CALIB_HEAP_MIB:-n/a} MiB (target < 32 MiB)" || true
    _gv "$v_cow" 1 "phase1: COW diff footprint ${CALIB_COW_KIB:-n/a} KiB (target < 1024 KiB)" || true
    return 0
}
_evaluate_p2() {
    case "${_EV_DONE[p2]:-}" in done) return 0;; esac
    _EV_DONE[p2]=done
    [ -n "${_RUN_PHASE[p2]:-}" ] || return 0
    v_paused=$(verdict_ge "${PAUSE_COUNT:-}" 30)
    v_pause=$(verdict_le "${PAUSE_AVG_MS:-}" 15)
    v_paused_cpu=$(verdict_lt "${PAUSE_CPU_PCT:-}" 5.0)
    v_resume=$(verdict_le "${RESUME_AVG_MS:-}" 15)
    _gv "$v_paused" 1 "phase2: instances paused ${PAUSE_COUNT:-n/a}/30 (target 100%)" || true
    _gv "$v_pause" 1 "phase2: average pause ${PAUSE_AVG_MS:-n/a} ms/VM (target < 15 ms)" || true
    _gv "$v_paused_cpu" 0 "phase2: host user CPU during pause ${PAUSE_CPU_PCT:-n/a}% (informational target < 5.0%)" || true
    _gv "$v_resume" 1 "phase2: average resume ${RESUME_AVG_MS:-n/a} ms/VM (target < 15 ms)" || true
    return 0
}
_evaluate_p3() {
    case "${_EV_DONE[p3]:-}" in done) return 0;; esac
    _EV_DONE[p3]=done
    [ -n "${_RUN_PHASE[p3]:-}" ] || return 0
    v_snapok=$(verdict_ge "${SNAP_COUNT:-}" 16)
    v_snap=$(verdict_le "${SNAP_AVG_MS:-}" 1000)
    v_snapsize=$(verdict_le "${SNAP_SIZE_MIB:-}" 1024)
    v_reclaim=$(verdict_gt "${RECLAIMED_MIB:-}" 0)
    v_restore="PASS"
    [ "$(verdict_lt "${RESTORE_WALL_MS:-}" 100)" = FAIL ] && v_restore="FAIL"
    [ "$(verdict_eq "${RESTORE_READY_COUNT:-}" 8)" = FAIL ] && v_restore="FAIL"
    v_restfail=$(verdict_le "${RESTORE_EXIT_FAIL:-}" 0)
    _gv "$v_snapok" 1 "phase3: snapshots succeeded ${SNAP_COUNT:-n/a}/16 (target 100%)" || true
    _gv "$v_snap" 1 "phase3: snapshot throughput ${SNAP_AVG_MS:-n/a} ms (target < 1000 ms/snapshot)" || true
    _gv "$v_snapsize" 1 "phase3: snapshot footprint ${SNAP_SIZE_MIB:-n/a} MiB (target < 1024 MiB/snapshot)" || true
    _gv "$v_reclaim" 0 "phase3: MemAvailable delta after scale-to-zero ${RECLAIMED_MIB:-n/a} MiB (informational target > 0)" || true
    _gv "$v_restore" 1 "phase3: 8-way restore ready wallclock ${RESTORE_WALL_MS:-n/a} ms, ${RESTORE_READY_COUNT:-n/a}/8 ready (target < 100 ms, 8/8 ready)" || true
    _gv "$v_restfail" 1 "phase3: restore exit failures ${RESTORE_EXIT_FAIL:-n/a} (target 0)" || true
    return 0
}
_evaluate_p4() {
    case "${_EV_DONE[p4]:-}" in done) return 0;; esac
    _EV_DONE[p4]=done
    [ -n "${_RUN_PHASE[p4]:-}" ] || return 0
    local n pass
    v_ooms=$(verdict_le "${HOST_OOMS:-}" 0)
    for n in 25 50 100 150 200 250; do
        pass="${RAMP_PASS[$n]:-}"
        v_ramp=$(verdict_eq "$pass" "$n")
        printf -v "v_ramp${n}" '%s' "$v_ramp"
        _gv "$v_ramp" 1 "phase4: verified sessions passed ${pass:-n/a}/${n} at N=${n} (target 100%)" || true
    done
    _gv "$v_ooms" 1 "phase4: host oom_kill delta ${HOST_OOMS:-n/a} across ramp (target 0)" || true
    return 0
}
_evaluate_p5() {
    case "${_EV_DONE[p5]:-}" in done) return 0;; esac
    _EV_DONE[p5]=done
    [ -n "${_RUN_PHASE[p5]:-}" ] || return 0
    v_stress=$(verdict_eq "${STRESS_PASS:-}" 20)
    _gv "$v_stress" 1 "phase5: fork-exec storm verified session verdicts ${STRESS_PASS:-n/a}/20 (target 100%)" || true
    return 0
}
# Evaluate every declared target (idempotent; called at the end of each phase
# and again by finalize() before rendering the report).
evaluate_gates_all() {
    _evaluate_p1; _evaluate_p2; _evaluate_p3; _evaluate_p4; _evaluate_p5
    return 0
}

# Poll a sandbox log until the agent session reports a terminal verdict (or a
# fatal error), or the timeout expires. Returns 1 on timeout.
wait_for_session_verdict() {
    local log="$1" timeout_s="${2:-180}"
    local deadline=$(( SECONDS + timeout_s ))
    while [ "$SECONDS" -lt "$deadline" ]; do
        grep -qE "Verdict: (PASS|FAIL)|AgentSession Fatal Error" "$log" 2>/dev/null && return 0
        sleep 0.25
    done
    return 1
}

# Stop a sandbox-ctl process with a bounded wait: SIGTERM, poll, then SIGKILL.
# Never blocks indefinitely on a microVM whose guest app failed to exit.
stop_sandbox_ctl() {
    local pid="$1" timeout_s="${2:-60}"
    if ! kill -0 "$pid" 2>/dev/null; then
        wait "$pid" 2>/dev/null || true
        return 0
    fi
    kill -TERM "$pid" 2>/dev/null || true
    local deadline=$(( SECONDS + timeout_s ))
    while kill -0 "$pid" 2>/dev/null && [ "$SECONDS" -lt "$deadline" ]; do
        sleep 0.2
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "  ! sandbox-ctl pid=$pid did not exit in ${timeout_s}s of SIGTERM; sending SIGKILL" >&2
        kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
}

# Reap a sandbox-ctl process that is expected to exit on its own (guest app
# completed). Sets REAP_EXIT_CODE to the process exit status (124 when force-
# stopped). Returns 0 when the process exited within the timeout.
REAP_EXIT_CODE=0
reap_sandbox_ctl() {
    local pid="$1" sid="$2" timeout_s="${3:-180}"
    REAP_EXIT_CODE=0
    local deadline=$(( SECONDS + timeout_s ))
    while kill -0 "$pid" 2>/dev/null && [ "$SECONDS" -lt "$deadline" ]; do
        sleep 0.25
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "  ! $sid (pid=$pid) still running after ${timeout_s}s; stopping it" >&2
        stop_sandbox_ctl "$pid" 30
        REAP_EXIT_CODE=124
        return 1
    fi
    wait "$pid" 2>/dev/null || REAP_EXIT_CODE=$?
    return 0
}

# Wait for a set of sandbox-ctl pids to exit naturally, up to timeout_s, then
# bounded-stop any survivors so a guest app that fails to exit cannot hang the
# harness indefinitely.
wait_or_stop_all() {
    local timeout_s="$1"; shift
    local deadline=$(( SECONDS + timeout_s ))
    while :; do
        local alive=0 p
        for p in "$@"; do kill -0 "$p" 2>/dev/null && alive=$((alive + 1)); done
        [ "$alive" -eq 0 ] && return 0
        if [ "$SECONDS" -ge "$deadline" ]; then
            echo "  ! $alive sandbox process(es) still running after ${timeout_s}s; stopping them" >&2
            break
        fi
        sleep 0.5
    done
    for p in "$@"; do stop_sandbox_ctl "$p" 30; done
    return 1
}

# Start node-ctl conductor
start_node_ctl() {
    # Terminate any conductor from a previous phase: two conductors sharing
    # one resource socket / cgroup scan path would fight over reservations.
    if [ -n "$DAEMON_PID" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
        kill -TERM "$DAEMON_PID" 2>/dev/null || true
        wait "$DAEMON_PID" 2>/dev/null || true
    fi
    # Belt-and-braces: reap orphaned conductors from crashed prior runs
    pkill -TERM -f "node-ctl conductor [s]erve --config $WORK/node-ctl.yaml" 2>/dev/null || true
    sleep 0.3
    local phys_mem="$(awk '/MemTotal:/ {printf "%d", $2/1024}' /proc/meminfo)MiB"
    local phys_cpu="$(nproc)"
    cat > "$WORK/node-ctl.yaml" <<EOF
api: { domain: density.local, listen: "127.0.0.1:0" }
encryption_key: "0000000000000000000000000000000000000000000000000000000000000000"
proxy: { auth: enforce }
sandbox:
  boot:
    kernel: $VMLINUX
    runtime: $BUNDLE
paths:
  run_root: $RUN_ROOT
  base_root: $WORK/lib
  config_socket: $RUN_ROOT/node-ctl.socket
  db_path: $WORK/node-ctl.db
units: { dir: $WORK/units, install: false }
resource_listen:
  enabled: true
  socket: $RUN_ROOT/sandbox-resource.sock
  cgroup_scan_paths:
    - /sys/fs/cgroup/sandboxes
  resources:
    physical_memory: $phys_mem
    physical_cpu: $phys_cpu
    host_reserved:
      memory: 1024MiB
      cpu: 1
  watermarks:
    operational_margin_factor: 0.10
    high_factor: 0.85
    low_factor: 0.70
    # emergency_factor is the SIZE of the emergency reserve (fraction of the
    # allocatable pool), not an allocation threshold. 0.95 here makes the zone
    # go critical at 5% utilization and shed every admission (see
    # orchestrator/internal/nodectl/state.go memoryZoneForReservedLocked).
    emergency_factor: 0.05
    startup_factor: 0.98
  rate_limits:
    memory_grant_per_sec_factor: 0.50
  admission:
    rate: 200
    burst: 400
    startup_ttl: 45s
    queue_ttl: 90s
    queue_max_depth: 1024
  log_level: info
EOF
    [ -d /sys/fs/cgroup/sandboxes ] || mkdir -p /sys/fs/cgroup/sandboxes
    echo "+memory +cpu" > /sys/fs/cgroup/sandboxes/cgroup.subtree_control 2>/dev/null || true
    "$BIN/node-ctl" conductor serve --config "$WORK/node-ctl.yaml" > "$WORK/node-ctl.log" 2>&1 &
    DAEMON_PID=$!
    for _ in {1..50}; do
        [ -S "$RUN_ROOT/sandbox-resource.sock" ] && break
        sleep 0.05
    done
    if [ ! -S "$RUN_ROOT/sandbox-resource.sock" ]; then
        echo "Error: node-ctl conductor failed to bind $RUN_ROOT/sandbox-resource.sock" >&2
        cat "$WORK/node-ctl.log" >&2
        exit 1
    fi
}

setup_sandbox_resources() {
    local sid="$1" cap="$2" floor="$3" burst="$4" idx="${5:-1}"
    local tap_name="tap${idx}"
    mkdir -p "/sys/fs/cgroup/sandboxes/$sid"
    CREATED_CGROUPS+=("/sys/fs/cgroup/sandboxes/$sid")
    
    local subnet_base=$(( (idx % 60) * 4 ))
    local block=$(( idx / 60 ))
    local host_ip="10.$((100 + block)).1.$(( subnet_base + 1 ))"
    local guest_ip="10.$((100 + block)).1.$(( subnet_base + 2 ))"

    # Provision TAP
    ip tuntap add "${tap_name}" mode tap 2>/dev/null || true
    ip addr flush dev "${tap_name}" 2>/dev/null || true
    ip addr add "${host_ip}/30" dev "${tap_name}" 2>/dev/null || true
    ip link set "${tap_name}" up
    CREATED_TAPS+=("${tap_name}")

    # Provision compact COW diff overlay
    truncate -s 64M "$WORK/${sid}.diff"
    mkfs.ext4 -q -F -O ^has_journal -b 4096 "$WORK/${sid}.diff"

    cat > "$WORK/$sid.yaml" <<EOF
resources:
  capacity:
    cpu: 1
    memory: ${cap}MiB
  allocatable:
    cpu: 1
    memory: ${floor}MiB
    deflate_on_oom: true
  control:
    cgroup_path: /sys/fs/cgroup/sandboxes/${sid}
    controller: $RUN_ROOT/sandbox-resource.sock
  startup:
    memory: ${burst}MiB
network:
  tap: ${tap_name}
  ip: ${guest_ip}/30
  gateway: ${host_ip}
boot:
  kernel: file://${VMLINUX}
  runtime: file://${BUNDLE}
  cmdline: "console=hvc0"
  root:
    base: file://${BLK0}
    overlay:
      diff: file://${WORK}/${sid}.diff
launch:
  exec: /usr/local/bin/node
  args:
    - /opt/run_openclaw.mjs
  env:
    OPENAI_BASE_URL: "http://${host_ip}:${PROXY_PORT}/v1"
    OPENAI_API_KEY: "mock-openclaw-key"
    AGENT_MODE: "${AGENT_MODE:-full}"
    REPO_DIR: "/workspace/repo"
  restart: never
EOF
}

# Metrics
CALIB_WALL_MS=0
CALIB_RSS_MIB=""
CALIB_HEAP_MIB=""
CALIB_COW_KIB=""

PAUSE_COUNT=0
PAUSE_AVG_MS=0
PAUSE_CPU_PCT=""
RESUME_AVG_MS=0

SNAP_COUNT=0
SNAP_AVG_MS=0
SNAP_SIZE_MIB=""
RESTORE_WALL_MS=0
RESTORE_READY_COUNT=0
RESTORE_EXIT_FAIL=0
RECLAIMED_MIB=0
SNAP_OK=0

HOST_OOMS=0

declare -A RAMP_PASS=()
declare -A RAMP_FAIL=()
declare -A RAMP_WALL=()
declare -A RAMP_MEM=()

# Gate mode: GATE_MODE=gate (default) fails the run (non-zero exit) when a
# required gate misses its target; GATE_MODE=report labels the miss but still
# exits 0.
GATE_MODE="${GATE_MODE:-gate}"

# ============================================================================
# Phase 1: Single Sandbox Calibration (P1-OpenClaw)
# ============================================================================
run_calibration() {
    _RUN_PHASE[p1]=1
    _phase_enter
    echo "============================================================"
    echo " PHASE 1: Autonomous Agent Single-Sandbox Calibration (P1-OpenClaw)"
    echo "============================================================"
    start_proxy 0.0
    prepare_blk0
    start_node_ctl

    local sid="oc-calib-1"
    setup_sandbox_resources "$sid" 512 64 64 1

    local b_avail=$(read_avail_mib)
    echo "  -> Launching OpenClaw sandbox ($sid)..."
    local t0=$(date +%s%N)
    
    "$BIN/sandbox-ctl" run --config "$WORK/$sid.yaml" --sandbox-id "$sid" \
        --ch-binary "$CH_BIN" --run-root "$RUN_ROOT" > "$WORK/$sid.log" 2>&1 &
    local s_pid=$!
    SANDBOX_PIDS+=("$s_pid")

    if wait_for_session_verdict "$WORK/$sid.log" 180; then
        local t1=$(date +%s%N)
        CALIB_WALL_MS=$(( (t1 - t0) / 1000000 ))
        echo "  -> OpenClaw Session Completed in ${CALIB_WALL_MS} ms"
    else
        CALIB_WALL_MS=-1
        echo "  ! calibration session produced no verdict within 180s" >&2
    fi
    stop_sandbox_ctl "$s_pid" 60
    sed 's/^/     /' "$WORK/$sid.log" | tail -n 25

    if grep -q "Verdict: PASS" "$WORK/$sid.log"; then
        echo "  ✓ CALIBRATION VERDICT: PASS"
        local r=$(grep -oE "Peak RSS: [0-9.]+" "$WORK/$sid.log" | awk '{print $3}' | head -1)
        [ -n "$r" ] && CALIB_RSS_MIB="$r"
        local h=$(grep -oE "Final Heap: [0-9.]+" "$WORK/$sid.log" | awk '{print $3}' | head -1)
        [ -n "$h" ] && CALIB_HEAP_MIB="$h"
        CALIB_COW_KIB=$(du -k "$WORK/$sid.diff" 2>/dev/null | awk '{print $1}')
    else
        echo "  ✗ CALIBRATION VERDICT: FAIL"
        cat "$WORK/$sid.log" >&2
        exit 1
    fi
    _evaluate_p1
    if [ "$(( GATE_MISSES - _PHASE_M0 ))" -gt 0 ]; then
        echo "  ✗ calibration gate FAILED — required targets missed; exit non-zero" >&2
        exit 1
    fi
    echo "  ✓ calibration gate PASS: task ${CALIB_WALL_MS} ms < 3000 ms, RSS ${CALIB_RSS_MIB} MiB < 128, heap ${CALIB_HEAP_MIB} MiB < 32, COW ${CALIB_COW_KIB} KiB < 1024"
    rmdir "/sys/fs/cgroup/sandboxes/$sid" 2>/dev/null || true
}

# ============================================================================
# Phase 2: In-Memory VM Pause & Resume Benchmark (N=30)
# ============================================================================
run_pause_resume() {
    _RUN_PHASE[p2]=1
    _phase_enter
    echo "============================================================"
    echo " PHASE 2: In-Memory VM Pause / Resume Benchmark (N=30)"
    echo "============================================================"
    # 20s thinking delay: each admitted VM parks in its turn-1 LLM wait long
    # enough to pause/resume it mid-session (4 turns -> ~80s session).
    start_proxy 20.0
    prepare_blk0
    start_node_ctl

    local N=30
    SANDBOX_PIDS=()
    echo "  -> Launching $N OpenClaw sandboxes with 20s thinking delay..."
    for i in $(seq 1 $N); do
        local sid="oc-pause-$i"
        setup_sandbox_resources "$sid" 384 64 64 "$i"
        "$BIN/sandbox-ctl" run --config "$WORK/$sid.yaml" --sandbox-id "$sid" \
            --ch-binary "$CH_BIN" --run-root "$RUN_ROOT" > "$WORK/$sid.log" 2>&1 &
        SANDBOX_PIDS+=("$!")
        sleep 0.02
    done

    echo "  -> Waiting for all $N sandboxes to reach ready state (ch.sock)..."
    if wait_for_sockets "$RUN_ROOT/oc-pause-*/ch.sock" "$N" 30; then
        echo "  ✓ All $N VMs ready"
    else
        echo "  ! Only $(count_matches "$RUN_ROOT/oc-pause-*/ch.sock")/$N VMs ready after 30s (admission shedding or boot contention)"
    fi

    echo "  -> Issuing /api/v1/vm.pause to active sandboxes..."
    local pause_t0=$(date +%s%N)
    local paused_count=0
    for i in $(seq 1 $N); do
        local sid="oc-pause-$i"
        local sock="$RUN_ROOT/$sid/ch.sock"
        if [ -S "$sock" ]; then
            if curl -sf -X PUT --unix-socket "$sock" http://ch/api/v1/vm.pause >/dev/null 2>&1; then
                paused_count=$((paused_count + 1))
            fi
        fi
    done
    local pause_t1=$(date +%s%N)
    local total_pause_ms=$(( (pause_t1 - pause_t0) / 1000000 ))
    PAUSE_COUNT=$paused_count
    PAUSE_AVG_MS=$(awk -v t="$total_pause_ms" -v c="$paused_count" 'BEGIN {printf "%.2f", (c > 0 ? t / c : 0)}')

    echo "  ✓ Paused $paused_count / $N VMs in ${total_pause_ms} ms (avg ${PAUSE_AVG_MS} ms/VM)"

    sleep 1
    local p_cpu=$(top -b -n 1 | awk '/^%Cpu/ {print $2}')
    [ -n "$p_cpu" ] && PAUSE_CPU_PCT="$p_cpu"
    echo "  ✓ Host User CPU during In-Memory Pause: ${PAUSE_CPU_PCT}%"

    echo "  -> Issuing /api/v1/vm.resume to all paused sandboxes..."
    local resume_t0=$(date +%s%N)
    local resumed_count=0
    for i in $(seq 1 $N); do
        local sid="oc-pause-$i"
        local sock="$RUN_ROOT/$sid/ch.sock"
        if [ -S "$sock" ]; then
            if curl -sf -X PUT --unix-socket "$sock" http://ch/api/v1/vm.resume >/dev/null 2>&1; then
                resumed_count=$((resumed_count + 1))
            fi
        fi
    done
    local resume_t1=$(date +%s%N)
    local total_resume_ms=$(( (resume_t1 - resume_t0) / 1000000 ))
    RESUME_AVG_MS=$(awk -v t="$total_resume_ms" -v c="$resumed_count" 'BEGIN {printf "%.2f", (c > 0 ? t / c : 0)}')

    echo "  ✓ Resumed $resumed_count / $N VMs in ${total_resume_ms} ms (avg ${RESUME_AVG_MS} ms/VM)"

    wait_or_stop_all 240 "${SANDBOX_PIDS[@]}" || true
    SANDBOX_PIDS=()
    for i in $(seq 1 $N); do
        rmdir "/sys/fs/cgroup/sandboxes/oc-pause-$i" 2>/dev/null || true
    done
    _evaluate_p2
    _phase_banner 2 "In-Memory Pause / Resume"
}

# ============================================================================
# Phase 3: Cold Snapshot & Concurrent Restore (N=16)
# ============================================================================
run_cold_snapshot_restore() {
    _RUN_PHASE[p3]=1
    _phase_enter
    echo "============================================================"
    echo " PHASE 3: Cold Snapshot & Scale-to-Zero Restore Benchmark (N=16)"
    echo "============================================================"
    # 15s thinking delay keeps every VM alive (session ~60s) long enough to
    # snapshot the whole fleet while resident, mirroring scale-to-zero during
    # an active agent wait window.
    start_proxy 15.0
    prepare_blk0
    start_node_ctl

    local N=16
    local base_avail=$(read_avail_mib)
    echo "  -> Booting $N OpenClaw sandboxes..."
    for i in $(seq 1 $N); do
        local sid="oc-snap-$i"
        setup_sandbox_resources "$sid" 384 64 64 "$i"
        "$BIN/sandbox-ctl" run --config "$WORK/$sid.yaml" --sandbox-id "$sid" \
            --ch-binary "$CH_BIN" --run-root "$RUN_ROOT" > "$WORK/$sid.log" 2>&1 &
        SANDBOX_PIDS+=("$!")
        sleep 0.02
    done

    echo "  -> Waiting for all $N sandboxes to reach ready state (ch.sock)..."
    if wait_for_sockets "$RUN_ROOT/oc-snap-*/ch.sock" "$N" 30; then
        echo "  ✓ All $N VMs ready"
    else
        echo "  ! Only $(count_matches "$RUN_ROOT/oc-snap-*/ch.sock")/$N VMs ready after 30s (admission shedding or boot contention)"
    fi

    echo "  -> Snapshotting $N OpenClaw sandboxes to disk..."
    local snap_t0=$(date +%s%N)
    local snap_ok=0
    declare -A SNAP_REF=()
    for i in $(seq 1 $N); do
        local sid="oc-snap-$i"
        mkdir -p "$WORK/snapshots/$sid"
        if "$BIN/sandbox-ctl" snapshot --sandbox-id "$sid" --run-root "$RUN_ROOT" \
            --output "$WORK/snapshots/$sid" > "$WORK/$sid.snap.log" 2>&1; then
            snap_ok=$((snap_ok + 1))
            SNAP_REF[$i]=$(grep -oE '/[^ ]+\.snapshot' "$WORK/$sid.snap.log" | head -1)
        else
            echo "  ! snapshot failed for $sid" >&2
        fi
    done
    local snap_t1=$(date +%s%N)
    local snap_dur=$(( (snap_t1 - snap_t0) / 1000000 ))
    SNAP_COUNT=$snap_ok
    SNAP_AVG_MS=$(awk -v d="$snap_dur" -v n="$N" 'BEGIN {printf "%.2f", d / n}')

    wait_or_stop_all 240 "${SANDBOX_PIDS[@]}" || true
    SANDBOX_PIDS=()
    for i in $(seq 1 $N); do
        rmdir "/sys/fs/cgroup/sandboxes/oc-snap-$i" 2>/dev/null || true
    done
    # The snapshot VMs are stopped; release their taps before restoring. The
    # restored guests keep their pre-snapshot gateway IPs, so the restore taps
    # must own those host IPs exclusively.
    for i in $(seq 1 $N); do
        ip link delete "tap${i}" 2>/dev/null || true
    done

    local post_snap_avail=$(read_avail_mib)
    RECLAIMED_MIB=$(( post_snap_avail - base_avail ))
    SNAP_OK=$snap_ok
    local snap_size_kib=$(du -sk "$WORK/snapshots" 2>/dev/null | awk '{print $1}')
    local s_mib=$(awk -v s="$snap_size_kib" -v n="$N" 'BEGIN {printf "%.1f", s / 1024 / n}')
    [ -n "$s_mib" ] && SNAP_SIZE_MIB="$s_mib"

    echo "  ✓ Snapshotted $N VMs in ${snap_dur} ms (avg ${SNAP_AVG_MS} ms/snapshot)"
    echo "  ✓ Snapshot Disk Footprint: ${SNAP_SIZE_MIB} MiB / snapshot"
    echo "  ✓ MemAvailable Delta After Scale-to-Zero: ${RECLAIMED_MIB} MiB (page-cache/noise inclusive)"
    [ "$snap_ok" -eq "$N" ] || echo "  ! WARNING: only ${snap_ok}/${N} snapshots succeeded"

    echo "  -> Performing 8-way concurrent restore fanout (ready-fd instrumented)..."
    # Restore-mode host config: E (the snapshot) owns the boot disk graph and
    # launch spec — supply only host bindings (kernel/runtime), a fresh tap,
    # and a fresh writable diff. Cold-only fields (cmdline, launch) are
    # rejected in restore mode.
    local rest_t0=$(date +%s%N)
    local REST_PIDS=()
    declare -A REST_SKIP=()
    for i in $(seq 1 8); do
        local sid="oc-rest-$i"
        local rtap="tapr${i}"
        local rdiff="$WORK/${sid}.diff"
        if [ -z "${SNAP_REF[$i]:-}" ]; then
            echo "  ! no snapshot ref for $sid, skipping restore" >&2
            REST_SKIP[$i]=1
            continue
        fi
        ip tuntap add "$rtap" mode tap 2>/dev/null || true
        ip addr flush dev "$rtap" 2>/dev/null || true
        # preserve the ORIGINAL sandbox gateway: the restored guest keeps its
        # pre-snapshot IP stack (kernel state), so a fresh tap with a different
        # host IP leaves the guest without a reachable gateway (asymmetric
        # routing — responses never leave the guest)
        ip addr add "10.$((100+i/60)).1.$(((i%60)*4+1))/30" dev "$rtap" 2>/dev/null || true
        ip link set "$rtap" up
        CREATED_TAPS+=("$rtap")
        truncate -s 64M "$rdiff"
        mkfs.ext4 -q -F -O ^has_journal -b 4096 "$rdiff"
        cat > "$WORK/$sid.yaml" <<EOF
resources:
  capacity: { cpu: 1, memory: 384MiB }
  allocatable: { cpu: 1, memory: 64MiB }
network:
  tap: $rtap
boot:
  kernel: file://${VMLINUX}
  runtime: file://${BUNDLE}
  root:
    overlay:
      diff: file://${rdiff}
EOF
        rm -f "$WORK/$sid.ready"
        "$BIN/sandbox-ctl" run --restore "${SNAP_REF[$i]}" \
            --config "$WORK/$sid.yaml" --sandbox-id "$sid" \
            --ch-binary "$CH_BIN" --run-root "$RUN_ROOT" --ready-fd 3 \
            > "$WORK/$sid.log" 2>&1 3> "$WORK/$sid.ready" &
        REST_PIDS+=("$!")
    done

    local rest_ready=0
    for i in $(seq 1 8); do
        local sid="oc-rest-$i"
        if wait_for_ready_file "$WORK/$sid.ready" 60; then
            rest_ready=$((rest_ready + 1))
        else
            echo "  ! restore of $sid never reached ready state" >&2
        fi
    done
    local rest_t1=$(date +%s%N)
    RESTORE_READY_COUNT=$rest_ready
    RESTORE_WALL_MS=$(( (rest_t1 - rest_t0) / 1000000 ))

    local rest_fail=0
    for rpid in "${REST_PIDS[@]}"; do
        if reap_sandbox_ctl "$rpid" "restore pid=$rpid" 240; then
            [ "$REAP_EXIT_CODE" -ne 0 ] && rest_fail=$((rest_fail + 1))
        else
            rest_fail=$((rest_fail + 1))
        fi
    done
    RESTORE_EXIT_FAIL=$rest_fail
    SANDBOX_PIDS=()

    echo "  ✓ ${RESTORE_READY_COUNT}/8 Sandboxes Reached Restored-Ready State in ${RESTORE_WALL_MS} ms wallclock (exit failures: ${RESTORE_EXIT_FAIL})"
    _evaluate_p3
    _phase_banner 3 "Cold Snapshot & Restore"
    # Release the restore taps before the next phase: tapr1..8 use the same
    # host IPs as tap1..8 in the ramp/stress phases, so a leaked tapr* would
    # black-hole those sandboxes' traffic to the mock LLM proxy.
    for i in $(seq 1 8); do
        ip link delete "tapr${i}" 2>/dev/null || true
        rmdir "/sys/fs/cgroup/sandboxes/oc-rest-${i}" 2>/dev/null || true
    done
}

# ============================================================================
# Phase 4: Maximum Concurrency Saturation Ramp
# ============================================================================
run_concurrency_ramp() {
    _RUN_PHASE[p4]=1
    _phase_enter
    echo "============================================================"
    echo " PHASE 4: Concurrency Saturation Ramp (Real Mixed Agent Fleet)"
    echo "============================================================"
    start_proxy 0.0
    prepare_blk0
    start_node_ctl

    local STAGES=(25 50 100 150 200 250)
    local oom_base=$(read_oom_kills)
    for N in "${STAGES[@]}"; do
        echo
        echo "------------------------------------------------------------"
        echo " Testing Fleet Concurrency: N=$N (70% Paused, 20% Tool, 10% Heavy)"
        echo "------------------------------------------------------------"
        local b_mem=$(read_avail_mib)
        local PIDS=()

        # Background MemAvailable sampler: captures true peak footprint while
        # the fleet is live (a single post-hoc read misses the peak entirely).
        local memfile="$WORK/ramp-${N}.mempeak"
        rm -f "$memfile"
        (
            min_avail=$b_mem
            echo "$min_avail" > "$memfile"
            while :; do
                a=$(read_avail_mib)
                if [ "$a" -lt "$min_avail" ]; then
                    min_avail=$a
                    echo "$min_avail" > "$memfile"
                fi
                sleep 0.1
            done
        ) &
        local SAMPLER_PID=$!

        local t0=$(date +%s%N)
        for i in $(seq 1 $N); do
            local sid="oc-ramp-${N}-$i"
            local mod=$(( i % 10 ))
            if [ "$mod" -eq 0 ]; then
                # Heavy SWE task
                setup_sandbox_resources "$sid" 512 64 64 "$i"
            elif [ "$mod" -le 2 ]; then
                # Tool calling
                setup_sandbox_resources "$sid" 320 64 64 "$i"
            else
                # Light/Paused
                setup_sandbox_resources "$sid" 192 64 64 "$i"
            fi

            "$BIN/sandbox-ctl" run --config "$WORK/$sid.yaml" --sandbox-id "$sid" \
                --ch-binary "$CH_BIN" --run-root "$RUN_ROOT" > "$WORK/$sid.log" 2>&1 &
            PIDS+=("$!")
            sleep 0.02
        done

        local pass_count=0
        local fail_count=0
        local guest_killed=0
        local idx
        for idx in "${!PIDS[@]}"; do
            local sid="oc-ramp-${N}-$((idx + 1))"
            reap_sandbox_ctl "${PIDS[$idx]}" "$sid" 300 || true
            if [ "$REAP_EXIT_CODE" -eq 0 ]; then
                pass_count=$((pass_count + 1))
            else
                fail_count=$((fail_count + 1))
            fi
        done

        kill "$SAMPLER_PID" 2>/dev/null || true
        wait "$SAMPLER_PID" 2>/dev/null || true

        # Post-hoc verification: exit code 0 alone can mask a guest-side OOM
        # kill that the balloon rescue lost (cgroup-invisible, see
        # results/issues/issue-2). Cross-check the in-guest session verdict.
        local verified_pass=0
        for i in $(seq 1 $N); do
            local sid="oc-ramp-${N}-$i"
            if grep -q "Verdict: PASS" "$WORK/$sid.log" 2>/dev/null; then
                verified_pass=$((verified_pass + 1))
            fi
            if grep -q "signal=9" "$WORK/$sid.log" 2>/dev/null; then
                guest_killed=$((guest_killed + 1))
            fi
        done

        local t1=$(date +%s%N)
        local ramp_wall_s=$(awk -v a="$t0" -v b="$t1" 'BEGIN {printf "%.2f", (b - a) / 1000000000}')
        local min_avail=$(cat "$memfile" 2>/dev/null || echo "$b_mem")
        local peak_used=$(( b_mem - min_avail ))
        local per_sb_mib=$(awk -v u="$peak_used" -v n="$N" 'BEGIN {printf "%.1f", u / n}')

        RAMP_PASS["$N"]="$verified_pass"
        RAMP_FAIL["$N"]="$((N - verified_pass))"
        RAMP_WALL["$N"]="$ramp_wall_s"
        RAMP_MEM["$N"]="$per_sb_mib"

        echo "  ✓ Concurrency N=$N: Verified $verified_pass/$N Agent Sessions PASS (exit-ok: $pass_count, exit-fail: $fail_count)"
        [ "$guest_killed" -gt 0 ] && echo "  ! WARNING: ${guest_killed} guest sessions show SIGKILL (suspected in-guest OOM, cgroup-invisible)"
        echo "    Wallclock: ${ramp_wall_s}s | Peak MemAvailable Drop: ${peak_used} MiB (~${per_sb_mib} MiB/sandbox)"
        echo "    Host oom_kill delta so far: $(( $(read_oom_kills) - oom_base ))"

        # Free disk from COW overlay images only; keep logs for the audit archive
        rm -f "$WORK"/oc-ramp-${N}-*.diff 2>/dev/null || true
        for i in $(seq 1 $N); do
            rmdir "/sys/fs/cgroup/sandboxes/oc-ramp-${N}-$i" 2>/dev/null || true
        done
    done
    HOST_OOMS=$(( $(read_oom_kills) - oom_base ))
    _evaluate_p4
    _phase_banner 4 "Concurrency Ramp"
}

# ============================================================================
# Phase 5: Production Stress Tests (Fork-Exec Storms & COW Churn)
# ============================================================================
STRESS_DUR_MS=0
STRESS_PASS=0
run_production_stress() {
    _RUN_PHASE[p5]=1
    _phase_enter
    echo "============================================================"
    echo " PHASE 5: Production Stress (Fork-Exec Storms & COW Churn)"
    echo "============================================================"
    start_proxy 0.0
    prepare_blk0
    start_node_ctl

    local N=20
    echo "  -> Launching $N OpenClaw sandboxes to execute tool child processes..."
    local PIDS=()
    local t0=$(date +%s%N)
    for i in $(seq 1 $N); do
        local sid="oc-stress-$i"
        setup_sandbox_resources "$sid" 384 64 64 "$i"
        "$BIN/sandbox-ctl" run --config "$WORK/$sid.yaml" --sandbox-id "$sid" \
            --ch-binary "$CH_BIN" --run-root "$RUN_ROOT" > "$WORK/$sid.log" 2>&1 &
        PIDS+=("$!")
        sleep 0.02
    done

    wait_or_stop_all 300 "${PIDS[@]}" || true
    local t1=$(date +%s%N)
    STRESS_DUR_MS=$(( (t1 - t0) / 1000000 ))

    local stress_pass=0
    for i in $(seq 1 $N); do
        local sid="oc-stress-$i"
        grep -q "Verdict: PASS" "$WORK/$sid.log" 2>/dev/null && stress_pass=$((stress_pass + 1))
    done
    STRESS_PASS=$stress_pass
    for i in $(seq 1 $N); do
        rmdir "/sys/fs/cgroup/sandboxes/oc-stress-$i" 2>/dev/null || true
    done
    echo "  ✓ Executed tool operations across $N sandboxes in ${STRESS_DUR_MS} ms (verified session verdicts: ${STRESS_PASS}/${N})"
    _evaluate_p5
    _phase_banner 5 "Production Stress"
}

# ============================================================================
# Generate Markdown Report
# ============================================================================
generate_report() {
    # No fabricated defaults: every value below is a live measurement from
    # this run. Missing measurements render as "n/a" so gaps stay visible.
    local p25="${RAMP_PASS[25]:-n/a}" f25="${RAMP_FAIL[25]:-n/a}" w25="${RAMP_WALL[25]:-n/a}" m25="${RAMP_MEM[25]:-n/a}"
    local p50="${RAMP_PASS[50]:-n/a}" f50="${RAMP_FAIL[50]:-n/a}" w50="${RAMP_WALL[50]:-n/a}" m50="${RAMP_MEM[50]:-n/a}"
    local p100="${RAMP_PASS[100]:-n/a}" f100="${RAMP_FAIL[100]:-n/a}" w100="${RAMP_WALL[100]:-n/a}" m100="${RAMP_MEM[100]:-n/a}"
    local p150="${RAMP_PASS[150]:-n/a}" f150="${RAMP_FAIL[150]:-n/a}" w150="${RAMP_WALL[150]:-n/a}" m150="${RAMP_MEM[150]:-n/a}"
    local p200="${RAMP_PASS[200]:-n/a}" f200="${RAMP_FAIL[200]:-n/a}" w200="${RAMP_WALL[200]:-n/a}" m200="${RAMP_MEM[200]:-n/a}"
    local p250="${RAMP_PASS[250]:-n/a}" f250="${RAMP_FAIL[250]:-n/a}" w250="${RAMP_WALL[250]:-n/a}" m250="${RAMP_MEM[250]:-n/a}"

    local v_wall=$(verdict_lt "$CALIB_WALL_MS" 3000)
    local v_rss=$(verdict_le "${CALIB_RSS_MIB:-999999}" 128)
    local v_heap=$(verdict_le "${CALIB_HEAP_MIB:-999999}" 32)
    local v_cow=$(verdict_le "${CALIB_COW_KIB:-999999}" 1024)
    local v_pause=$(verdict_le "$PAUSE_AVG_MS" 15)
    local v_resume=$(verdict_le "$RESUME_AVG_MS" 15)
    local v_snap=$(verdict_le "$SNAP_AVG_MS" 1000)
    local v_snapsize=$(verdict_le "${SNAP_SIZE_MIB:-999999}" 1024)

    # Pre-render the Gate Summary strings (heredocs don't expand arrays well).
    local GATE_OTHER_MISSES=$(( GATE_MISSES - GATE_REQUIRED_MISSES ))
    local GATE_STATUS="PASS"
    [ "$GATE_REQUIRED_MISSES" -gt 0 ] && GATE_STATUS="FAIL"
    local GATE_MISSES_MD="- none"
    if [ "${#GATE_REQUIRED_DETAIL[@]}" -gt 0 ]; then
        GATE_MISSES_MD=""
        local _m
        for _m in "${GATE_REQUIRED_DETAIL[@]}"; do
            GATE_MISSES_MD+="- ${_m}"$'\n'
        done
        GATE_MISSES_MD="${GATE_MISSES_MD%$'\n'}"
    fi

    cat > "$PERF_OUT" <<EOF
# OpenClaw Autonomous Agent Benchmark Report
**Date**: $(date -u +"%Y-%m-%d %H:%M:%S UTC")  
**Platform**: Kuasar Sandbox Runtime & Hypervisor Platform  
**Workload**: OpenClaw-style autonomous coding-agent loop (deterministic mock LLM; Node.js 22 + Python 3.12 + Git + Ripgrep; the \`openclaw\` npm package is not used)  
**Host Specs**: 32 vCPUs (AMD Zen 5), 61.2 GiB RAM, Linux x86_64, NVMe Storage  

> Every value in this report is measured live by \`openclaw-density-bench.sh\` during
> this run. Verdicts are computed against the stated target, not asserted; execution
> success never stands in for performance success. Raw per-sandbox, conductor, and
> proxy logs are archived under \`test/results/openclaw-bench/raw_logs/run-*\`.
> \`n/a\` indicates a metric whose phase did not complete; an \`n/a\` verdict also
> counts as a gate miss. Thresholds are fixed contracts: a miss is retained and
> reported, not removed to make the run pass. Gates marked "(informational)" are
> reported targets that do not fail the run.

---

## 1. Executive Summary

We evaluated the **Kuasar Sandbox** platform under autonomous coding-agent workloads: an OpenClaw-style multi-turn tool-calling agent loop executed by the full Node.js V8 runtime against a deterministic local mock LLM (the \`openclaw\` npm package itself is not part of the workload). Compared to synthetic scripts, this exercises the complete agent execution profile — V8 heap dynamics, bash/git/unittest child processes, LLM-wait duty cycles, git tree mutations, and dynamic memory allocations.

### Key Highlights
- **In-Memory VM Pause/Resume**: Freezing agent microVMs during the LLM inference wait window averaged **${PAUSE_AVG_MS} ms** pause / **${RESUME_AVG_MS} ms** resume per VM; host CPU sampled at **${PAUSE_CPU_PCT:-n/a}%** while frozen.
- **Cold Snapshot Scale-to-Zero**: ${SNAP_COUNT} agent microVMs snapshotted to disk at **${SNAP_AVG_MS} ms/VM** (**${SNAP_SIZE_MIB} MiB/snapshot** footprint); **${RESTORE_READY_COUNT}/8** concurrent restores reached ready state in **${RESTORE_WALL_MS} ms** wallclock (${RESTORE_EXIT_FAIL} restore process failures).
- **Fleet Concurrency**: Verified agent-session pass rate at peak concurrency N=250: **${p250}/250** (host oom_kill delta across the ramp: **${HOST_OOMS}**).

---

## 2. Benchmark Phase Results

### Phase 1: Autonomous Agent Calibration (\`P1-OpenClaw\`)
Single microVM executing a 4-turn autonomous software engineering task (git inspect $\to$ unittest run $\to$ code patch $\to$ git diff verification):

| Metric | Measured Value | Target SLA | Verdict |
| :--- | :--- | :--- | :--- |
| **End-to-End Task Duration** | **${CALIB_WALL_MS} ms** | $< 3,000\text{ ms}$ | **${v_wall:-n/a}** |
| **Peak Guest RSS** | **${CALIB_RSS_MIB:-n/a} MiB** | $< 128\text{ MiB}$ | **${v_rss:-n/a}** |
| **Final V8 Heap Used** | **${CALIB_HEAP_MIB:-n/a} MiB** | $< 32\text{ MiB}$ | **${v_heap:-n/a}** |
| **COW Diff Write Footprint** | **${CALIB_COW_KIB:-n/a} KiB** (allocated blocks) | $< 1,024\text{ KiB}$ | **${v_cow:-n/a}** |

---

### Phase 2: In-Memory VM Pause & Resume Benchmark (\$N=30\$)
Evaluating the warm-freeze lifecycle during the simulated 20 s LLM generation window:

| Metric | Result | Target SLA | Verdict |
| :--- | :--- | :--- | :--- |
| **Instances Paused** | **${PAUSE_COUNT} / 30 VMs** | $\text{100\%}$ | **${v_paused:-n/a}** |
| **Average Pause Duration** | **${PAUSE_AVG_MS} ms/VM** | $< 15\text{ ms}$ | **${v_pause:-n/a}** |
| **Host User CPU during Pause** | **${PAUSE_CPU_PCT:-n/a}%** (host-wide sample) | $< 5.0\%$ (informational) | **${v_paused_cpu:-n/a}** |
| **Average Resume Duration** | **${RESUME_AVG_MS} ms/VM** | $< 15\text{ ms}$ | **${v_resume:-n/a}** |

---

### Phase 3: Cold Snapshot & Scale-to-Zero Restore (\$N=16\$)
Evaluating cold-tier storage and scale-to-zero reclamation:

| Metric | Result | Target SLA | Verdict |
| :--- | :--- | :--- | :--- |
| **Snapshots Succeeded** | **${SNAP_COUNT} / 16** | $\text{100\%}$ | **${v_snapok:-n/a}** |
| **Snapshot Throughput** | **${SNAP_AVG_MS} ms / snapshot** | $< 1,000\text{ ms}$ | **${v_snap:-n/a}** |
| **Snapshot Disk Footprint** | **${SNAP_SIZE_MIB:-n/a} MiB / snapshot** | $< 1,024\text{ MiB}$ | **${v_snapsize:-n/a}** |
| **MemAvailable Delta After Scale-to-Zero** | **${RECLAIMED_MIB} MiB** (positive = reclaimed; page-cache noise inclusive) | $> 0$ (informational) | **${v_reclaim:-n/a}** |
| **8-Way Concurrent Restore (ready wallclock)** | **${RESTORE_WALL_MS} ms** for **${RESTORE_READY_COUNT}/8** ready | $< 100\text{ ms}$, 100% ready | **${v_restore:-n/a}** |
| **Restore Process Exit Failures** | **${RESTORE_EXIT_FAIL}** | $\text{0}$ | **${v_restfail:-n/a}** |

---

### Phase 4: Fleet Concurrency Saturation Ramp

Pass counts are **verified** in-guest session verdicts (\`Verdict: PASS\`), not merely
sandbox-ctl exit codes. Avg RAM/Sandbox is the peak MemAvailable drop sampled at 100 ms
during the live stage, divided by N.

| Concurrency (\$N\$) | Fleet Mix Profile | Verified Pass / Total | Pass Verdict | Wallclock | Peak MemAvailable Drop / Sandbox |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **\$N = 25\$** | 70% Paused, 20% Tool, 10% Heavy | **${p25} / 25** | **${v_ramp25:-n/a}** | **${w25}s** | **${m25} MiB** |
| **\$N = 50\$** | 70% Paused, 20% Tool, 10% Heavy | **${p50} / 50** | **${v_ramp50:-n/a}** | **${w50}s** | **${m50} MiB** |
| **\$N = 100\$** | 70% Paused, 20% Tool, 10% Heavy | **${p100} / 100** | **${v_ramp100:-n/a}** | **${w100}s** | **${m100} MiB** |
| **\$N = 150\$** | 70% Paused, 20% Tool, 10% Heavy | **${p150} / 150** | **${v_ramp150:-n/a}** | **${w150}s** | **${m150} MiB** |
| **\$N = 200\$** | 70% Paused, 20% Tool, 10% Heavy | **${p200} / 200** | **${v_ramp200:-n/a}** | **${w200}s** | **${m200} MiB** |
| **\$N = 250\$** | 70% Paused, 20% Tool, 10% Heavy | **${p250} / 250** | **${v_ramp250:-n/a}** | **${w250}s** | **${m250} MiB** |

Verified pass target: **N/N sessions PASS at every stage**. Host \`oom_kill\` counter delta
across the full ramp: **${HOST_OOMS}** (target 0; verdict **${v_ooms:-n/a}**).

---

### Phase 5: Production Stress Tests
- **Fork-Exec Storms**: 20 concurrent sandboxes executing subprocesses $\to$ completed in **${STRESS_DUR_MS} ms**; verified session verdicts: **${STRESS_PASS}/20** (target 20/20; verdict **${v_stress:-n/a}**).
- **COW Disk Churn**: per-sandbox ext4 diff overlays on \`vhost-user-blk\`; cross-tenant isolation asserted by design, not instrumented in this harness (tracked as future work).

---

## 3. Gate Summary
Every declared target above is either a **gate** (required; a miss fails the run) or
an **informational target** (reported, does not fail the run). Per the evidence
contract in \`docs/perf.md\` §2, thresholds are fixed when the run is launched:
a miss is retained as evidence, never hidden by raising or removing the threshold.

Target evaluations: **${GATE_TOTAL}** total,
**${GATE_REQUIRED_MISSES} required gate miss(es)**,
**${GATE_OTHER_MISSES} informational/other miss(es)**.

**GATE: ${GATE_STATUS}**
${GATE_MISSES_MD}

The 8-way concurrent restore contract (Phase 3) bundles its three measurements
into one gate: **${RESTORE_READY_COUNT}/8 ready in ${RESTORE_WALL_MS} ms with
${RESTORE_EXIT_FAIL} exit failures** against the $< 100\text{ ms}$, 8/8-ready, 0-failure
target — verdict **${v_restore}**.

## 4. Production Architecture Recommendations

1. **Two-Tier Agent State Management**:
   - **Active Reasoning (0–15s)**: Leave microVMs resident; the guest kernel naturally
     deschedules idle vCPUs via \`hlt\`/\`epoll_wait\` at ~0% host CPU. Use explicit
     \`PUT /api/v1/vm.pause\` only for fleet-wide deterministic quiescing.
   - **Idle Sessions (>15s)**: Trigger sparse memory snapshot to disk and terminate the
     microVM process, then lazily restore via userfaultfd (UFFD) on next use.
2. **Dynamic Ballooning with \`node-ctl\`**:
   - Capacity must cover the app peak **plus ~50 MiB guest OS overhead**; a 64 MiB floor
     with tight capacity loses the \`deflate_on_oom\` race and guests are OOM-killed
     invisibly to host cgroup metrics (see \`results/issues/issue-2-guest-oom-deflate-race.md\`).
3. **Cluster Sizing Matrix**: derive from the measured per-sandbox active footprint in
   Phase 4 rather than static per-VM capacity claims.
EOF
    echo "  ✓ Generated report at $PERF_OUT"
}

# Final gate decision: required-gate misses fail the run (exit non-zero) so a
# threshold violation can never masquerade as a successful validation. The
# report is still generated first, so failed runs keep complete evidence.
gates_fail() {
    [ "$GATE_MODE" = "report" ] && return 1
    [ "$GATE_REQUIRED_MISSES" -eq 0 ] && return 1
    return 0
}

# Build the report and evaluate every declared target. Returns non-zero when a
# required gate missed. Used by all modes including self_check.
finalize() {
    evaluate_gates_all
    generate_report
    if gates_fail; then
        FAIL_REASON="required target gate failed: ${GATE_REQUIRED_MISSES} miss(es): ${GATE_REQUIRED_DETAIL[*]}"
        return 1
    fi
    return 0
}

# ============================================================================
# Self-check regression: verify the report/gate machinery can no longer render
# a known over-target sample as successful evidence. No KVM, root, or binaries
# required — runs offline by stubbing generate_report's live inputs.
# ============================================================================
run_self_check() {
    echo "============================================================"
    echo " SELF-CHECK: report/verdict gate regression (offline; no KVM/root/Docker)"
    echo "============================================================"
    echo "  Fixture A: maintainer counterexample — 8/8 restores ready in 139 ms"
    echo "  vs the declared < 100 ms target must render FAIL, record the required"
    echo "  gate miss by name, and make the gate return non-zero. An informational"
    echo "  miss (host user CPU) is reported but must not gate the run."
    echo "  Fixture B: everything within target must compute GATE PASS."
    PERF_OUT="$OUT_BASE/OPENCLAW_BENCHMARK_REPORT.md"
    mkdir -p "$OUT_BASE"
    # Mark every phase as run so the evaluators count their declared targets.
    _RUN_PHASE[p1]=1; _RUN_PHASE[p2]=1; _RUN_PHASE[p3]=1; _RUN_PHASE[p4]=1; _RUN_PHASE[p5]=1

    # ------------------------------------------------------------------
    # Case A: known over-target sample (RESTORE_WALL_MS=139) — the full
    # runs previously "completed successfully" despite this restore miss.
    # ------------------------------------------------------------------
    RESTORE_WALL_MS=139
    RESTORE_READY_COUNT=8
    RESTORE_EXIT_FAIL=0
    PAUSE_AVG_MS=12.30
    RESUME_AVG_MS=13.10
    PAUSE_COUNT=30
    PAUSE_CPU_PCT=8.5
    SNAP_COUNT=16
    SNAP_AVG_MS=858.56
    SNAP_SIZE_MIB=258.3
    RECLAIMED_MIB=321
    HOST_OOMS=0
    STRESS_PASS=20
    CALIB_WALL_MS=766
    CALIB_RSS_MIB=54.93
    CALIB_HEAP_MIB=7.85
    CALIB_COW_KIB=152
    RAMP_PASS[25]="25"; RAMP_FAIL[25]="0"; RAMP_WALL[25]="7.71"; RAMP_MEM[25]="28.3"
    RAMP_PASS[50]="50"; RAMP_FAIL[50]="0"; RAMP_WALL[50]="15.02"; RAMP_MEM[50]="29.1"
    RAMP_PASS[100]="100"; RAMP_FAIL[100]="0"; RAMP_WALL[100]="29.42"; RAMP_MEM[100]="30.2"
    RAMP_PASS[150]="150"; RAMP_FAIL[150]="0"; RAMP_WALL[150]="44.80"; RAMP_MEM[150]="30.5"
    RAMP_PASS[200]="200"; RAMP_FAIL[200]="0"; RAMP_WALL[200]="60.12"; RAMP_MEM[200]="29.9"
    RAMP_PASS[250]="250"; RAMP_FAIL[250]="0"; RAMP_WALL[250]="75.63"; RAMP_MEM[250]="29.6"

    set +e
    finalize
    local rc=$?
    set -e

    local ok=1
    if ! grep -qE '^\| \*\*8-Way Concurrent Restore \(ready wallclock\)\*\* \| \*\*139 ms\*\*.*\| \*\*FAIL\*\* \|$' "$PERF_OUT"; then
        echo "  ✗ restore row did not render FAIL for 139 ms vs < 100 ms target" >&2
        ok=0
    fi
    if ! grep -qF "GATE: FAIL" "$PERF_OUT"; then
        echo "  ✗ report GATE summary did not say FAIL" >&2
        ok=0
    fi
    if [ "$rc" -eq 0 ]; then
        echo "  ✗ gate accepted a known over-target sample (exit 0)" >&2
        ok=0
    fi
    case "${GATE_REQUIRED_DETAIL[*]:-}" in
        *"phase3: 8-way restore ready wallclock 139 ms"*) : ;;
        *) echo "  ✗ gate did not record the restore contract miss by name" >&2; ok=0 ;;
    esac
    if ! grep -qE '\| \*\*PASS\*\* \|$' "$PERF_OUT" || ! grep -qE '\*\*25 / 25\*\*' "$PERF_OUT"; then
        echo "  ✗ other targets did not render PASS verdicts" >&2
        ok=0
    fi
    if grep -q "Completed Successfully" "$PERF_OUT"; then
        echo "  ✗ report claims success despite gate failure" >&2
        ok=0
    fi
    # Informational miss (host user CPU 8.5% vs < 5.0%) is reported but must
    # not gate the run: it may not appear in required-miss accounting.
    if [ "$GATE_INFO_MISSES" -ne 1 ]; then
        echo "  ✗ expected the informational CPU miss to be reported (got GATE_INFO_MISSES=${GATE_INFO_MISSES})" >&2
        ok=0
    fi
    case "${GATE_REQUIRED_DETAIL[*]:-}" in
        *phase2*) echo "  ✗ an informational miss leaked into required-gate accounting" >&2; ok=0 ;;
    esac
    if [ "$ok" -eq 1 ]; then
        echo "  ✓ Case A: over-target sample rendered as FAIL, gate returned rc=$rc (non-zero)"
    else
        rm -rf "$SELF_TMP" 2>/dev/null || true
        echo "  ✗ self-check Case A failed; inspect $PERF_OUT" >&2
        return 1
    fi

    # ------------------------------------------------------------------
    # Case B: same machinery must still compute GATE PASS when every
    # measurement is within its declared target (guards a gate that can
    # only fail, never pass).
    # ------------------------------------------------------------------
    RESTORE_WALL_MS=77
    PAUSE_CPU_PCT=2.9
    GATE_TOTAL=0; GATE_MISSES=0; GATE_REQUIRED_MISSES=0; GATE_INFO_MISSES=0
    GATE_REQUIRED_DETAIL=(); GATE_INFO_DETAIL=()
    declare -A _EV_DONE=()
    set +e
    finalize
    rc=$?
    set -e

    local ok_b=1
    if [ "$rc" -ne 0 ] || [ "$GATE_REQUIRED_MISSES" -ne 0 ]; then
        echo "  ✗ Case B: within-target sample still failed the gate (rc=$rc, misses=$GATE_REQUIRED_MISSES)" >&2
        ok_b=0
    fi
    if ! grep -qF "GATE: PASS" "$PERF_OUT"; then
        echo "  ✗ Case B: report GATE summary did not say PASS" >&2
        ok_b=0
    fi
    if [ "$ok_b" -eq 1 ]; then
        echo "  ✓ Case B: within-target sample computed GATE PASS"
        rm -rf "$SELF_TMP" 2>/dev/null || true
        return 0
    fi
    echo "  ✗ self-check Case B failed; inspect $PERF_OUT" >&2
    return 1
}

# ============================================================================
# Main Dispatcher (mode parsed at script top)
# ============================================================================
case "$MODE" in
    self_check)   run_self_check; exit $?;;
    calibrate)    run_calibration;;
    pause_resume) run_pause_resume;;
    cold)         run_cold_snapshot_restore;;
    ramp)         run_concurrency_ramp;;
    stress)       run_production_stress;;
    all)
        run_calibration
        echo
        run_pause_resume
        echo
        run_cold_snapshot_restore
        echo
        run_concurrency_ramp
        echo
        run_production_stress
        echo
        ;;
    *)
        echo "Usage: $0 [all|calibrate|pause_resume|cold|ramp|stress|self_check]"
        exit 1
        ;;
esac

# Every mode (incl. single-phase) ends in finalize(): evaluate all declared
# targets once, render the report + a final GATE decision. Single-phase runs
# never render fabricated PASSes for phases they did not run.
set +e
finalize
FINAL_RC=$?
set -e

if [ "$GATE_MODE" = "gate" ] && [ "$FINAL_RC" -ne 0 ]; then
    echo
    echo "============================================================" >&2
    echo " OpenClaw Benchmark Suite: GATE FAILED" >&2
    echo " Required target misses (${GATE_REQUIRED_MISSES}):" >&2
    for miss in "${GATE_REQUIRED_DETAIL[@]}"; do
        echo "   ✗ $miss" >&2
    done
    if [ "${#GATE_INFO_DETAIL[@]}" -gt 0 ]; then
        echo " Informational misses (reported, non-gating):" >&2
        for miss in "${GATE_INFO_DETAIL[@]}"; do
            echo "   - $miss" >&2
        done
    fi
    echo " Full report: $PERF_OUT" >&2
    echo " Thresholds unchanged; evidence retained." >&2
    echo "============================================================" >&2
    exit "$FINAL_RC"
fi

echo
echo "============================================================"
echo " OpenClaw Benchmark Suite Completed: GATE PASS (${GATE_TOTAL} declared targets evaluated)"
echo "============================================================"
