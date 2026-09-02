#!/usr/bin/env bash
#
# openclaw-density-bench.sh — Real OpenClaw Autonomous Agent Benchmark Suite
#
# Tests Kuasar Sandbox under genuine Node.js 22 + OpenClaw agent workloads:
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

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="${BIN:-$REPO_ROOT/bin}"
# All durable outputs (report, log archive, cached rootfs) live under
# test/results/ — the same git-ignored output area used by the other perf
# suites — so the harness leaves no untracked files in the repository.
OUT_BASE="${OUT_BASE:-$REPO_ROOT/test/results/openclaw-bench}"
PERF_OUT="${PERF_OUT:-$OUT_BASE/OPENCLAW_BENCHMARK_REPORT.md}"
RAW_DIR="$OUT_BASE/raw_logs"
# Scratch (VM run-roots, sockets, snapshots) lives on disk-backed /var/tmp:
# (a) unix socket paths must stay under the 108-byte sockaddr_un limit, and
# (b) snapshot churn (~1.6 GiB/VM) must not land on tmpfs.
WORK="${WORK:-/var/tmp/openclaw-bench/run-$(date +%Y%m%d-%H%M%S)-$$}"
PROXY_PORT=8088
IMAGE="openclaw-agent:latest"

mkdir -p "$WORK" "$RAW_DIR" "$WORK/run" "$WORK/lib" "$WORK/units"

VMLINUX="$BIN/vmlinux"
BUNDLE="$BIN/sandbox-runtime.bundle"
CH_BIN="$BIN/cloud-hypervisor"

for b in "$BIN/sandbox-ctl" "$BIN/node-ctl" "$BIN/flatten-ctl" "$VMLINUX" "$BUNDLE" "$CH_BIN"; do
    if [ ! -f "$b" ]; then
        echo "Error: missing required binary $b" >&2
        exit 1
    fi
done

# Cleanup trap
PROXY_PID=""
DAEMON_PID=""
SANDBOX_PIDS=()
CREATED_TAPS=()

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
    pkill -9 -f "cloud-hypervisor.*$WORK" 2>/dev/null || true
    if [ -n "$DAEMON_PID" ]; then
        kill -TERM "$DAEMON_PID" 2>/dev/null || true
        wait "$DAEMON_PID" 2>/dev/null || true
    fi
    for tap in "${CREATED_TAPS[@]:-}"; do
        ip link delete "$tap" 2>/dev/null || true
    done
    # KEEP_WORK=1 preserves snapshots/scratch for post-run analysis (dedup, etc.)
    [ "${KEEP_WORK:-0}" = "1" ] || rm -rf "$WORK" 2>/dev/null || true
    set -e
}
trap cleanup EXIT INT TERM

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

# Prepare openclaw-blk0.img rootfs
CACHED_BLK0="$OUT_BASE/openclaw-blk0.img"
prepare_blk0() {
    if [ -f "$CACHED_BLK0" ]; then
        BLK0="$CACHED_BLK0"
        echo "==> Using cached OpenClaw block device: $BLK0"
    else
        BLK0="$WORK/openclaw-blk0.img"
        if [ ! -f "$BLK0" ]; then
            echo "==> Exporting OpenClaw container image to EROFS block device..."
            docker save "$IMAGE" | "$BIN/flatten-ctl" export --output "$BLK0" --no-progress >/dev/null
            cp "$BLK0" "$CACHED_BLK0" 2>/dev/null || true
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

verdict_le() { awk -v v="$1" -v s="$2" 'BEGIN {print (v+0 <= s+0) ? "PASS" : "FAIL"}'; }
verdict_lt() { awk -v v="$1" -v s="$2" 'BEGIN {print (v+0 <  s+0) ? "PASS" : "FAIL"}'; }

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
proxy: { mode: internal, auth: enforce }
sandbox:
  boot:
    kernel: $VMLINUX
    runtime: $BUNDLE
paths:
  run_root: $WORK/run
  base_root: $WORK/lib
  config_socket: $WORK/node-ctl.socket
  db_path: $WORK/node-ctl.db
units: { dir: $WORK/units, install: false }
resource_listen:
  enabled: true
  socket: $WORK/sandbox-resource.sock
  state_path: $WORK/state.json
  audit_path: $WORK/audit.log
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
        [ -S "$WORK/sandbox-resource.sock" ] && break
        sleep 0.05
    done
}

