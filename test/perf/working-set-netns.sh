#!/usr/bin/env bash

# Run the working-set smoke inside a runner-owned network namespace (#43, #53).
#
# The smoke's TAP holds the shared 169.254.1.0/31 host subnet. On a reused
# self-hosted runner, an interrupted prior run left its TAP behind, and the
# stale connected route could win route selection against the current run.
# Prefix-based cleanup cannot prove ownership, so instead every run executes
# inside a namespace whose name is derived from RUNNER_NAME. The namespace is
# a precise resource boundary: recovery after any interruption (including
# SIGKILL) is by exact name, and other runners or host interfaces are never
# touched.
#
# Semantics:
#   - pre-smoke recovery of a leftover namespace is fail-closed: if the
#     namespace cannot be fully reclaimed, the job fails before measuring;
#   - exit-time teardown is best-effort: a failure emits ::warning:: and
#     keeps the named namespace for the next pre-job reset to reclaim, but
#     does not change the verdict of an already-green measurement.
#
# Usage (from CI):  bash test/perf/working-set-netns.sh <smoke-script> [args...]

set -euo pipefail

fatal() {
    echo "working-set-netns: FATAL: $*" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || fatal "must run as root (the smoke itself re-execs sudo)"

# --- namespace identity ---------------------------------------------------

# Deterministic from RUNNER_NAME so two runners on one host never collide and
# an interrupted run can be reclaimed by exact name. Not derived from the run
# id: a random name would force the next reset back to guessing ownership.
working_set_netns_name() { # $1=runner name
    printf 'kuasar-ws-%s\n' "$(printf '%s' "$1" | sha256sum | cut -c1-8)"
}

NETNS_NAME="$(working_set_netns_name "${RUNNER_NAME:-local}")"

# --- fail-closed reclamation ----------------------------------------------

TERM_WAIT_SECONDS="${WORKING_SET_NETNS_TERM_WAIT:-10}"
KILL_WAIT_SECONDS="${WORKING_SET_NETNS_KILL_WAIT:-5}"

# Kill every process inside the namespace, then verify it is empty. Any
# survivor means the namespace cannot be reclaimed safely; deleting the name
# while processes hold the namespace would orphan it beyond exact-name
# recovery, so the caller must fail instead.
working_set_netns_kill_all() { # $1=netns name
    local netns="$1" pid deadline
    local -a pids=()

    mapfile -t pids < <(ip netns pids "$netns" 2>/dev/null)
    for pid in "${pids[@]}"; do
        [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null || true
    done

    deadline=$((SECONDS + TERM_WAIT_SECONDS))
    while [ "$SECONDS" -lt "$deadline" ]; do
        [ -z "$(ip netns pids "$netns" 2>/dev/null)" ] && return 0
        sleep 0.5
    done

    mapfile -t pids < <(ip netns pids "$netns" 2>/dev/null)
    for pid in "${pids[@]}"; do
        [ -n "$pid" ] && kill -KILL "$pid" 2>/dev/null || true
    done

    deadline=$((SECONDS + KILL_WAIT_SECONDS))
    while [ "$SECONDS" -lt "$deadline" ]; do
        [ -z "$(ip netns pids "$netns" 2>/dev/null)" ] && return 0
        sleep 0.5
    done

    [ -z "$(ip netns pids "$netns" 2>/dev/null)" ]
}

# Fail-closed: used before the smoke starts. A namespace that cannot be
# reclaimed must stop the job, because measuring with an unknown tenant
# inside the namespace is exactly the poisoned-runner failure mode (#43).
working_set_netns_reclaim() { # $1=netns name
    local netns="$1"
    if ! ip netns list | awk '{print $1}' | grep -Fxq "$netns"; then
        return 0
    fi
    echo "working-set-netns: reclaiming leftover namespace $netns" >&2
    working_set_netns_kill_all "$netns" \
        || fatal "processes in $netns survived TERM and KILL; refusing to measure"
    ip netns del "$netns" \
        || fatal "cannot delete leftover namespace $netns"
}

# Best-effort: used on exit. Failures are loud but never change the verdict
# of a completed measurement; the named namespace stays for the next pre-job
# reset to reclaim by exact name.
working_set_netns_teardown() { # $1=netns name
    local netns="$1"
    set +e
    if ! ip netns list | awk '{print $1}' | grep -Fxq "$netns"; then
        return 0
    fi
    if ! working_set_netns_kill_all "$netns"; then
        echo "::warning::working-set-netns: processes in $netns survived teardown; the next pre-job reset will retry" >&2
        return 0
    fi
    if ! ip netns del "$netns"; then
        echo "::warning::working-set-netns: cannot delete $netns; the next pre-job reset will retry" >&2
    fi
}

# --- run ------------------------------------------------------------------

if [ "${WORKING_SET_NETNS_SOURCED:-}" = 1 ]; then
    return 0 2>/dev/null || true
fi

SMOKE_SCRIPT="${1:-}"
[ -n "$SMOKE_SCRIPT" ] && [ -f "$SMOKE_SCRIPT" ] \
    || fatal "usage: working-set-netns.sh <smoke-script> [args...]"

working_set_netns_reclaim "$NETNS_NAME"
ip netns add "$NETNS_NAME" || fatal "cannot create namespace $NETNS_NAME"
ip netns exec "$NETNS_NAME" ip link set lo up \
    || { working_set_netns_teardown "$NETNS_NAME"; fatal "cannot bring up lo in $NETNS_NAME"; }

# Fixed TAP name inside the private namespace: no other tenant can hold it.
# The smoke's own random-name default remains for manual host runs.
EXIT_CODE=0
if ! ip netns exec "$NETNS_NAME" env \
        TAP_NAME=kws0 \
        RUNNER_NAME="${RUNNER_NAME:-local}" \
        bash "$SMOKE_SCRIPT" "${@:2}"; then
    EXIT_CODE=$?
fi

working_set_netns_teardown "$NETNS_NAME"
exit "$EXIT_CODE"
