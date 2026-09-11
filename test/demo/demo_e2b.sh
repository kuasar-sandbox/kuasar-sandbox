#!/usr/bin/env bash
#
# demo_e2b.sh — end-to-end demo of the e2b-compatible sandbox host, driven by the
# UNMODIFIED e2b Python SDK (the exact version is in requirements.txt). The storage tier
# (content store + L1 cache + image registry) is persistent prep — run demo_prep.sh
# ONCE first; this script starts only the per-run pieces (orchestrator + eBPF switch)
# and runs the flow:
#
#   Template().from_image(ref).copy(…).run_cmd(…).set_start_cmd(…).build()
#                                        → the node runs the 3-phase in-sandbox build
#                                          (A pull+flatten · B COPY/RUN/ENV/WORKDIR via
#                                          envd · C startCmd→snapshot); NO client docker
#   Sandbox.create(template)             → restore the snapshot into a real microVM
#   sbx.commands.run(...)                → run commands in the guest (through the proxy)
#   port forward + egress                → reach the build's start_cmd server via floatingip
#                                          + guest→internet (NAT)
#   sbx.pause() / Sandbox.connect(id)    → snapshot / restore (resume = connect)
#   export-sandbox --to-template         → fork the paused state into a reusable template
#   connect(id, headers=migration)       → one-call migrate (auto import + resume)
#   sbx.kill()                           → lifecycle
#
# The SDK reaches this node exactly as it reaches e2b.dev: control plane at
# https://api.<domain>, data plane at https://<port>-<sid>.<domain>. We resolve those
# locally (/etc/hosts + a demo-CA-signed *.<domain> cert; SSL_CERT_FILE so httpx trusts
# it) and point the SDK with E2B_DOMAIN / E2B_API_KEY. Nothing about the SDK changes.
#
# Requires: demo_prep.sh already run; e2b Python SDK; systemd+root; /dev/kvm; openssl,
# a pinned Python environment, ip, curl, sqlite3, iptables; the built
# kernel+runtime EROFS in bin; and both loopback addresses on TCP :443.

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=demo_common.sh
. "$SCRIPT_DIR/demo_common.sh"

demo_select_owner
demo_default_data_dir
demo_validate_data_path
demo_init_data_dir

BIN="${BIN:-$REPO_ROOT/bin}"
case "$BIN" in /*) ;; *) demo_die "BIN must be an absolute path across sudo: $BIN" ;; esac
PYTHON_BIN="${PYTHON_BIN:-}"
case "$PYTHON_BIN" in /*) [ -x "$PYTHON_BIN" ] || demo_die "PYTHON_BIN is not executable: $PYTHON_BIN" ;; *)
    demo_die "set PYTHON_BIN to the absolute python path in the pinned Demo virtual environment"
esac

HANDOFF="$DEMO_DATA_DIR/prep.env"
demo_validate_handoff "$HANDOFF"
DEMO_LOCK_FD=200
exec 200<>"$DEMO_DATA_DIR/.lock"
flock -sn "$DEMO_LOCK_FD" || demo_die "preparation is being stopped, reset, or changed under $DEMO_DATA_DIR"
# prep.env is root-owned mode 0600. Its values were emitted with Bash %q.
# shellcheck disable=SC1090
. "$HANDOFF"
VGW_ACCESS_KEY_VALUE="${VGW_ACCESS_KEY:-}"
VGW_SECRET_KEY_VALUE="${VGW_SECRET_KEY:-}"
unset VGW_ACCESS_KEY VGW_SECRET_KEY
demo_require_single_line REGISTRY "${REGISTRY:-}"
demo_require_yaml_single_quoted VGW_ACCESS_KEY "$VGW_ACCESS_KEY_VALUE"
demo_require_yaml_single_quoted VGW_SECRET_KEY "$VGW_SECRET_KEY_VALUE"
demo_require_yaml_single_quoted VGW_BUCKET "${VGW_BUCKET:-}"
demo_require_yaml_single_quoted VGW_REGION "${VGW_REGION:-}"
case "$STORE_SOCK" in "$DEMO_DATA_DIR"/run/*) ;; *) demo_die "invalid Store socket from preparation: $STORE_SOCK" ;; esac
case "$CACHE_SOCK" in "$DEMO_DATA_DIR"/run/*) ;; *) demo_die "invalid Cache socket from preparation: $CACHE_SOCK" ;; esac
case "$CACHE_HEALTH_SOCK" in "$DEMO_DATA_DIR"/run/*) ;; *) demo_die "invalid Cache health socket from preparation: $CACHE_HEALTH_SOCK" ;; esac
case "$BASE_REF" in *@sha256:????????????????????????????????????????????????????????????????) ;; *) demo_die "prepared base reference is not immutable: $BASE_REF" ;; esac
if [ -n "${REGISTRY_AUTH_FILE:-}" ]; then
    case "$REGISTRY_AUTH_FILE" in "$DEMO_DATA_DIR"/docker/*/config.json) ;; *) demo_die "invalid registry auth path from preparation" ;; esac
    demo_validate_handoff "$REGISTRY_AUTH_FILE"
fi

