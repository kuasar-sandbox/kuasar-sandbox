#!/usr/bin/env bash
#
# openclaw-soak.sh — Long-Running Mixed-Fleet OpenClaw Sustainment Soak
#
# Churns a mixed fleet of real OpenClaw agent sandboxes in waves until the
# time budget expires, mixing three agent "types" (fast / medium / slow
# thinking delays) with periodic lifecycle churn (vm.pause/vm.resume every
# wave, snapshot→restore every SOAK_CHURN_EVERY waves) to surface bugs that
# only appear under sustained load: admission shedding, guest OOM kills,
# process/TAP/cgroup leaks, restore races, balloon failures.
#
# Usage:
#   sudo bash openclaw-soak.sh [duration_seconds]   (default 1200)
#
# Environment:
#   SOAK_WAVE_N      sandboxes per wave (default 24)
#   SOAK_CHURN_EVERY snapshot/restore churn interval in waves (default 3)
#   KEEP_WORK=1      keep scratch dir for post-mortem
#   OUT_BASE         output base dir (default test/results/openclaw-soak)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="${BIN:-$REPO_ROOT/bin}"
OUT_BASE="${OUT_BASE:-$REPO_ROOT/test/results/openclaw-soak}"
DURATION="${1:-1200}"
WAVE_N="${SOAK_WAVE_N:-24}"
CHURN_EVERY="${SOAK_CHURN_EVERY:-3}"
WORK="${WORK:-/var/tmp/openclaw-soak-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT_BASE" "$WORK/run" "$WORK/lib" "$WORK/units" "$WORK/snapshots"

VMLINUX="$BIN/vmlinux"
BUNDLE="$BIN/sandbox-runtime.bundle"
CH_BIN="$BIN/cloud-hypervisor"
for b in "$BIN/sandbox-ctl" "$BIN/node-ctl" "$VMLINUX" "$BUNDLE" "$CH_BIN"; do
    [ -f "$b" ] || { echo "Error: missing $b" >&2; exit 1; }
done

BLK0="$REPO_ROOT/test/results/openclaw-bench/openclaw-blk0.img"
if [ ! -f "$BLK0" ]; then
    echo "==> Exporting OpenClaw container image to EROFS (one-time)..."
    mkdir -p "$(dirname "$BLK0")"
    docker save openclaw-agent:latest | "$BIN/flatten-ctl" export --output "$BLK0" --no-progress >/dev/null
fi

# ---- agent types: name:port:delay:mode:cap ---------------------------------
# 12 fast-churn (full 4-turn, no delay), 6 tool-only w/ 2s thinking,
# 4 heavy full w/ 5s thinking, 2 tool-only w/ 5s thinking  (per 24)
TYPES=(fast:8088:0.0:full:192 tool:8089:2.0:tool_only:320 heavy:8090:5.0:full:512 idle:8091:5.0:tool_only:192)

PROXY_PIDS=()
DAEMON_PID=""
SANDBOX_PIDS=()
CREATED_TAPS=()
WAVE=0