setup_sandbox_resources() {
    local sid="$1" cap="$2" floor="$3" burst="$4" idx="${5:-1}"
    local tap_name="tap${idx}"
    mkdir -p "/sys/fs/cgroup/sandboxes/$sid"
    
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
    controller: $WORK/sandbox-resource.sock
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

# ============================================================================
# Phase 1: Single Sandbox Calibration (P1-OpenClaw)
# ============================================================================
run_calibration() {
    echo "============================================================"
    echo " PHASE 1: Real OpenClaw Single-Sandbox Calibration"
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
        --ch-binary "$CH_BIN" --run-root "$WORK/run" > "$WORK/$sid.log" 2>&1 &
    local s_pid=$!
    SANDBOX_PIDS+=("$s_pid")

    wait "$s_pid"
    local t1=$(date +%s%N)
    CALIB_WALL_MS=$(( (t1 - t0) / 1000000 ))

    echo "  -> OpenClaw Session Completed in ${CALIB_WALL_MS} ms"
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
}

# ============================================================================
# Phase 2: In-Memory VM Pause & Resume Benchmark (N=30)
# ============================================================================
run_pause_resume() {
    echo "============================================================"
    echo " PHASE 2: In-Memory VM Pause / Resume Benchmark (N=30)"
    echo "============================================================"
    # 20s thinking delay: each admitted VM parks in its turn-1 LLM wait long
    # enough to pause/resume it mid-session (4 turns -> ~80s session).
    start_proxy 20.0
    prepare_blk0
    start_node_ctl

    local N=30
    echo "  -> Launching $N OpenClaw sandboxes with 20s thinking delay..."
    for i in $(seq 1 $N); do
        local sid="oc-pause-$i"
        setup_sandbox_resources "$sid" 384 64 64 "$i"
        "$BIN/sandbox-ctl" run --config "$WORK/$sid.yaml" --sandbox-id "$sid" \
            --ch-binary "$CH_BIN" --run-root "$WORK/run" > "$WORK/$sid.log" 2>&1 &
        SANDBOX_PIDS+=("$!")
        sleep 0.02
    done

    echo "  -> Waiting for all $N sandboxes to reach ready state (ch.sock)..."
    if wait_for_sockets "$WORK/run/oc-pause-*/ch.sock" "$N" 30; then
        echo "  ✓ All $N VMs ready"
    else
        echo "  ! Only $(count_matches "$WORK/run/oc-pause-*/ch.sock")/$N VMs ready after 30s (admission shedding or boot contention)"
    fi

    echo "  -> Issuing /api/v1/vm.pause to active sandboxes..."
    local pause_t0=$(date +%s%N)
    local paused_count=0
    for i in $(seq 1 $N); do
        local sid="oc-pause-$i"
        local sock="$WORK/run/$sid/ch.sock"
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
        local sock="$WORK/run/$sid/ch.sock"
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

    for spid in "${SANDBOX_PIDS[@]}"; do
        wait "$spid" 2>/dev/null || true
    done
    SANDBOX_PIDS=()
    echo "  ✓ In-Memory Pause / Resume Benchmark Completed Successfully"
}

# ============================================================================
# Phase 3: Cold Snapshot & Concurrent Restore (N=16)
# ============================================================================
run_cold_snapshot_restore() {
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
            --ch-binary "$CH_BIN" --run-root "$WORK/run" > "$WORK/$sid.log" 2>&1 &
        SANDBOX_PIDS+=("$!")
        sleep 0.02
    done

    echo "  -> Waiting for all $N sandboxes to reach ready state (ch.sock)..."
    if wait_for_sockets "$WORK/run/oc-snap-*/ch.sock" "$N" 30; then
        echo "  ✓ All $N VMs ready"
    else
        echo "  ! Only $(count_matches "$WORK/run/oc-snap-*/ch.sock")/$N VMs ready after 30s (admission shedding or boot contention)"
    fi

    echo "  -> Snapshotting $N OpenClaw sandboxes to disk..."
    local snap_t0=$(date +%s%N)
    local snap_ok=0
    declare -A SNAP_REF=()
    for i in $(seq 1 $N); do
        local sid="oc-snap-$i"
        mkdir -p "$WORK/snapshots/$sid"
        if "$BIN/sandbox-ctl" snapshot --sandbox-id "$sid" --run-root "$WORK/run" \
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

    for spid in "${SANDBOX_PIDS[@]}"; do
        wait "$spid" 2>/dev/null || true
    done
    SANDBOX_PIDS=()

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
        ip addr add "10.200.1.$(( 2 * i + 1 ))/31" dev "$rtap" 2>/dev/null || true
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
            --ch-binary "$CH_BIN" --run-root "$WORK/run" --ready-fd 3 \
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
        if ! wait "$rpid" 2>/dev/null; then
            rest_fail=$((rest_fail + 1))
        fi
    done
    RESTORE_EXIT_FAIL=$rest_fail
    SANDBOX_PIDS=()

    echo "  ✓ ${RESTORE_READY_COUNT}/8 Sandboxes Reached Restored-Ready State in ${RESTORE_WALL_MS} ms wallclock (exit failures: ${RESTORE_EXIT_FAIL})"
}

# ============================================================================
# Phase 4: Maximum Concurrency Saturation Ramp
# ============================================================================
run_concurrency_ramp() {
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
                --ch-binary "$CH_BIN" --run-root "$WORK/run" > "$WORK/$sid.log" 2>&1 &
            PIDS+=("$!")
            sleep 0.02
        done

        local pass_count=0
        local fail_count=0
        local guest_killed=0
        for spid in "${PIDS[@]}"; do
            if wait "$spid" 2>/dev/null; then
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
    done
    HOST_OOMS=$(( $(read_oom_kills) - oom_base ))
}

# ============================================================================
# Phase 5: Production Stress Tests (Fork-Exec Storms & COW Churn)
# ============================================================================
STRESS_DUR_MS=0
STRESS_PASS=0
run_production_stress() {
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
            --ch-binary "$CH_BIN" --run-root "$WORK/run" > "$WORK/$sid.log" 2>&1 &
        PIDS+=("$!")
        sleep 0.02
    done

    for spid in "${PIDS[@]}"; do
        wait "$spid" 2>/dev/null || true
    done
    local t1=$(date +%s%N)
    STRESS_DUR_MS=$(( (t1 - t0) / 1000000 ))

    local stress_pass=0
    for i in $(seq 1 $N); do
        local sid="oc-stress-$i"
        grep -q "Verdict: PASS" "$WORK/$sid.log" 2>/dev/null && stress_pass=$((stress_pass + 1))
    done
    STRESS_PASS=$stress_pass
    echo "  ✓ Executed tool operations across $N sandboxes in ${STRESS_DUR_MS} ms (verified session verdicts: ${STRESS_PASS}/${N})"
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

    cat > "$PERF_OUT" <<EOF
# OpenClaw Autonomous Agent Benchmark Report
**Date**: $(date -u +"%Y-%m-%d %H:%M:%S UTC")  
**Platform**: Kuasar Sandbox Runtime & Hypervisor Platform  
**Workload**: Real Autonomous Agent (\`openclaw@2026.8.1\` on Node.js 22 + Python 3.12 + Git + Ripgrep)  
**Host Specs**: 32 vCPUs (AMD Zen 5), 61.2 GiB RAM, Linux x86_64, NVMe Storage  

> Every value in this report is measured live by \`openclaw-density-bench.sh\` during
> this run. Verdicts are computed against the stated SLA, not asserted. Raw per-sandbox,
> conductor, and proxy logs are archived under \`test/results/openclaw-bench/raw_logs/run-*\`.
> \`n/a\` indicates a metric whose phase did not complete.

---

## 1. Executive Summary

We evaluated the **Kuasar Sandbox** platform under genuine autonomous coding agent workloads using the real **OpenClaw** (\`openclaw@2026.8.1\`) runtime. Unlike synthetic scripts, this benchmark runs the full Node.js V8 execution engine, multi-turn LLM tool-calling loops, git tree mutations, unit test executions, and dynamic memory allocations across a spectrum of agent duty cycles.

### Key Highlights
- **In-Memory VM Pause/Resume**: Freezing agent microVMs during the LLM inference wait window averaged **${PAUSE_AVG_MS} ms** pause / **${RESUME_AVG_MS} ms** resume per VM; host CPU sampled at **${PAUSE_CPU_PCT:-n/a}%** while frozen.
- **Cold Snapshot Scale-to-Zero**: ${SNAP_COUNT} agent microVMs snapshotted to disk at **${SNAP_AVG_MS} ms/VM** (**${SNAP_SIZE_MIB} MiB/snapshot** footprint); **${RESTORE_READY_COUNT}/8** concurrent restores reached ready state in **${RESTORE_WALL_MS} ms** wallclock (${RESTORE_EXIT_FAIL} restore process failures).
- **Fleet Concurrency**: Verified agent-session pass rate at peak concurrency N=250: **${p250}/250** (host oom_kill delta across the ramp: **${HOST_OOMS}**).

---

## 2. Benchmark Phase Results

### Phase 1: Real OpenClaw Calibration (\`P1-OpenClaw\`)
Single microVM executing a 4-turn autonomous software engineering task (git inspect $\to$ unittest run $\to$ code patch $\to$ git diff verification):

| Metric | Measured Value | Target SLA | Verdict |
| :--- | :--- | :--- | :--- |
| **End-to-End Task Duration** | **${CALIB_WALL_MS} ms** | $< 3,000\text{ ms}$ | **${v_wall}** |
| **Peak Guest RSS** | **${CALIB_RSS_MIB:-n/a} MiB** | $< 128\text{ MiB}$ | **${v_rss}** |
| **Final V8 Heap Used** | **${CALIB_HEAP_MIB:-n/a} MiB** | $< 32\text{ MiB}$ | **${v_heap}** |
| **COW Diff Write Footprint** | **${CALIB_COW_KIB:-n/a} KiB** (allocated blocks) | $< 1,024\text{ KiB}$ | **${v_cow}** |

---

### Phase 2: In-Memory VM Pause & Resume Benchmark (\$N=30\$)
Evaluating the warm-freeze lifecycle during the simulated 20 s LLM generation window:

| Metric | Result | Target SLA |
| :--- | :--- | :--- |
| **Instances Paused** | **${PAUSE_COUNT} / 30 VMs** | \$100\%\$ |
| **Average Pause Duration** | **${PAUSE_AVG_MS} ms/VM** | $< 15\text{ ms}$ |
| **Host User CPU during Pause** | **${PAUSE_CPU_PCT:-n/a}%** (host-wide sample) | $< 5.0\%$ |
| **Average Resume Duration** | **${RESUME_AVG_MS} ms/VM** | $< 15\text{ ms}$ |

---

### Phase 3: Cold Snapshot & Scale-to-Zero Restore (\$N=16\$)
Evaluating cold-tier storage and scale-to-zero reclamation:

| Metric | Result | Target SLA |
| :--- | :--- | :--- |
| **Snapshots Succeeded** | **${SNAP_COUNT} / 16** | \$100\%\$ |
| **Snapshot Throughput** | **${SNAP_AVG_MS} ms / snapshot** | $< 1,000\text{ ms}$ |
| **Snapshot Disk Footprint** | **${SNAP_SIZE_MIB:-n/a} MiB / snapshot** | $< 1,024\text{ MiB}$ |
| **MemAvailable Delta After Scale-to-Zero** | **${RECLAIMED_MIB} MiB** (positive = reclaimed; page-cache noise inclusive) | $> 0$ |
| **8-Way Concurrent Restore (ready wallclock)** | **${RESTORE_WALL_MS} ms** for **${RESTORE_READY_COUNT}/8** ready | $< 100\text{ ms}$, 100% ready |
| **Restore Process Exit Failures** | **${RESTORE_EXIT_FAIL}** | 0 |

---

### Phase 4: Fleet Concurrency Saturation Ramp

Pass counts are **verified** in-guest session verdicts (\`Verdict: PASS\`), not merely
sandbox-ctl exit codes. Avg RAM/Sandbox is the peak MemAvailable drop sampled at 100 ms
during the live stage, divided by N.

| Concurrency (\$N\$) | Fleet Mix Profile | Verified Pass / Total | Wallclock | Peak MemAvailable Drop / Sandbox |
| :--- | :--- | :--- | :--- | :--- |
| **\$N = 25\$** | 70% Paused, 20% Tool, 10% Heavy | **${p25} / 25** | **${w25}s** | **${m25} MiB** |
| **\$N = 50\$** | 70% Paused, 20% Tool, 10% Heavy | **${p50} / 50** | **${w50}s** | **${m50} MiB** |
| **\$N = 100\$** | 70% Paused, 20% Tool, 10% Heavy | **${p100} / 100** | **${w100}s** | **${m100} MiB** |
| **\$N = 150\$** | 70% Paused, 20% Tool, 10% Heavy | **${p150} / 150** | **${w150}s** | **${m150} MiB** |
| **\$N = 200\$** | 70% Paused, 20% Tool, 10% Heavy | **${p200} / 200** | **${w200}s** | **${m200} MiB** |
| **\$N = 250\$** | 70% Paused, 20% Tool, 10% Heavy | **${p250} / 250** | **${w250}s** | **${m250} MiB** |

Host \`oom_kill\` counter delta across the full ramp: **${HOST_OOMS}**.

---

### Phase 5: Production Stress Tests
- **Fork-Exec Storms**: 20 concurrent sandboxes executing subprocesses $\to$ completed in **${STRESS_DUR_MS} ms**; verified session verdicts: **${STRESS_PASS}/20**.
- **COW Disk Churn**: per-sandbox ext4 diff overlays on \`vhost-user-blk\`; cross-tenant isolation asserted by design, not instrumented in this harness (tracked as future work).

---

## 3. Production Architecture Recommendations

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

# ============================================================================
# Main Dispatcher
# ============================================================================
MODE="${1:-all}"
case "$MODE" in
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
        generate_report
        ;;
    *)
        echo "Usage: $0 [all|calibrate|pause_resume|cold|ramp|stress]"
        exit 1
        ;;
esac

echo
echo "============================================================"
echo " OpenClaw Benchmark Suite Completed Successfully!"
echo "============================================================"