DOMAIN="${DOMAIN:-sandboxes.demo.local}"
[[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || demo_die "DOMAIN must be a lowercase DNS name"
TLS_PORT="${TLS_PORT:-443}"
[ "$TLS_PORT" = 443 ] || demo_die "the E2B_DOMAIN Demo requires TLS_PORT=443"
CONTROL_IP=127.0.0.1
DATA_IP=127.0.0.2
RUN_KEY="${DEMO_RUN_ID:-$(tr -d '-' </proc/sys/kernel/random/uuid | cut -c1-10)}"
[[ "$RUN_KEY" =~ ^[a-z0-9]{6,12}$ ]] || demo_die "DEMO_RUN_ID must contain 6-12 lowercase letters or digits"
SWITCH="${SWITCH:-k${RUN_KEY:0:8}}"
SW_NETNS="${SW_NETNS:-kdns-$RUN_KEY}"
SW_MGMT="${SW_MGMT:-kdm$RUN_KEY}"
# Connector appends "-dummy" (and port suffixes) to this name. Reserve those
# six bytes within Linux's 15-byte interface-name limit before host setup.
[[ "$SWITCH" =~ ^[a-zA-Z0-9_.-]{1,9}$ ]] || demo_die "switch name must be 1-9 safe characters: $SWITCH"
[[ "$SW_NETNS" =~ ^[a-zA-Z0-9_.-]+$ ]] || demo_die "unsafe network namespace name: $SW_NETNS"
[[ "$SW_MGMT" =~ ^[a-zA-Z0-9_.-]{1,15}$ ]] || demo_die "management interface name must be 1-15 safe characters"
FIP_CIDR="${FIP_CIDR:-100.100.96.0/20}"
FIP_BASE="${FIP_CIDR%/*}"
GUEST_DNS="${GUEST_DNS:-169.254.169.253}"; HOST_DNS=""
MGMT_VIP="169.254.169.254"; SW_MGMT_ADDR="169.254.1.0/31"
WORK="$DEMO_DATA_DIR/runs/$RUN_KEY"
[ ! -e "$WORK" ] || demo_die "run directory already exists; choose another DEMO_RUN_ID: $WORK"
RUN_ROOT="/run/kd-$RUN_KEY"
if [ -e "$RUN_ROOT" ] || [ -L "$RUN_ROOT" ]; then
    demo_die "socket alias already exists; choose another DEMO_RUN_ID: $RUN_ROOT"
fi
RESULT_DIR="$DEMO_DATA_DIR/results/$RUN_KEY"
if [ -n "${DEMO_KEEP:-}" ] && [ -e "$RESULT_DIR" ]; then
    demo_die "result directory already exists; refusing to overwrite it: $RESULT_DIR"
fi
install -d -m 0700 "$DEMO_DATA_DIR/runs" "$WORK" "$WORK/run" "$WORK/lib" "$WORK/home" "$WORK/saved" "$WORK/logs"
CLI_ENV_FILE="$WORK/cli.env"
SIDS_FILE="$WORK/sandbox-ids"
UNIT_DIR=/run/systemd/system
RUNNER_TEMPLATE="kuasar-demo-${RUN_KEY}-runner@.service"
BUILDER_TEMPLATE="kuasar-demo-${RUN_KEY}-builder@.service"
UNIT_FILES=("$RUNNER_TEMPLATE" "$BUILDER_TEMPLATE" sandbox-runner.slice sandbox-builder.slice)
HOST_MARKER="kuasar-demo-$RUN_KEY"

c_hd=$'\e[1;36m'; c_cmd=$'\e[1;33m'; c_ok=$'\e[1;32m'; c_dim=$'\e[2m'; c_off=$'\e[0m'
[ -t 1 ] || { c_hd=; c_cmd=; c_ok=; c_dim=; c_off=; }
step=0
banner() { step=$((step+1)); echo; echo "${c_hd}══════ [$step] $* ══════${c_off}"; }
say()  { echo "${c_dim}  · $*${c_off}"; }
ok()   { echo "${c_ok}  ✓ $*${c_off}"; }
pause() {
    if [ -n "${DEMO_PAUSE:-}" ]; then
        printf '%s' "${c_dim}  ⏎ to continue…${c_off}"
        read -r _ </dev/tty 2>/dev/null || true
    fi
}
die()  { echo $'\e[1;31m'"  ✗ $*"$'\e[0m' >&2; exit 1; }

# ---- run ownership + teardown ---------------------------------------------
declare -a PROCESS_NAMES=() PROCESS_PIDS=() PROCESS_STARTS=() PROCESS_EXES=() NAT_ADDED=()
declare -A UNIT_HASHES=()
SWITCH_OWNED=0; NETNS_OWNED=0; HOSTS_OWNED=0; AMBIGUOUS_SWITCH=0
RUN_ALIAS_OWNED=0
AK=""; API_SECRET=""; MK=""; ENC=""; TOK=""; MIG_TOKEN=""

proc_start_time() {
    local stat rest; local -a fields
    IFS= read -r stat <"/proc/$1/stat" || return 1
    rest="${stat##*) }"; read -r -a fields <<<"$rest"
    [ "${#fields[@]}" -ge 20 ] || return 1
    printf '%s\n' "${fields[19]}"
}
start_process() { # name log executable args...
    local name="$1" log="$2" exe pid start current_exe index; shift 2
    exe="$(readlink -f "$1")"
    setsid "$@" >"$log" 2>&1 </dev/null 200>&- & pid=$!
    sleep 0.1
    kill -0 "$pid" 2>/dev/null || {
        [ "$name" != conductor ] || capture_unit_files
        wait "$pid" 2>/dev/null || true
        die "$name exited during startup (see $log)"
    }
    start="$(proc_start_time "$pid")" || die "cannot record $name process identity"
    current_exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
    [ -n "$current_exe" ] || die "cannot record $name launch executable"
    index="${#PROCESS_PIDS[@]}"
    PROCESS_NAMES+=("$name"); PROCESS_PIDS+=("$pid"); PROCESS_STARTS+=("$start"); PROCESS_EXES+=("$current_exe")
    for _ in $(seq 1 300); do
        [ "$(proc_start_time "$pid" 2>/dev/null || true)" = "$start" ] \
            || die "$name exited or changed process identity before exec"
        current_exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
        [ -n "$current_exe" ] || die "$name exited before exec"
        PROCESS_EXES[index]="$current_exe"
        [ "$current_exe" != "$exe" ] || break
        sleep 0.1
    done
    [ "$current_exe" = "$exe" ] || die "$name did not execute its requested program within 30 seconds"
    STARTED_PID="$pid"
}
stop_process_at() {
    local index="$1" pid="${PROCESS_PIDS[index]}" start="${PROCESS_STARTS[index]}" exe="${PROCESS_EXES[index]}" sid
    kill -0 "$pid" 2>/dev/null || return 0
    if [ "$(proc_start_time "$pid" 2>/dev/null || true)" != "$start" ] \
        || [ "$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)" != "$exe" ]; then
        echo "  ! ${PROCESS_NAMES[index]} pid identity changed; refusing to signal pid $pid" >&2
        return 1
    fi
    sid="$(ps -o sid= -p "$pid" 2>/dev/null | tr -d ' ')"
    if [ "$sid" = "$pid" ]; then kill -TERM -- "-$pid" 2>/dev/null; else kill -TERM "$pid" 2>/dev/null; fi
    for _ in $(seq 1 100); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
    if kill -0 "$pid" 2>/dev/null; then
        if [ "$sid" = "$pid" ]; then kill -KILL -- "-$pid" 2>/dev/null; else kill -KILL "$pid" 2>/dev/null; fi
    fi
    wait "$pid" 2>/dev/null || true
}
capture_unit_files() {
    local name path
    for name in "${UNIT_FILES[@]}"; do
        path="$UNIT_DIR/$name"
        if [ -f "$path" ] && [ ! -L "$path" ]; then UNIT_HASHES["$path"]="$(sha256sum "$path" | awk '{print $1}')"; fi
    done
}
stop_owned_units() {
    local prefix pattern unit failed=0
    for prefix in "${RUNNER_TEMPLATE%@.service}@" "${BUILDER_TEMPLATE%@.service}@"; do
        pattern="${prefix}*.service"
        while IFS= read -r unit; do
            [ -n "$unit" ] || continue
            case "$unit" in
                "$prefix"*.service) systemctl stop "$unit" >/dev/null 2>&1 || failed=1 ;;
                *) echo "  ! refusing unexpected unit $unit" >&2; failed=1 ;;
            esac
        done < <(systemctl list-units --all --type=service --no-legend --plain "$pattern" 2>/dev/null | awk '{print $1}')
        if systemctl list-units --all --type=service --no-legend --plain "$pattern" 2>/dev/null \
            | awk '$4 != "inactive" && $4 != "failed" { found=1 } END { exit !found }'; then
            echo "  ! owned unit instances remain for $pattern" >&2
            failed=1
        fi
    done
    return "$failed"
}
hosts_add() { # ip fqdn
    local ip="$1" fqdn="$2"
    if grep -Eq "(^|[[:space:]])${fqdn//./\\.}([[:space:]]|$)" /etc/hosts; then
        die "/etc/hosts already contains $fqdn; refusing to replace a foreign mapping"
    fi
    printf '%s %s # %s\n' "$ip" "$fqdn" "$HOST_MARKER" >>/etc/hosts
    HOSTS_OWNED=1
}
cleanup_sandboxes() {
    [ -n "$AK" ] && [ -s "$SIDS_FILE" ] || return 0
    HOME="$WORK/home" E2B_DOMAIN="$DOMAIN" E2B_API_KEY="$AK" \
        SSL_CERT_FILE="$WORK/demo-ca.crt" REQUESTS_CA_BUNDLE="$WORK/demo-ca.crt" NO_PROXY='*' \
        "$PYTHON_BIN" - "$SIDS_FILE" <<'PY' >/dev/null 2>&1 || true
from e2b import Sandbox
import pathlib, sys
for sid in dict.fromkeys(pathlib.Path(sys.argv[1]).read_text().splitlines()):
    try:
        Sandbox.connect(sid).kill()
    except Exception:
        pass
PY
}
keep_safe_logs() {
    [ -n "${DEMO_KEEP:-}" ] || return 0
    local output="$RESULT_DIR" log secret sensitive
    install -d -m 0700 "$DEMO_DATA_DIR/results" "$output"
    for log in "$WORK"/*.log "$WORK/logs"/*.log; do
        [ -f "$log" ] || continue; sensitive=0
        for secret in "$AK" "$API_SECRET" "$MK" "$ENC" "$TOK" "$MIG_TOKEN" "$VGW_SECRET_KEY_VALUE"; do
            if [ "${#secret}" -ge 8 ] && grep -Fq -- "$secret" "$log"; then sensitive=1; break; fi
        done
        if [ "$sensitive" -eq 0 ]; then install -m 0600 "$log" "$output/$(basename "$log")"; fi
    done
    echo "  - retained non-secret logs: $output"
}
cleanup() {
    local status=$? index path current cleanup_failed="$AMBIGUOUS_SWITCH" switch_gone=1
    trap - EXIT INT TERM
    set +e
    echo; echo "${c_dim}── teardown (persistent preparation remains running) ──${c_off}"
    cleanup_sandboxes
    stop_owned_units || cleanup_failed=1
    for ((index=${#PROCESS_PIDS[@]}-1; index>=0; index--)); do stop_process_at "$index" || cleanup_failed=1; done
    for current in "${NAT_ADDED[@]:-}"; do case "$current" in
        forward-out) iptables -D FORWARD -o "$SW_MGMT" -m state --state RELATED,ESTABLISHED -m comment --comment "$HOST_MARKER" -j ACCEPT 2>/dev/null || true
            iptables -C FORWARD -o "$SW_MGMT" -m state --state RELATED,ESTABLISHED -m comment --comment "$HOST_MARKER" -j ACCEPT 2>/dev/null && cleanup_failed=1 ;;
        forward-in) iptables -D FORWARD -i "$SW_MGMT" -m comment --comment "$HOST_MARKER" -j ACCEPT 2>/dev/null || true
            iptables -C FORWARD -i "$SW_MGMT" -m comment --comment "$HOST_MARKER" -j ACCEPT 2>/dev/null && cleanup_failed=1 ;;
        masquerade) iptables -t nat -D POSTROUTING -s "$FIP_CIDR" -m comment --comment "$HOST_MARKER" -j MASQUERADE 2>/dev/null || true
            iptables -t nat -C POSTROUTING -s "$FIP_CIDR" -m comment --comment "$HOST_MARKER" -j MASQUERADE 2>/dev/null && cleanup_failed=1 ;;
        dns-*) iptables -t nat -D PREROUTING -d "$GUEST_DNS" -p "${current#dns-}" --dport 53 -m comment --comment "$HOST_MARKER" -j DNAT --to-destination "${HOST_DNS}:53" 2>/dev/null || true
            iptables -t nat -C PREROUTING -d "$GUEST_DNS" -p "${current#dns-}" --dport 53 -m comment --comment "$HOST_MARKER" -j DNAT --to-destination "${HOST_DNS}:53" 2>/dev/null && cleanup_failed=1 ;;
    esac; done
    if [ "$SWITCH_OWNED" -eq 1 ]; then
        "$BIN/connector-ctl" vswitch stop "$SWITCH" --force >/dev/null 2>&1 || true
        "$BIN/connector-ctl" vswitch status "$SWITCH" >/dev/null 2>&1; switch_status=$?
        if [ "$switch_status" -ne 3 ]; then echo "  ! owned vSwitch remains: $SWITCH" >&2; cleanup_failed=1; switch_gone=0; fi
    fi
    if [ "$NETNS_OWNED" -eq 1 ] && [ "$switch_gone" -eq 1 ]; then
        ip netns del "$SW_NETNS" 2>/dev/null || true
        if ip netns list | awk '{print $1}' | grep -Fxq "$SW_NETNS"; then echo "  ! owned netns remains: $SW_NETNS" >&2; cleanup_failed=1; fi
    fi
    if [ "$HOSTS_OWNED" -eq 1 ]; then
        sed -i "/[[:space:]]# $HOST_MARKER\$/d" /etc/hosts 2>/dev/null || cleanup_failed=1
        grep -Fq "# $HOST_MARKER" /etc/hosts && cleanup_failed=1
    fi
    for path in "${!UNIT_HASHES[@]}"; do
        if [ ! -f "$path" ] || [ -L "$path" ]; then continue; fi
        current="$(sha256sum "$path" | awk '{print $1}')"
        if [ "$current" = "${UNIT_HASHES[$path]}" ]; then rm -f -- "$path" || cleanup_failed=1; else echo "  ! unit changed after creation; preserved: $path" >&2; cleanup_failed=1; fi
    done
    systemctl daemon-reload >/dev/null 2>&1
    rm -f -- "$CLI_ENV_FILE"
    keep_safe_logs
    if [ "$cleanup_failed" -eq 0 ] && [ "$RUN_ALIAS_OWNED" -eq 1 ]; then
        demo_remove_run_alias "$RUN_ROOT" "$WORK/run" || cleanup_failed=1
    fi
    if [ "$cleanup_failed" -eq 0 ]; then
        case "$WORK/" in "$DEMO_DATA_DIR"/runs/"$RUN_KEY"/) rm -rf -- "$WORK" || cleanup_failed=1 ;; *) echo "  ! unsafe work path preserved: $WORK" >&2; cleanup_failed=1 ;; esac
    else
        echo "  ! cleanup was incomplete; preserved private diagnostics: $WORK" >&2
    fi
    if [ "$status" -eq 0 ] && [ "$cleanup_failed" -ne 0 ]; then status=1; fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# ---- prerequisites --------------------------------------------------------
for b in node-ctl e2b-key-ctl connector-ctl cloud-hypervisor; do [ -x "$BIN/$b" ] || die "missing $BIN/$b — use one matching source build or release source-set"; done
if [ ! -f "$BIN/vmlinux" ] || [ ! -f "$BIN/sandbox-runtime.bundle" ]; then die "missing kernel/runtime EROFS in $BIN"; fi
[ -S "${STORE_SOCK:-}" ] || die "store socket $STORE_SOCK absent — run demo_prep.sh"
[ -S "${CACHE_SOCK:-}" ] || die "cache socket $CACHE_SOCK absent — run demo_prep.sh"
if [ -z "${REGISTRY:-}" ] || [ -z "${BASE_REF:-}" ]; then die "REGISTRY/BASE_REF not set — run demo_prep.sh"; fi
for t in openssl ip curl sqlite3 iptables flock sha256sum setsid systemctl timeout; do command -v "$t" >/dev/null 2>&1 || die "$t not on PATH"; done
"$PYTHON_BIN" - <<'PY' || die "install the exact SDK from test/demo/requirements.txt into PYTHON_BIN's environment"
from importlib.metadata import version
assert version("e2b") == "2.25.1", version("e2b")
import e2b  # noqa: F401
PY
[ -d /run/systemd/system ] || die "systemd is not PID1"
if [ ! -e /dev/kvm ] || [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; then die "/dev/kvm not available (rw)"; fi
echo "${c_hd}"'
   kuasar-sandbox · e2b-compatible microVM host — Python SDK demo
   (everything below is driven by the UNMODIFIED e2b Python SDK; storage tier reused from demo_prep.sh)
'"${c_off}"

# py — run a Python SDK snippet with the node env (control + data via separate TLS listeners).
py() { HOME="$WORK/home" E2B_DOMAIN="$DOMAIN" E2B_API_KEY="$AK" \
       SSL_CERT_FILE="$WORK/demo-ca.crt" REQUESTS_CA_BUNDLE="$WORK/demo-ca.crt" NO_PROXY='*' "$PYTHON_BIN" - "$@"; }

# Reject every pre-existing host object before creating anything outside WORK.
[ -n "${DEMO_QUICKSTART:-}" ] || [ -n "${VGW_ENDPOINT:-}" ] \
    || die "the complete Demo requires versitygw for its promised COPY step; prepare it or use DEMO_QUICKSTART=1"
"$BIN/cache-ctl" ping --endpoint "unix://$CACHE_HEALTH_SOCK" >/dev/null 2>&1 \
    || die "prepared Cache health endpoint is not serving"
[ "$(cat /proc/sys/net/ipv4/ip_forward)" = 1 ] \
    || die "net.ipv4.ip_forward must already be 1; the Demo will not change a host-global setting"
"$PYTHON_BIN" - "$FIP_CIDR" "$STORE_SOCK" "$CACHE_SOCK" "$WORK/node-ctl.socket" "$WORK/proxy-stats.sock" "$WORK/proxy-routes.shm" <<'PY' \
    || die "invalid CIDR or Unix socket path; choose a shorter DEMO_DATA_DIR"
import ipaddress, sys
network = ipaddress.ip_network(sys.argv[1], strict=True)
assert network.version == 4 and str(network.network_address) == sys.argv[1].split("/", 1)[0]
for path in sys.argv[2:]:
    assert len(path.encode()) <= 107, (len(path.encode()), path)
PY
for name in "${UNIT_FILES[@]}"; do
    if systemctl cat "$name" >/dev/null 2>&1; then die "systemd unit $name already exists; refusing to replace it"; fi
    for unit_root in /etc/systemd/system /run/systemd/system /usr/local/lib/systemd/system /usr/lib/systemd/system /lib/systemd/system; do
        [ ! -e "$unit_root/$name" ] || die "$unit_root/$name already exists; refusing to replace it"
    done
done
if systemctl list-units --all --type=service --no-legend --plain \
    'sandbox-runner@*.service' 'sandbox-builder@*.service' 2>/dev/null | grep -q .; then
    die "standard sandbox runner/builder instances already exist; run the Demo on an unused host"
fi
for endpoint in "$CONTROL_IP:$TLS_PORT" "$DATA_IP:$TLS_PORT"; do
    host="${endpoint%:*}"; port="${endpoint##*:}"
    # shellcheck disable=SC2016 # $1/$2 belong to the inner shell.
    if timeout 1 bash -c 'exec 9<>"/dev/tcp/$1/$2"' _ "$host" "$port" 2>/dev/null; then
        die "$endpoint is already in use; refusing to replace a foreign listener"
    fi
done
set +e
SWITCH_STATUS="$("$BIN/connector-ctl" vswitch status "$SWITCH" 2>&1)"; SWITCH_STATUS_CODE=$?
set -e
case "$SWITCH_STATUS_CODE" in
    3) ;;
    0|4) die "vSwitch $SWITCH already exists; refusing to stop or replace it" ;;
    *) die "cannot establish that vSwitch $SWITCH is absent: $SWITCH_STATUS" ;;
esac
if ip netns list | awk '{print $1}' | grep -Fxq "$SW_NETNS"; then
    die "network namespace $SW_NETNS already exists; refusing to delete or adopt it"
fi
if ip link show dev "$SW_MGMT" >/dev/null 2>&1; then
    die "network link $SW_MGMT already exists; refusing to delete or adopt it"
fi
if { iptables -S; iptables -t nat -S; } 2>/dev/null | grep -Fq -- "$HOST_MARKER"; then
    die "iptables already contains the run marker $HOST_MARKER; refusing to adopt or replace those rules"
fi
demo_create_run_alias "$RUN_ROOT" "$WORK/run" || die "cannot reserve the Demo socket alias"
RUN_ALIAS_OWNED=1

# ===========================================================================
banner "Per-run node stack (orchestrator + eBPF switch; storage tier already up)"
# ---------------------------------------------------------------------------
say "overlay diff_template — pre-formatted empty ext4 seeding each cold boot's writable upper"
MKFS_EXT4="$(command -v mkfs.ext4 || echo /sbin/mkfs.ext4)"; [ -x "$MKFS_EXT4" ] || die "mkfs.ext4 not found"
OVL="$WORK/overlay-1G.ext4"; truncate -s 1G "$OVL"; "$MKFS_EXT4" -F -q -b 4096 "$OVL" >/dev/null 2>&1 || die "mkfs.ext4"
say "builder diff_template — build-sandbox writable disk (pull cache + steps delta + export scratch; sparse)"
# Sparse, so the cap is free until written. The FULL build re-flattens the base
# again in phase B (steps export) on top of phase A's pull+flatten, so headroom
# beyond the compact default base matters: blobs + unpacked tree + two EROFS
# outputs + mkfs chunk staging can coexist. The 24 GiB sparse cap also leaves
# room for an explicitly selected, larger digest-pinned base.
BLD="$WORK/builder-24G.ext4"; truncate -s 24G "$BLD"; "$MKFS_EXT4" -F -q -b 4096 "$BLD" >/dev/null 2>&1 || die "mkfs.ext4 (builder)"

say "local demo CA + *.$DOMAIN server cert (SDK trusts the CA via SSL_CERT_FILE; data plane is https)"
cat > "$WORK/ca.cnf" <<EOF
[req]
distinguished_name=dn
prompt=no
x509_extensions=v3_ca
[dn]
CN=Kuasar Demo CA
[v3_ca]
basicConstraints=critical,CA:TRUE,pathlen:0
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid:always,issuer
EOF
openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$WORK/demo-ca.key" -out "$WORK/demo-ca.crt" -days 2 \
    -config "$WORK/ca.cnf" >/dev/null 2>&1 || die "openssl demo CA"
openssl req -newkey rsa:2048 -nodes -keyout "$WORK/tls.key" -out "$WORK/tls.csr" \
    -subj "/CN=*.$DOMAIN" >/dev/null 2>&1 || die "openssl server request"
cat > "$WORK/tls.ext" <<EOF
[server_cert]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
subjectAltName=DNS:*.$DOMAIN,DNS:$DOMAIN
EOF
openssl x509 -req -in "$WORK/tls.csr" \
    -CA "$WORK/demo-ca.crt" -CAkey "$WORK/demo-ca.key" -CAcreateserial \
    -out "$WORK/tls.crt" -days 2 -sha256 \
    -extfile "$WORK/tls.ext" -extensions server_cert >/dev/null 2>&1 || die "openssl server cert"

# manifest store/cache point at the persistent UDS daemons (demo_prep.sh).
cat > "$WORK/manifest.yaml" <<EOF
manifest: { key: "" }
store: { endpoint: '$STORE_SOCK', pool: 4, timeout: 30s }
cache: { endpoint: '$CACHE_SOCK' }
chunker: { mode: cdc, cdc: { min: 128KiB, avg: 512KiB, max: 1MiB } }
crypto: { chunk: aes, manifest: aes }
EOF
INSECURE=false; [ -n "${REGISTRY_INSECURE:-}" ] && INSECURE=true
MK="$("$BIN/e2b-key-ctl" gen-key)"; ENC="$("$BIN/e2b-key-ctl" gen-key)"
# A build microVM cannot use the host's loopback address directly. Keep a local
# registry on 127.0.0.1 and let the vswitch translate the guest-visible VIP to it.
GUEST_BASE_REF="$BASE_REF"
LOCAL_REGISTRY_PORT=""
declare -a MGMT_SERVICE_ARGS=()
case "$BASE_REF" in
  127.0.0.1:*|localhost:*)
    LOCAL_REGISTRY_PORT="${REGISTRY##*:}"
    [[ "$LOCAL_REGISTRY_PORT" =~ ^[0-9]+$ ]] \
      || die "local registry must use <host>:<port>, got $REGISTRY"
    GUEST_BASE_REF="$MGMT_VIP:${BASE_REF#*:}"
    MGMT_SERVICE_ARGS+=("--mgmt-service=$MGMT_VIP:$LOCAL_REGISTRY_PORT:127.0.0.1:$LOCAL_REGISTRY_PORT")
    ;;
esac
# DEMO_MMDS=1: envd runs in FC mode + the orchestrator re-keys it via the metadata
# service (envd-enforced, defense-in-depth). Unset: envd runs non-secure, the proxy is
# the sole data-plane gate. Both modes make snapshot forks SDK-usable.
MMDS_CFG=""
if [ -n "${DEMO_MMDS:-}" ]; then
    MMDS_CFG='mmds: { enabled: true, listen: "127.0.0.1:19254" }'
    # vswitch translates the guest's management VIP to the orchestrator's
    # loopback MMDS — no iptables (the eBPF datapath does it + rewrites replies).
    MGMT_SERVICE_ARGS+=("--mgmt-service=$MGMT_VIP:80:127.0.0.1:19254")
fi
# COPY context HEAD/presigning, SDK upload and Builder download are host-side.
# The Builder streams the downloaded context into the VM. Unlike image import,
# this endpoint must stay host-reachable and does not need a guest VIP mapping.
FILES_STORAGE_CFG=""
if [ -n "${VGW_ENDPOINT:-}" ]; then
    FILES_STORAGE_CFG="  files_storage: { endpoint: '$VGW_ENDPOINT', region: '${VGW_REGION:-us-east-1}', bucket: '$VGW_BUCKET', access_key: '$VGW_ACCESS_KEY_VALUE', secret_key: '$VGW_SECRET_KEY_VALUE', force_path_style: true }"
fi
cat > "$WORK/conductor.yaml" <<EOF
api: { domain: '$DOMAIN', listen: '$CONTROL_IP:$TLS_PORT', tls: { cert: '$WORK/tls.crt', key: '$WORK/tls.key' } }
proxy: { auth: enforce }
$MMDS_CFG
encryption_key: "$ENC"
manifest_config: '$WORK/manifest.yaml'
paths: { run_root: '$RUN_ROOT', base_root: '$WORK/lib', config_socket: '$WORK/node-ctl.socket' }
units: { dir: '$UNIT_DIR', runner: '$RUNNER_TEMPLATE', builder: '$BUILDER_TEMPLATE' }
sandbox:
  network: { switch: '$SWITCH' }          # e2b defaults: ip 169.254.0.21/30 nexthop .22
  resources:
    capacity: { cpu: 2, memory: 6GiB }     # phase VM peak; Template.build resources remain admission inputs
    allocatable: { cpu: 2, memory: 4GiB }  # preserve peak headroom while retaining Balloon reclaim semantics
    watermark_high: { ratio: 0.95 }        # single-node demo leaves build working-set headroom below hard max
  boot:
    kernel: '$BIN/vmlinux'
    runtime: '$BIN/sandbox-runtime.bundle'
    overlay_diff_template: '$OVL'
builder:                                    # Template.build supplies resources; from_image names the image directly
  insecure_registry: $INSECURE
  diff_template: '$BLD'
  pull_timeout_sec: 1800                    # pull + flatten can be CPU/storage bound on a first run
  step_timeout_sec: 300
  ready_timeout_sec: 120
  total_timeout_sec: 2400
$FILES_STORAGE_CFG
checkpoint: { mode: local }
EOF
cat > "$WORK/proxy.yaml" <<EOF
config_socket: '$WORK/node-ctl.socket'
paths: { run_root: '$RUN_ROOT' }
data_listen: '$DATA_IP:$TLS_PORT'
stats_socket: '$WORK/proxy-stats.sock'
shm_path: '$WORK/proxy-routes.shm'
route_capacity: 65536
workers: 2
tls: { cert: '$WORK/tls.crt', key: '$WORK/tls.key' }
auth: enforce
park_timeout: 30s
EOF
"$BIN/node-ctl" config conductor --config "$WORK/conductor.yaml" >"$WORK/conductor.normalized.yaml" 2>"$WORK/config-validation.log" \
    || { sed 's/^/    /' "$WORK/config-validation.log" >&2; die "invalid conductor configuration"; }
"$BIN/node-ctl" config proxy --config "$WORK/proxy.yaml" >"$WORK/proxy.normalized.yaml" 2>>"$WORK/config-validation.log" \
    || { sed 's/^/    /' "$WORK/config-validation.log" >&2; die "invalid Proxy configuration"; }

# eBPF/TC switch + host NAT (per run; the storage tier is persistent, not the network).
ip netns add "$SW_NETNS" || die "create network namespace $SW_NETNS"
NETNS_OWNED=1
say "connector-ctl vswitch start — eBPF/TC switch; --mgmt-extract adds host NIC $SW_MGMT (traffic match only)"
if "$BIN/connector-ctl" vswitch start "$SWITCH" --netns="$SW_NETNS" --ports=64 --mac-addr=02:00:00:00:00:01 \
    --floating-ip-base="$FIP_BASE" --mode=tap \
    "--mgmt-extract=:$SW_MGMT:$MGMT_VIP,0.0.0.0/0" \
    "${MGMT_SERVICE_ARGS[@]}" >"$WORK/vswitch.log" 2>&1; then
    SWITCH_OWNED=1
else
    set +e; "$BIN/connector-ctl" vswitch status "$SWITCH" >/dev/null 2>&1; switch_after_failure=$?; set -e
    case "$switch_after_failure" in
        0|4)
            # A concurrent creator and a partial start are indistinguishable.
            # Preserve both objects for inspection instead of force-stopping a
            # switch whose ownership cannot be proven.
            AMBIGUOUS_SWITCH=1
            NETNS_OWNED=0
            ;;
    esac
    sed 's/^/    /' "$WORK/vswitch.log" >&2
    die "vSwitch startup failed"
fi
ip link set dev "$SW_MGMT" up || die "bring up management interface $SW_MGMT"
ip addr replace "$SW_MGMT_ADDR" dev "$SW_MGMT" || die "assign $SW_MGMT_ADDR to $SW_MGMT"
if [ "${#MGMT_SERVICE_ARGS[@]}" -gt 0 ]; then
    # Every demo management service currently targets host loopback. Packets
    # arrive through the management veth after the eBPF DNAT.
    sysctl -q -w "net.ipv4.conf.$SW_MGMT.route_localnet=1" >/dev/null \
      || die "enable route_localnet on $SW_MGMT"
fi
iptables -A FORWARD -o "$SW_MGMT" -m state --state RELATED,ESTABLISHED -m comment --comment "$HOST_MARKER" -j ACCEPT \
    && NAT_ADDED+=(forward-out)
iptables -A FORWARD -i "$SW_MGMT" -m comment --comment "$HOST_MARKER" -j ACCEPT \
    && NAT_ADDED+=(forward-in)
iptables -t nat -A POSTROUTING -s "$FIP_CIDR" -m comment --comment "$HOST_MARKER" -j MASQUERADE \
    && NAT_ADDED+=(masquerade)
HOST_DNS="$(awk '/^nameserver[[:space:]]+[0-9]+\./{print $2; exit}' /etc/resolv.conf 2>/dev/null)"
if [ -n "$HOST_DNS" ]; then for pr in udp tcp; do
    iptables -t nat -A PREROUTING -d "$GUEST_DNS" -p "$pr" --dport 53 \
        -m comment --comment "$HOST_MARKER" -j DNAT --to-destination "$HOST_DNS:53" \
        && NAT_ADDED+=("dns-$pr")
done; fi
ok "switch + NAT ready ($SW_MGMT $SW_MGMT_ADDR; guest DNS $GUEST_DNS → host ${HOST_DNS:-unrouted})"
if [ -n "$LOCAL_REGISTRY_PORT" ]; then
    curl -fs --max-time 3 --noproxy '*' -o /dev/null "http://127.0.0.1:$LOCAL_REGISTRY_PORT/v2/" \
      || die "local registry $REGISTRY is not healthy on 127.0.0.1:$LOCAL_REGISTRY_PORT"
    ok "local registry service: guest $MGMT_VIP:$LOCAL_REGISTRY_PORT → host 127.0.0.1:$LOCAL_REGISTRY_PORT"
fi
if [ -n "${DEMO_MMDS:-}" ]; then
    ok "MMDS mode: envd re-keyed via metadata service (vswitch mgmt-service $MGMT_VIP:80 → 127.0.0.1:19254)"
fi

hosts_add "$CONTROL_IP" "api.$DOMAIN"
say "node-ctl conductor serve — API-only control plane on $CONTROL_IP:$TLS_PORT"
start_process conductor "$WORK/orch.log" "$BIN/node-ctl" conductor serve --config "$WORK/conductor.yaml"
CONDUCTOR_PID="$STARTED_PID"; conductor_ready=0
for _ in $(seq 1 80); do
    capture_unit_files
    if [ -S "$WORK/node-ctl.socket" ] && curl -fsS --max-time 2 --noproxy '*' --cacert "$WORK/demo-ca.crt" \
        --resolve "api.$DOMAIN:$TLS_PORT:$CONTROL_IP" "https://api.$DOMAIN/health" >/dev/null; then
        conductor_ready=1; break
    fi
    kill -0 "$CONDUCTOR_PID" 2>/dev/null || { sed 's/^/    /' "$WORK/orch.log" >&2; die "conductor exited before readiness"; }
    sleep 0.25
done
[ "$conductor_ready" -eq 1 ] || { sed 's/^/    /' "$WORK/orch.log" >&2; die "conductor did not become ready"; }
capture_unit_files
for name in "${UNIT_FILES[@]}"; do [ -n "${UNIT_HASHES[$UNIT_DIR/$name]:-}" ] || die "conductor did not install owned unit $name"; done
ok "conductor ready: https://api.$DOMAIN on $CONTROL_IP:$TLS_PORT"

say "node-ctl proxy serve — independent data plane on $DATA_IP:$TLS_PORT"
start_process proxy "$WORK/proxy.log" "$BIN/node-ctl" proxy serve --config "$WORK/proxy.yaml"
PROXY_PID="$STARTED_PID"; proxy_ready=0
for _ in $(seq 1 120); do
    proxy_code="$(curl -sS --max-time 2 --noproxy '*' --cacert "$WORK/demo-ca.crt" \
        --resolve "readiness.$DOMAIN:$TLS_PORT:$DATA_IP" -o "$WORK/proxy-readiness.body" -w '%{http_code}' \
        "https://readiness.$DOMAIN/" 2>/dev/null || true)"
    if [ -S "$WORK/proxy-stats.sock" ] && [[ "$proxy_code" =~ ^[1-5][0-9][0-9]$ ]]; then proxy_ready=1; break; fi
    kill -0 "$PROXY_PID" 2>/dev/null || { sed 's/^/    /' "$WORK/proxy.log" >&2; die "Proxy exited before readiness"; }
    sleep 0.25
done
[ "$proxy_ready" -eq 1 ] || { sed 's/^/    /' "$WORK/proxy.log" >&2; die "Proxy did not become ready"; }
control_data_code="$(curl -sS --max-time 3 --noproxy '*' --cacert "$WORK/demo-ca.crt" \
    --resolve "49983-not-a-sandbox.$DOMAIN:$TLS_PORT:$CONTROL_IP" -o "$WORK/control-data.body" -w '%{http_code}' \
    "https://49983-not-a-sandbox.$DOMAIN/private/sandboxes/not-a-sandbox/49983/health")"
[ "$control_data_code" = 404 ] || die "conductor accepted a data-plane-shaped request (HTTP $control_data_code)"
ok "independent Proxy ready and conductor remains API-only"
pause

# ===========================================================================
banner "Onboard a tenant (ManifestKey + derived APISecret → allowlist + API key)"
# ---------------------------------------------------------------------------
REG_FLAGS=(); [ -z "${REGISTRY_AUTH_FILE:-}" ] || REG_FLAGS=(--registry-auth "$REGISTRY_AUTH_FILE")
say "allowlist the tenant manifest key + its default registry pull creds (so the node can pull $REGISTRY):"
echo "${c_cmd}  \$ node-ctl manifest-key add ${REG_FLAGS:+--registry-auth <private-config> } \$MANIFEST_KEY${c_off}"
"$BIN/node-ctl" manifest-key add --socket "$WORK/node-ctl.socket" "${REG_FLAGS[@]}" "$MK" >/dev/null || die "manifest-key add"
API_SECRET="$("$BIN/e2b-key-ctl" derive-api-secret "$MK")"
AK="$("$BIN/e2b-key-ctl" gen-apikey "$API_SECRET")"
ok "tenant ready — a per-run e2b API key was issued"
# Root-only handoff for an optional second root terminal.
cat > "$CLI_ENV_FILE" <<EOF
export E2B_DOMAIN='$DOMAIN' E2B_API_KEY='$AK'
export SSL_CERT_FILE='$WORK/demo-ca.crt' REQUESTS_CA_BUNDLE='$WORK/demo-ca.crt' NO_PROXY='*'
EOF
chmod 0600 "$CLI_ENV_FILE"
say "a second root terminal may source the private handoff while the Demo is paused: $CLI_ENV_FILE"
pause

# ===========================================================================
banner "Build a template — full pipeline (from_image + COPY + RUN + ENV + WORKDIR + startCmd→snapshot)"
# ---------------------------------------------------------------------------
# The pull runs INSIDE the build microVM (tenant network). Local registries use
# the guest-visible mgmt-service ref prepared above; third-party refs pass through.

# A tiny local build context for the COPY step: a static site the template's
# start command will serve. BUILT_MARKER threads through the whole demo — it is
# COPY'd into the image here, RUN appends to the page, and step 6 fetches it from
# the start_cmd server after a snapshot+restore (proving every phase end to end).
BUILT_MARKER="kuasar-built-$RANDOM"
mkdir -p "$WORK/ctx/site"
cat > "$WORK/ctx/site/index.html" <<HTML
<h1>kuasar build demo</h1>
<p>[COPY] this file came from the build context.</p>
HTML
echo "this file was COPY'd from the build context" > "$WORK/ctx/site/COPIED.txt"

# COPY needs builder.files_storage (versitygw, brought up by demo_prep.sh).
# The complete Demo required it during preflight. Quick Start deliberately keeps
# the shorter no-COPY path when it is absent.
HAS_COPY=False; [ -n "${VGW_ENDPOINT:-}" ] && HAS_COPY=True
COPY_DISP=""; [ "$HAS_COPY" = True ] && COPY_DISP=".copy('site','/home/user/site')"
if [ "$HAS_COPY" = True ]; then
    say "files_storage (versitygw) up — COPY context is direct-uploaded via presigned PUT, fetched on the host, then streamed into the build VM"
else
    say "Quick Start has no files_storage — its build omits COPY"
fi
say "ONE fluent build drives all three phases — A pull+flatten · B steps (COPY/RUN/ENV/WORKDIR via envd) · C startCmd→snapshot:"
echo "${c_cmd}  \$ Template().from_image('$GUEST_BASE_REF')${COPY_DISP}.run_cmd(…).set_envs(…).set_workdir(…).set_start_cmd('python3 -m http.server 8000', wait_for_timeout(2000))${c_off}"
say "on_build_logs streams the node's build journal (every phase + step) live as it runs:"
# on_build_logs prints to STDERR so the live stream shows in the terminal without
# polluting the template id captured from stdout below. With a start command the
# result is a SNAPSHOT template (kind=snp): a microVM frozen with the start
# command left running under envd, so create is a restore, not a cold boot.
BUILD_RESULT="$(py "$WORK/ctx" "$GUEST_BASE_REF" "$BUILT_MARKER" "$HAS_COPY" "$RUN_KEY" <<'PY'
import json, os, sys
from e2b import Template, wait_for_timeout
context, base_ref, marker, has_copy, run_key = sys.argv[1:]
os.chdir(context)                                       # COPY src paths resolve from here
def show(e):
    msg = e.message.rstrip()
    if msg:
        print("    · " + msg, file=sys.stderr, flush=True)
tpl = Template().from_image(base_ref)
if has_copy == "True":
    tpl = tpl.copy("site", "/home/user/site", user="1000:1000")   # B: COPY into the image, owned by the e2b user
tpl = (tpl
    .run_cmd("mkdir -p /home/user/site && echo '<p>[RUN] " + marker + " - appended in the build sandbox via envd.</p>' >> /home/user/site/index.html")
    .set_envs({"DEMO_BUILT": "kuasar"})                # B: ENV (into the image config)
    .set_workdir("/home/user/site")                    # B: WORKDIR
    .set_start_cmd("python3 -m http.server 8000 --directory /home/user/site",   # C: startCmd → snapshot
                   wait_for_timeout(2000)))                                  # C: SDK readyCmd without extra image tools
# The SDK forwards build headers to both registration and trigger. Builder
# configuration is register-only, so use the existing auto target: these
# nonempty start/ready commands select a memory Sandbox (snapshot) output.
info = Template.build(
    tpl,
    name="demo-app-" + run_key,
    cpu_count=2,
    memory_mb=6144,
    on_build_logs=show,
)
print(json.dumps({"template_id": info.template_id, "build_id": info.build_id}))
PY
)" || die "template build failed (see $WORK/orch.log)"
read -r REGISTERED_TEMPLATE BUILD_ID < <("$PYTHON_BIN" -c \
    'import json,sys; x=json.loads(sys.argv[1]); print(x["template_id"], x["build_id"])' "$BUILD_RESULT") \
    || die "invalid SDK build result"
if [ -z "$REGISTERED_TEMPLATE" ] || [ -z "$BUILD_ID" ]; then die "no template/build id from build"; fi
# This SDK version returns the registration handle even after waiting for the
# build. Resolve the creatable published ID from this exact build's status.
curl -fsS --max-time 10 --noproxy '*' --cacert "$WORK/demo-ca.crt" \
    --resolve "api.$DOMAIN:$TLS_PORT:$CONTROL_IP" -H "X-API-KEY: $AK" \
    "https://api.$DOMAIN/templates/$REGISTERED_TEMPLATE/builds/$BUILD_ID/status" >"$WORK/build-status.json" \
    || die "could not read back the built template"
TEMPLATE="$("$PYTHON_BIN" - "$WORK/build-status.json" "$BUILD_ID" <<'PY'
import json, sys
result = json.load(open(sys.argv[1], encoding="utf-8"))
assert result.get("buildID") == sys.argv[2], "build identity mismatch"
assert result.get("status") == "ready", "build is not ready"
assert result.get("profile") == "e2b", "build profile mismatch"
assert "target" in result and result["target"] is None, "expected registered auto target"
assert result.get("kind") == "snp", "build did not publish a memory snapshot"
template = result.get("templateID", "")
assert template.startswith("e2b-snp-"), "build did not return a published snapshot ID"
print(template)
PY
)" || die "build readback did not match the command-selected snapshot target"
ok "snapshot template built → $TEMPLATE  (start command frozen under envd)"
pause

# ===========================================================================
banner "Spawn a real microVM sandbox (Sandbox.create)"
# ---------------------------------------------------------------------------
say "create boots cloud-hypervisor from the flattened template; envd starts inside (create is lazy — no eager data-plane connect)."
SID="$(py <<PY
from e2b import Sandbox
s = Sandbox.create("$TEMPLATE", timeout=300)
print(s.sandbox_id)
PY
)" || die "create failed"
[ -n "$SID" ] || die "no sandbox id from create"
printf '%s\n' "$SID" >>"$SIDS_FILE"; chmod 0600 "$SIDS_FILE"
hosts_add "$DATA_IP" "49983-$SID.$DOMAIN"; hosts_add "$DATA_IP" "49999-$SID.$DOMAIN"
ok "sandbox accepted: $SID  (the next guest operation verifies readiness through https://49983-$SID.$DOMAIN)"
pause

# ===========================================================================
banner "Run commands in the guest (sbx.commands.run)"
# ---------------------------------------------------------------------------
py <<PY || die "exec failed"
from e2b import Sandbox
s = Sandbox.connect("$SID")
r = s.commands.run("id; uname -sm; python3 --version; grep ^PRETTY_NAME= /etc/os-release")
print(r.stdout.rstrip())
# e2b sandboxes share sandbox-init's PID namespace (launch.pid_namespace=shared) so PID 1
# reaps orphaned descendants of guest commands instead of them piling up as zombies under envd.
init1 = s.commands.run("cat /proc/1/comm").stdout.strip()
print("guest PID 1 (reaps orphaned descendants):", init1)
assert init1 != "envd", f"e2b sandbox must share sandbox-init's PID ns (PID 1 = reaper, not envd); got {init1!r}"
print("--- the template's build outputs are present (COPY + RUN + ENV + startCmd) ---")
print(s.commands.run("echo '[/home/user/site/index.html]'; cat /home/user/site/index.html; "
                     "echo '[/home/user/site/COPIED.txt]'; cat /home/user/site/COPIED.txt 2>/dev/null || true").stdout.rstrip())
if $HAS_COPY:
    copied = s.commands.run("cat /home/user/site/COPIED.txt").stdout.strip()
    assert copied == "this file was COPY'd from the build context", copied
print("ENV DEMO_BUILT =", s.commands.run("printenv DEMO_BUILT || echo '(image env not applied to exec)'").stdout.strip())
code = s.commands.run(
    "python3 -c \"import urllib.request; "
    "print(urllib.request.urlopen('http://localhost:8000/', timeout=5).status)\""
).stdout.strip()
print("startCmd http.server in-guest (HTTP", code + ") — frozen in the snapshot, live after restore")
assert code == "200", f"the template start command (http.server) is not serving after restore (got {code!r})"
print("--- write a file that must survive pause/resume ---")
s.commands.run("echo 'hello from before the snapshot' > /home/user/state.txt")
print(s.commands.run("cat /home/user/state.txt").stdout.rstrip())
print("--- SDK Files API write/read ---")
s.files.write("/home/user/sdk-data.txt", "written through the E2B Files API")
assert s.files.read("/home/user/sdk-data.txt") == "written through the E2B Files API"
PY
ok "command and Files API data access work end-to-end (SDK → Proxy → envd → guest)"
pause

# ===========================================================================
banner "Networking: port forwarding (host -> sandbox floatingip) + guest egress (NAT)"
# ---------------------------------------------------------------------------
# No server to start: the template's start command (python3 -m http.server 8000,
# set at build time) is ALREADY running in the sandbox, serving the COPY'd+RUN-built
# page — reaching it proves the start_cmd survived create (snapshot→restore).
say "the template's start command already serves the built site on :8000 (frozen under envd; /etc/hosts+resolv.conf injected by orchestrator via files:)"
DB="$WORK/lib/node-ctl.db"
[ -s "$DB" ] || DB="$WORK/lib/orchestrator.db"
FIP="$(sqlite3 "$DB" "select floatingip from sandboxes where id='$SID'" 2>/dev/null || true)"
[ -n "$FIP" ] || die "could not resolve floatingip for sandbox $SID from $DB"
say "Sandbox floatingip = ${FIP:-?}  (host reaches it through $SW_MGMT)"
echo "${c_cmd}  \$ curl http://$FIP:8000/    # served by the template's start command${c_off}"
out=""; for _ in $(seq 1 8); do out="$(curl -s --max-time 5 --noproxy '*' "http://$FIP:8000/" 2>&1)" || true; case "$out" in *"$BUILT_MARKER"*) break;; esac; sleep 1; done
case "$out" in
  *"$BUILT_MARKER"*) ok "Direct host -> sandbox floatingip:8000 serves the built page (start_cmd survived snapshot/restore)";;
  *) if [ -n "${DEMO_NETDIAG:-}" ]; then say "Direct connection failed (NETDIAG: continuing)"; else die "Direct floatingip connection failed or did not serve the built page: ${out:-<empty>}"; fi ;;
esac
TOKEN_RESPONSE="$WORK/connect-token.json"
code="$(curl -sS --max-time 8 --noproxy '*' --cacert "$WORK/demo-ca.crt" \
    --resolve "api.$DOMAIN:$TLS_PORT:$CONTROL_IP" -o "$TOKEN_RESPONSE" -w '%{http_code}' \
    -X POST -H "X-API-KEY: $AK" -H 'Content-Type: application/json' -d '{}' \
    "https://api.$DOMAIN/sandboxes/$SID/connect" 2>/dev/null || true)"
[ "$code" = "200" ] || die "connect did not return sandbox credentials (HTTP ${code:-000})"
TOK="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1]))["forwardAccessToken"])' "$TOKEN_RESPONSE" 2>/dev/null || true)"
[ -n "$TOK" ] || die "connect response did not contain forwardAccessToken"
hosts_add "$DATA_IP" "8000-$SID.$DOMAIN"
out="$(curl -sS --max-time 8 --noproxy '*' --cacert "$WORK/demo-ca.crt" \
    --resolve "8000-$SID.$DOMAIN:$TLS_PORT:$DATA_IP" -H "X-Access-Token: $TOK" \
    "https://8000-$SID.$DOMAIN/" 2>&1)" || true
case "$out" in
  *"$BUILT_MARKER"*) ok "The e2b exposed port https://8000-<sid>.<domain> (proxy -> floatingip) serves the built page";;
  *) if [ -n "${DEMO_NETDIAG:-}" ]; then say "Exposed-port request failed (NETDIAG: continuing)"; else die "e2b exposed-port request failed: ${out:-<empty>}"; fi ;;
esac
say "Guest egress (NAT MASQUERADE): guest Python HTTP request to 1.1.1.1"
set +e
EGRESS_OUTPUT="$(py <<PY 2>&1
import re
from e2b import Sandbox
r = Sandbox.connect("$SID").commands.run(
    "python3 -c \"import http.client; "
    "c=http.client.HTTPConnection('1.1.1.1', 80, timeout=10); "
    "c.request('GET', '/'); print('egress HTTP', c.getresponse().status)\""
)
print(r.stdout.rstrip())
assert r.exit_code == 0, (r.exit_code, r.stderr)
assert re.fullmatch(r"egress HTTP [1-5][0-9][0-9]", r.stdout.strip()), r.stdout
PY
)"; EGRESS_STATUS=$?
set -e
printf '%s\n' "$EGRESS_OUTPUT" | sed 's/^/    /'
if [ "$EGRESS_STATUS" -ne 0 ]; then
    if [ -n "${DEMO_NETDIAG:-}" ]; then say "Guest egress failed (DEMO_NETDIAG keeps this diagnostic run going)"; else die "guest outbound NAT validation failed"; fi
else
    ok "guest outbound NAT returned an HTTP response"
fi
pause

# ===========================================================================
banner "Pause → resume (snapshot then restore; resume == Sandbox.connect)"
# ---------------------------------------------------------------------------
say "pause snapshots the live VM; connect resumes it (the SDK has no resume() — connect auto-resumes a paused sandbox)."
py <<PY || die "pause/resume failed"
from e2b import Sandbox
Sandbox.connect("$SID").pause()
print("paused; resuming via connect…")
s = Sandbox.connect("$SID")
state = s.commands.run("cat /home/user/state.txt").stdout.strip()
print("state survived:", state)
assert state == "hello from before the snapshot", state
assert s.files.read("/home/user/sdk-data.txt") == "written through the E2B Files API"
PY
ok "pause/resume preserved command and Files API data"
pause

# Quick Start intentionally stops at the shortest user lifecycle. The default
# full Demo continues with template fan-out and migration below.
if [ -n "${DEMO_QUICKSTART:-}" ]; then
    banner "Lifecycle: kill"
    py <<PY || die "kill failed"
from e2b import Sandbox
Sandbox.connect("$SID").kill()
print("killed $SID")
PY
    ok "sandbox killed"
    echo; echo "${c_ok}══════ quick start complete — built, created, ran, paused/resumed, and killed via the e2b Python SDK ══════${c_off}"
    exit 0
fi

# ===========================================================================
banner "Paused state -> template (export-sandbox --to-template) -> new sandbox instances"
# ---------------------------------------------------------------------------
say "pause, then promote the paused snapshot to a reusable remote template (local→remote)."
py <<PY || die "pause-before-export failed"
from e2b import Sandbox
Sandbox.connect("$SID").pause()
PY
echo "${c_cmd}  \$ node-ctl export-sandbox $SID --to-template --keep-source${c_off}"
FORK_TMPL="$(E2B_API_KEY="$AK" "$BIN/node-ctl" export-sandbox "$SID" --to-template --keep-source --socket "$WORK/node-ctl.socket")" \
  || die "export-sandbox --to-template failed"
ok "paused state → template $FORK_TMPL"
say "create a NEW sandbox from that template — it carries the forked state:"
CHILD="$(py <<PY
from e2b import Sandbox
s = Sandbox.create("$FORK_TMPL", timeout=120)
print(s.sandbox_id)
PY
)" || die "create-from-fork failed"
[ -n "$CHILD" ] || die "no sandbox id from template fan-out"
printf '%s\n' "$CHILD" >>"$SIDS_FILE"
hosts_add "$DATA_IP" "49983-$CHILD.$DOMAIN"; hosts_add "$DATA_IP" "49999-$CHILD.$DOMAIN"
py <<PY || die "forked child exec failed"
from e2b import Sandbox
ids = [s.sandbox_id for s in Sandbox.list().next_items()]
print("forked child", "$CHILD", "running:", "$CHILD" in ids)
assert "$CHILD" in ids, "forked child is missing from the running sandbox list"
child = Sandbox.connect("$CHILD")
result = child.commands.run("cat /home/user/state.txt")
assert result.exit_code == 0, (result.exit_code, result.stderr)
assert result.stdout.strip() == "hello from before the snapshot", result.stdout
assert child.files.read("/home/user/sdk-data.txt") == "written through the E2B Files API"
print("  child sees forked state:", result.stdout.rstrip())
print("  child exec user:", child.commands.run("id -un").stdout.rstrip())
child.kill()
PY
ok "fork via template: a fresh sandbox booted from the paused state + in-guest exec works"
pause

# ===========================================================================
banner "One-call migration (export move -> connect with X-Kuasar-Migration-Token = import+resume)"
# ---------------------------------------------------------------------------
say "export (move) mints a one-line token and relinquishes the source row; connect with the token re-imports + resumes in ONE SDK call."
echo "${c_cmd}  \$ TOKEN=\$(node-ctl export-sandbox $SID)${c_off}"
MIG_TOKEN="$(E2B_API_KEY="$AK" "$BIN/node-ctl" export-sandbox "$SID" --socket "$WORK/node-ctl.socket")" || die "export-sandbox (move) failed"
say "source row now gone; resume on (logically) another node with the migration header:"
py <<PY || die "connect-with-migration-token failed"
from e2b import Sandbox
s = Sandbox.connect("$SID", headers={"X-Kuasar-Migration-Token": "$MIG_TOKEN"})
result = s.commands.run("cat /home/user/state.txt")
assert result.exit_code == 0, (result.exit_code, result.stderr)
assert result.stdout.strip() == "hello from before the snapshot", result.stdout
assert s.files.read("/home/user/sdk-data.txt") == "written through the E2B Files API"
print("migrated + resumed; state:", result.stdout.rstrip())
PY
ok "one-call migration (auto import + resume) preserved guest state"
pause

# ===========================================================================
banner "Lifecycle: list, then kill"
# ---------------------------------------------------------------------------
py <<PY || die "final list/kill failed"
from e2b import Sandbox
print("running sandboxes:", [s.sandbox_id for s in Sandbox.list().next_items()])
Sandbox.connect("$SID").kill()
print("killed $SID")
PY
ok "sandbox killed"
echo; echo "${c_ok}══════ demo complete — built (full 3-phase pipeline → snapshot), created, ran, served the built site, paused/resumed, forked-to-template, migrated, killed — all via the e2b Python SDK ══════${c_off}"