cleanup() {
    set +e
    echo; echo "==> Soak cleanup..."
    local raw="$OUT_BASE/raw-logs-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$raw"; cp -a "$WORK"/*.log "$WORK"/*.csv "$WORK"/*.yaml "$raw"/ 2>/dev/null || true
    for p in "${PROXY_PIDS[@]:-}"; do kill -TERM "$p" 2>/dev/null || true; done
    for spid in "${SANDBOX_PIDS[@]:-}"; do kill -9 "$spid" 2>/dev/null || true; done
    pkill -9 -f "cloud-hypervisor.*openclaw-soak" 2>/dev/null || true
    [ -n "$DAEMON_PID" ] && { kill -TERM "$DAEMON_PID" 2>/dev/null || true; wait "$DAEMON_PID" 2>/dev/null || true; }
    for tap in "${CREATED_TAPS[@]:-}"; do ip link delete "$tap" 2>/dev/null || true; done
    [ "${KEEP_WORK:-0}" = "1" ] || rm -rf "$WORK"
    set -e
}
trap "cleanup; audit" EXIT INT TERM

# ---- hygiene + failure-class audit (post-teardown: true leak counts) ------
audit() {
    echo; echo "==> Soak complete: $WAVE waves"
    echo "  host oom_kill delta: $(( $(read_oom_kills) - OOM0 ))"
    echo "  leaked CH processes:    $(pgrep -fc '[c]loud-hypervisor' || true)"
    echo "  leaked conductors:      $(pgrep -fc 'conductor [s]erve' || true)"
    echo "  leaked sandbox-ctl:     $(pgrep -fc 'sandbox-ctl [r]un' || true)"
    echo "  zombies:                $(ps -eo stat | grep -c '^Z' || true)"
    echo "  leftover TAPs:          $(ip link | grep -cE 'tap[0-9r]' || true)"
    echo "  leftover cgroups:       $(ls /sys/fs/cgroup/sandboxes/ 2>/dev/null | grep -c '^oc-' || true)"
    echo "  guest SIGKILL events:   $(grep -l 'signal=9\|code=137' "$WORK"/oc-*.log 2>/dev/null | wc -l)"
    echo "  admission rejections:   $(grep -l 'admit rejected' "$WORK"/oc-*.log 2>/dev/null | wc -l)"
    echo "  uffd handler errors:    $(grep -h 'errors  *[1-9]' "$WORK"/oc-*.log 2>/dev/null | wc -l)"
    echo "  CH nonzero exits:       $(grep -h 'CH exited code=[^0]' "$WORK"/oc-*.log 2>/dev/null | wc -l)"
    echo "  CSV: $CSV"
}

read_avail_mib() { awk '/MemAvailable:/ {printf "%d", $2/1024}' /proc/meminfo; }
read_oom_kills() { awk '/^oom_kill /{print $2}' /proc/vmstat 2>/dev/null || echo 0; }

# ---- three agent-type proxies + conductor ---------------------------------
for t in "${TYPES[@]}"; do
    IFS=: read -r name port delay mode cap <<<"$t"
    python3 "$REPO_ROOT/test/perf/workloads/agent_llm_proxy.py" --port "$port" --delay "$delay" \
        > "$WORK/proxy-$name.log" 2>&1 &
    PROXY_PIDS+=("$!")
done
sleep 0.7
for t in "${TYPES[@]}"; do
    IFS=: read -r name port _ <<<"$t"
    curl -sf "http://127.0.0.1:$port/health" >/dev/null || { echo "proxy $name failed" >&2; exit 1; }
done

phys_mem="$(awk '/MemTotal:/ {printf "%d", $2/1024}' /proc/meminfo)MiB"
cat > "$WORK/conductor.yaml" <<EOF
api: { domain: soak.local, listen: "127.0.0.1:0" }
encryption_key: "0000000000000000000000000000000000000000000000000000000000000000"
proxy: { mode: internal, auth: enforce }
sandbox:
  boot: { kernel: $VMLINUX, runtime: $BUNDLE }
paths:
  run_root: $WORK/run
  base_root: $WORK/lib
  config_socket: $WORK/node-ctl.socket
  db_path: $WORK/node-ctl.db
units: { dir: $WORK/units, install: false }
resource_listen:
  enabled: true
  socket: $WORK/sandbox-resource.sock
  audit_path: $WORK/audit.log
  cgroup_scan_paths:
    - /sys/fs/cgroup/sandboxes
  resources:
    physical_memory: $phys_mem
    physical_cpu: $(nproc)
    host_reserved: { memory: 1024MiB, cpu: 1 }
  watermarks:
    operational_margin_factor: 0.10
    high_factor: 0.85
    low_factor: 0.70
    emergency_factor: 0.05
    startup_factor: 0.98
  rate_limits: { memory_grant_per_sec_factor: 0.50 }
  admission: { rate: 200, burst: 400, startup_ttl: 45s, queue_ttl: 90s, queue_max_depth: 1024 }
  log_level: info
EOF
[ -d /sys/fs/cgroup/sandboxes ] || mkdir -p /sys/fs/cgroup/sandboxes
echo "+memory +cpu" > /sys/fs/cgroup/sandboxes/cgroup.subtree_control 2>/dev/null || true
"$BIN/node-ctl" conductor serve --config "$WORK/conductor.yaml" > "$WORK/conductor.log" 2>&1 &
DAEMON_PID=$!
for _ in {1..50}; do [ -S "$WORK/sandbox-resource.sock" ] && break; sleep 0.05; done
echo "==> Conductor + 4 agent-type proxies up. Soak budget: ${DURATION}s, wave size: $WAVE_N"

setup_sb() { # sid cap proxy_port mode idx
    local sid="$1" cap="$2" port="$3" mode="$4" idx="$5"
    local tap="tap${idx}"
    local sb=$(( (idx % 60) * 4 )) blk=$(( idx / 60 ))
    local hip="10.$((100+blk)).1.$(( sb+1 ))" gip="10.$((100+blk)).1.$(( sb+2 ))"
    ip tuntap add "$tap" mode tap 2>/dev/null || true
    ip addr flush dev "$tap" 2>/dev/null || true
    ip addr add "$hip/30" dev "$tap" 2>/dev/null || true
    ip link set "$tap" up
    CREATED_TAPS+=("$tap")
    truncate -s 64M "$WORK/$sid.diff"
    mkfs.ext4 -q -F -O ^has_journal -b 4096 "$WORK/$sid.diff"
    mkdir -p "/sys/fs/cgroup/sandboxes/$sid"
    cat > "$WORK/$sid.yaml" <<EOF
resources:
  capacity: { cpu: 1, memory: ${cap}MiB }
  allocatable: { cpu: 1, memory: 64MiB, deflate_on_oom: true }
  control:
    cgroup_path: /sys/fs/cgroup/sandboxes/$sid
    controller: $WORK/sandbox-resource.sock
  startup: { memory: 64MiB }
network: { tap: $tap, ip: $gip/30, gateway: $hip }
boot:
  kernel: file://$VMLINUX
  runtime: file://$BUNDLE
  cmdline: "console=hvc0"
  root: { base: file://$BLK0, overlay: { diff: file://$WORK/$sid.diff } }
launch:
  exec: /usr/local/bin/node
  args: [ /opt/run_openclaw.mjs ]
  env:
    OPENAI_BASE_URL: "http://$hip:$port/v1"
    OPENAI_API_KEY: "mock-openclaw-key"
    AGENT_MODE: "$mode"
    REPO_DIR: "/workspace/repo"
  restart: never
EOF
}

launch() { # sid
    "$BIN/sandbox-ctl" run --config "$WORK/$1.yaml" --sandbox-id "$1" \
        --ch-binary "$CH_BIN" --run-root "$WORK/run" > "$WORK/$1.log" 2>&1 &
    SANDBOX_PIDS+=("$!")
}

CSV="$WORK/soak.csv"
echo "wave,launched,exit_ok,exit_fail,verified_pass,guest_sigkill,admit_rejected,paused,resumed,snap_ok,rest_ready,mem_min_mib,oom_delta,wave_s" > "$CSV"
OOM0=$(read_oom_kills)
T_END=$(( SECONDS + DURATION ))

while [ "$SECONDS" -lt "$T_END" ]; do
    WAVE=$((WAVE+1))
    w0=$(date +%s%N)
    local_pids=(); local_sids=()
    i=0
    for t in "${TYPES[@]}"; do
        IFS=: read -r name port delay mode cap <<<"$t"
        case "$name" in fast) cnt=12;; tool) cnt=6;; heavy) cnt=4;; idle) cnt=2;; esac
        for k in $(seq 1 $cnt); do
            i=$((i+1))
            sid="oc-w${WAVE}-${name}-${k}"
            setup_sb "$sid" "$cap" "$port" "$mode" "$i"
            launch "$sid"
            local_pids+=($!); local_sids+=("$sid")
            sleep 0.05
        done
    done

    # memory sampler for this wave
    mf="$WORK/wave-$WAVE.mem"
    echo "$(read_avail_mib)" > "$mf"
    ( min=$(read_avail_mib); echo "$min" > "$mf"
      while :; do a=$(read_avail_mib); if [ "$a" -lt "$min" ]; then min=$a; echo "$min" > "$mf"; fi; sleep 0.2; done ) &
    sampler=$!

    # lifecycle churn: pause/resume sockets of the long-lived types mid-wave
    sleep 3
    paused=0; resumed=0
    for sid in "${local_sids[@]}"; do
        case "$sid" in *-heavy-*|*-idle-*|*-tool-*) ;; *) continue;; esac
        sock="$WORK/run/$sid/ch.sock"
        [ -S "$sock" ] || continue
        curl -sf -X PUT --unix-socket "$sock" http://ch/api/v1/vm.pause >/dev/null 2>&1 && paused=$((paused+1))
    done
    sleep 2
    for sid in "${local_sids[@]}"; do
        case "$sid" in *-heavy-*|*-idle-*|*-tool-*) ;; *) continue;; esac
        sock="$WORK/run/$sid/ch.sock"
        [ -S "$sock" ] || continue
        curl -sf -X PUT --unix-socket "$sock" http://ch/api/v1/vm.resume >/dev/null 2>&1 && resumed=$((resumed+1))
    done

    # snapshot/restore churn every CHURN_EVERY waves on the slow-type fleet
    # (fast-type sandboxes have usually exited by churn time)
    snap_ok=0; rest_ready=0
    declare -A SREF=()
    # disk guard: each churn wave stages ~4 × 1.6 GiB of snapshot artifacts;
    # ENOSPC here degrades running guests (vdb write errors), not just churn
    free_kb=$(df --output=avail -k /var/tmp | tail -1)
    if [ $(( WAVE % CHURN_EVERY )) -eq 0 ] && [ "$SECONDS" -lt "$T_END" ] && [ "$free_kb" -gt 8388608 ]; then
        for k in 1 2 3 4; do
            sid="oc-w${WAVE}-heavy-${k}"
            [ -S "$WORK/run/$sid/ch.sock" ] || continue
            mkdir -p "$WORK/snapshots/$sid"
            if timeout 30 "$BIN/sandbox-ctl" snapshot --sandbox-id "$sid" --run-root "$WORK/run" \
                --output "$WORK/snapshots/$sid" > "$WORK/$sid.snap.log" 2>&1; then
                snap_ok=$((snap_ok+1))
                SREF[$k]=$(grep -oE '/[^ ]+\.snapshot' "$WORK/$sid.snap.log" | head -1)
            else
                echo "  ! snapshot of $sid failed/timed out" >&2
            fi
        done
        rest_pids=()
        for k in 1 2 3 4; do
            [ -n "${SREF[$k]:-}" ] || continue
            sid="oc-w${WAVE}-rest-${k}"; rtap="tapr${WAVE}-${k}"; rdiff="$WORK/$sid.diff"
            ip tuntap add "$rtap" mode tap 2>/dev/null || true
            ip addr flush dev "$rtap" 2>/dev/null || true
            ip addr add "10.220.$((WAVE % 250)).$(( 2*k+1 ))/31" dev "$rtap" 2>/dev/null || true
            ip link set "$rtap" up
            CREATED_TAPS+=("$rtap")
            truncate -s 64M "$rdiff"; mkfs.ext4 -q -F -O ^has_journal -b 4096 "$rdiff"
            cat > "$WORK/$sid.yaml" <<EOF
resources:
  capacity: { cpu: 1, memory: 512MiB }
  allocatable: { cpu: 1, memory: 64MiB }
network: { tap: $rtap }
boot:
  kernel: file://$VMLINUX
  runtime: file://$BUNDLE
  root: { overlay: { diff: file://$rdiff } }
EOF
            rm -f "$WORK/$sid.ready"
            "$BIN/sandbox-ctl" run --restore "${SREF[$k]}" --config "$WORK/$sid.yaml" \
                --sandbox-id "$sid" --ch-binary "$CH_BIN" --run-root "$WORK/run" --ready-fd 3 \
                > "$WORK/$sid.log" 2>&1 3> "$WORK/$sid.ready" &
            rest_pids+=("$!")
            SANDBOX_PIDS+=("$!")
        done
        for idx in "${!rest_pids[@]}"; do
            k=$((idx+1)); sid="oc-w${WAVE}-rest-$((idx+1))"
            dl=$(( SECONDS + 30 ))
            # abort early if the restore process exited (failure) instead of
            # burning the full 30s timeout per dead restore
            until [ -s "$WORK/$sid.ready" ] || ! kill -0 "${rest_pids[$idx]}" 2>/dev/null || [ "$SECONDS" -ge "$dl" ]; do
                sleep 0.1
            done
            [ -s "$WORK/$sid.ready" ] && rest_ready=$((rest_ready+1)) || echo "  ! restore $sid failed to reach ready state" >&2
        done
        # reap restored sessions (they continue their interrupted turn loop)
        rdl=$(( SECONDS + 40 ))
        for rp in "${rest_pids[@]:-}"; do
            while kill -0 "$rp" 2>/dev/null && [ "$SECONDS" -lt "$rdl" ]; do sleep 0.2; done
            kill -9 "$rp" 2>/dev/null || true
            wait "$rp" 2>/dev/null || true
        done
        unset rest_pids
        # churn consumed the snapshot artifacts; free the disk for the next wave
        rm -rf "$WORK/snapshots"/* 2>/dev/null || true
    elif [ $(( WAVE % CHURN_EVERY )) -eq 0 ]; then
        echo "  ! churn wave $WAVE skipped: only $(( free_kb / 1048576 )) GiB free on /var/tmp" >&2
    fi

    # non-blocking wave drain with real timeout: poll liveness, then reap
    wtimeout=$(( SECONDS + 150 ))
    while :; do
        alive=0
        for p in "${local_pids[@]}"; do
            kill -0 "$p" 2>/dev/null && alive=$((alive+1))
        done
        [ "$alive" -eq 0 ] && break
        if [ "$SECONDS" -ge "$wtimeout" ]; then
            echo "  ! wave $WAVE drain timeout with $alive sandboxes still alive; SIGKILLing stragglers" >&2
            for p in "${local_pids[@]}"; do kill -9 "$p" 2>/dev/null || true; done
            break
        fi
        sleep 0.2
    done
    for p in "${local_pids[@]}"; do wait "$p" 2>/dev/null || true; done
    kill "$sampler" 2>/dev/null || true; wait "$sampler" 2>/dev/null || true

    exit_ok=0; exit_fail=0; vpass=0; sigkill=0; arej=0
    for sid in "${local_sids[@]}"; do
        log="$WORK/$sid.log"
        grep -q "Verdict: PASS" "$log" 2>/dev/null && vpass=$((vpass+1))
        grep -q "signal=9\|code=137" "$log" 2>/dev/null && sigkill=$((sigkill+1))
        grep -q "admit rejected" "$log" 2>/dev/null && arej=$((arej+1))
    done
    for p in "${local_pids[@]}"; do wait "$p" 2>/dev/null && exit_ok=$((exit_ok+1)) || exit_fail=$((exit_fail+1)); done

    w1=$(date +%s%N)
    wave_s=$(( (w1 - w0) / 1000000 ))
    mem_min=$(cat "$mf" 2>/dev/null || echo "$(read_avail_mib)")
    oom_now=$(read_oom_kills)
    echo "$WAVE,${#local_sids[@]},$exit_ok,$exit_fail,$vpass,$sigkill,$arej,$paused,$resumed,$snap_ok,$rest_ready,$mem_min,$((oom_now-OOM0)),$wave_s" >> "$CSV"
    rm -f "$WORK"/oc-w${WAVE}-*.diff "$WORK"/oc-w${WAVE}-*.yaml 2>/dev/null || true
    SANDBOX_PIDS=()
    echo "  wave $WAVE: $vpass/${#local_sids[@]} verified | pause $paused/resume $resumed | snap $snap_ok/restore $rest_ready | mem_min ${mem_min}MiB | ${wave_s}ms"
    # stop launching new waves once the budget is exhausted
    [ "$SECONDS" -lt "$T_END" ] || break
done

