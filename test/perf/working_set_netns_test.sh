#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=test/perf/working-set-netns.sh
WORKING_SET_NETNS_SOURCED=1
. "$REPO_ROOT/test/perf/working-set-netns.sh"

WORK="$(mktemp -d /tmp/working-set-netns-test-XXXXXX)"
CLEANED_NETNSES=()
cleanup() {
    set +e
    for netns in "${CLEANED_NETNSES[@]}"; do
        ip netns pids "$netns" 2>/dev/null | while read -r pid; do
            [ -n "$pid" ] && kill -KILL "$pid" 2>/dev/null
        done
        ip netns del "$netns" 2>/dev/null
    done
    rm -rf "$WORK"
}
trap cleanup EXIT

fail() {
    echo "working_set_netns_test: FAIL: $*" >&2
    exit 1
}

require_root_netns() {
    if [ "$(id -u)" -ne 0 ] || ! ip netns add wsnt-probe >/dev/null 2>&1; then
        echo "working_set_netns_test: skipped (needs root network namespaces)" >&2
        exit 0
    fi
    ip netns del wsnt-probe
}

track() { CLEANED_NETNSES+=("$1"); }

# Wait until ip netns pids is empty, up to $2 seconds (default 5).
wait_empty() { # $1=netns, $2=timeout seconds
    local netns="$1" timeout="${2:-5}" waited=0
    while [ -n "$(ip netns pids "$netns" 2>/dev/null)" ] && [ "$waited" -lt $((timeout * 2)) ]; do
        sleep 0.5
        waited=$((waited + 1))
    done
    [ -z "$(ip netns pids "$netns" 2>/dev/null)" ]
}

require_root_netns

# ---- 1. deterministic naming ----------------------------------------------
A="$(working_set_netns_name runner-alpha)"
B="$(working_set_netns_name runner-beta)"
[ "$A" != "$B" ] || fail "two runners derived the same namespace name"
[ "$(working_set_netns_name runner-alpha)" = "$A" ] \
    || fail "namespace name is not deterministic across calls"
case "$A" in kuasar-ws-????????) ;; *) fail "unexpected namespace name format: $A" ;; esac

# ---- 2. host stale TAP is neither visible nor deleted ----------------------
# A leftover host TAP on the shared subnet must not affect a namespaced run
# and must not be deleted by it.
ip tuntap add dev wsnt-host0 mode tap
ip address add 169.254.1.0/31 dev wsnt-host0
ip link set wsnt-host0 up
NETNS_A="$A"
ip netns add "$NETNS_A"; track "$NETNS_A"
ip netns exec "$NETNS_A" ip link set lo up
ip netns exec "$NETNS_A" ip tuntap add dev kws0 mode tap
ip netns exec "$NETNS_A" ip address add 169.254.1.0/31 dev kws0
ip netns exec "$NETNS_A" ip link set kws0 up
ns_route="$(ip netns exec "$NETNS_A" ip -o route get 169.254.1.1 2>/dev/null || true)"
case "$ns_route" in
    *dev\ kws0*) ;;
    *) fail "namespaced route does not use the namespaced TAP: $ns_route" ;;
esac
ip link show dev wsnt-host0 >/dev/null 2>&1 \
    || fail "host stale TAP wsnt-host0 was deleted by the namespaced run"
working_set_netns_kill_all "$NETNS_A" || fail "kill_all failed on an idle namespace"
ip netns del "$NETNS_A"
ip link show dev wsnt-host0 >/dev/null 2>&1 \
    || fail "host stale TAP wsnt-host0 was deleted by reclamation"
ip link del wsnt-host0

# ---- 3. normal exit teardown ------------------------------------------------
ip netns add "$NETNS_A"; track "$NETNS_A"
ip netns exec "$NETNS_A" ip link set lo up
working_set_netns_teardown "$NETNS_A"
ip netns list | awk '{print $1}' | grep -Fxq "$NETNS_A" \
    && fail "teardown left namespace $NETNS_A behind"

# ---- 4. teardown with a live process inside ---------------------------------
ip netns add "$NETNS_A"; track "$NETNS_A"
ip netns exec "$NETNS_A" ip link set lo up
ip netns exec "$NETNS_A" setsid sleep 300 >/dev/null 2>&1 < /dev/null &
sleep 0.3
[ -n "$(ip netns pids "$NETNS_A")" ] || fail "test process did not enter the namespace"
working_set_netns_teardown "$NETNS_A"
ip netns list | awk '{print $1}' | grep -Fxq "$NETNS_A" \
    && fail "teardown left namespace $NETNS_A behind with a live process"
wait_empty "$NETNS_A" 2 2>/dev/null || true

# ---- 5. fail-closed reclaim when a process survives KILL --------------------
# Simulate an unkillable tenant with a fake ip that reports a persistent pid.
# Run in a subshell: the fail-closed path calls fatal(), which exits.
if (
    ip() {
        case "$*" in
            "netns pids stubborn"*) echo 424242 ;;
            "netns list"*) { echo "stubborn (id: 0)"; command ip netns list; } ;;
            *) command ip "$@" ;;
        esac
    }
    working_set_netns_reclaim stubborn
) </dev/null; then
    fail "reclaim succeeded although a process survived KILL"
fi
ip netns add "$NETNS_A"; track "$NETNS_A"
ip netns exec "$NETNS_A" ip link set lo up
working_set_netns_reclaim "$NETNS_A" \
    || fail "reclaim failed on an empty leftover namespace"
ip netns list | awk '{print $1}' | grep -Fxq "$NETNS_A" \
    && fail "reclaim left namespace $NETNS_A behind"

# ---- 6. two runner namespaces never touch each other ------------------------
NETNS_B="$B"
ip netns add "$NETNS_B"; track "$NETNS_B"
ip netns exec "$NETNS_B" ip link set lo up
ip netns exec "$NETNS_B" setsid sleep 300 >/dev/null 2>&1 < /dev/null &
sleep 0.3
ip netns add "$NETNS_A"; track "$NETNS_A"
working_set_netns_reclaim "$NETNS_A"
ip netns list | awk '{print $1}' | grep -Fxq "$NETNS_B" \
    || fail "reclaiming runner A deleted runner B's namespace"
[ -n "$(ip netns pids "$NETNS_B")" ] \
    || fail "reclaiming runner A killed runner B's process"
working_set_netns_kill_all "$NETNS_B"
ip netns del "$NETNS_B"

# ---- 7. SIGKILL recovery via exact name --------------------------------------
ip netns add "$NETNS_A"; track "$NETNS_A"
ip netns exec "$NETNS_A" ip link set lo up
ip netns exec "$NETNS_A" setsid sleep 300 >/dev/null 2>&1 < /dev/null &
sleep 0.3
# Simulate the wrapper being SIGKILLed: nothing cleans up here.
# The next pre-job reset reclaims by exact name:
working_set_netns_reclaim "$NETNS_A" \
    || fail "reclaim after simulated SIGKILL failed"
ip netns list | awk '{print $1}' | grep -Fxq "$NETNS_A" \
    && fail "reclaim after simulated SIGKILL left the namespace behind"

# ---- 8. flap keeps the /32 (working_set_reset_tap) -----------------------------
ip netns add "$NETNS_A"; track "$NETNS_A"
ip netns exec "$NETNS_A" ip link set lo up
ip netns exec "$NETNS_A" ip tuntap add dev kws0 mode tap
ip netns exec "$NETNS_A" ip address add 169.254.1.0/31 dev kws0
ip netns exec "$NETNS_A" ip link set kws0 up
ip netns exec "$NETNS_A" ip route add 169.254.1.1/32 dev kws0 src 169.254.1.0
ip netns exec "$NETNS_A" bash -c '
    . "$1/test/lib/working_set_tap.sh"
    working_set_reset_tap kws0 169.254.1.0/31 169.254.1.0 169.254.1.1
' _ "$REPO_ROOT"
ip netns exec "$NETNS_A" ip -o route show dev kws0 | grep -q "^169.254.1.1 " \
    || fail "guest /32 missing after flap+replace inside the namespace"
working_set_netns_kill_all "$NETNS_A"
ip netns del "$NETNS_A"

echo "working_set_netns_test: PASS"
