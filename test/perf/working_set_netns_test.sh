#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

fail() {
    echo "working_set_netns_test: FAIL: $*" >&2
    exit 1
}

# Non-root environments: the skip must be reachable WITHOUT the wrapper's
# own root check firing first (the root check lives below the SOURCED guard
# precisely for this).
if [ "$(id -u)" -ne 0 ]; then
    echo "working_set_netns_test: skipped (needs root network namespaces)" >&2
    exit 0
fi

WORK="$(mktemp -d /tmp/working-set-netns-test-XXXXXX)"  # scratch files only

# ---- test-owned resource identity ------------------------------------------
# Live resources use a DETERMINISTIC owner identity — RUNNER_NAME in CI,
# hostname locally — exactly like the production wrapper's runner-owned
# namespaces. A SIGKILLed run executes no trap at all, so the only reliable
# recovery is the NEXT run deriving the same names and reclaiming them at
# startup. Random per-run names would leak the killed run's namespaces and
# host TAP forever: nothing else can ever derive them.
WS_OWNER="${RUNNER_NAME:-$(hostname)}"
WS_OWNER_HASH="$(printf '%s' "$WS_OWNER" | sha256sum | cut -c1-8)"
WS_PROBE="wsnt-$WS_OWNER_HASH-probe"
HOST_TAP="kws-$WS_OWNER_HASH"
WS_LOCK="/tmp/wsnt-$WS_OWNER_HASH.lock"

# Deterministic names are shared state, so same-owner runs must serialize:
# without the lock two instances would reclaim each other's namespaces
# mid-run. flock is released automatically when the process dies (including
# SIGKILL); the wait bound only covers transient holders.
exec 9>"$WS_LOCK"
flock -w 60 9 || fail "another working_set_netns_test run for owner $WS_OWNER holds $WS_LOCK"

# shellcheck source=test/perf/working-set-netns.sh
WORKING_SET_NETNS_SOURCED=1
. "$REPO_ROOT/test/perf/working-set-netns.sh"

CLEANED_NETNSES=()
CLEANED_TAPS=()
cleanup() {
    # Best-effort, but never weaker than the production teardown invariant:
    # a raw kill+del here could delete the bind name while a KILL-surviving
    # tenant (e.g. D-state) is still inside, producing an anonymous orphan
    # that exact-name recovery can never find again. Delegating keeps every
    # failure mode NAMED and reclaimable; teardown always returns 0, so the
    # test's own exit status is never disturbed.
    set +e
    for tap in "${CLEANED_TAPS[@]}"; do
        ip link del "$tap" 2>/dev/null
    done
    for netns in "${CLEANED_NETNSES[@]}"; do
        working_set_netns_teardown "$netns"
    done
    rm -rf -- "$WORK"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

track() { CLEANED_NETNSES+=("$1"); }
track_tap() { CLEANED_TAPS+=("$1"); }

create_host_tap() { # $1=name — created TAPs are always tracked for cleanup
    ip tuntap add dev "$1" mode tap || fail "cannot create host TAP $1"
    track_tap "$1"
}

netns_exists() { ip netns list | awk '{print $1}' | grep -Fxq "$1"; }

# Capability probe, with an explicit return contract:
#   return 0  — netns support confirmed and the probe fully cleaned up
#   return 1  — GENUINE capability absence only (the one skip path)
#   fail      — any owned-collision, operational, or cleanup failure
# The caller runs this inside `if !`, which suppresses set -e for the whole
# call, so every failure inside must be handled explicitly — never let a
# command status fall through to the return value.
#
# State machine: no leftover + add ok + del ok          → 0
#                no leftover + add fails                → 1 (capability skip)
#                leftover + reclaim ok + re-add ok + del ok → 0
#                leftover reclaim fails                 → FAIL (never skip)
#                leftover + reclaim ok + re-add fails   → FAIL (never skip)
#                add ok + final del fails               → FAIL (never skip)
probe_netns_support() {
    local existed=0
    if netns_exists "$WS_PROBE"; then
        existed=1
        working_set_netns_reclaim "$WS_PROBE"   # fatal on any failure: FAIL, not skip
    fi
    if ! ip netns add "$WS_PROBE" >/dev/null 2>&1; then
        if [ "$existed" = 1 ]; then
            fail "cannot recreate probe namespace $WS_PROBE after reclaim (netns support was demonstrated by the leftover)"
        fi
        return 1   # no owned leftover and add fails: genuine capability skip
    fi
    # The add succeeded, so netns support is PROVEN: a delete failure here is
    # a cleanup failure, not a capability statement. Without this explicit
    # check the del status would become the function's return value and the
    # caller would misread it as "unsupported" — a false-green skip.
    if ! ip netns del "$WS_PROBE"; then
        fail "cannot delete fresh probe namespace $WS_PROBE (netns support was demonstrated by the successful add)"
    fi
}
if ! probe_netns_support; then
    echo "working_set_netns_test: skipped (needs root network namespaces)" >&2
    exit 0
fi

# ---- 1. deterministic naming ----------------------------------------------
A="$(working_set_netns_name "wsnt-$WS_OWNER_HASH-alpha")"
B="$(working_set_netns_name "wsnt-$WS_OWNER_HASH-beta")"
[ "$A" != "$B" ] || fail "two runners derived the same namespace name"
[ "$(working_set_netns_name "wsnt-$WS_OWNER_HASH-alpha")" = "$A" ] \
    || fail "namespace name is not deterministic across calls"
case "$A" in kuasar-ws-????????) ;; *) fail "unexpected namespace name format: $A" ;; esac

# ---- startup recovery of test-owned leftovers ------------------------------
# Reclaim exactly the names this run will use: a SIGKILLed prior same-owner
# run cannot run any trap, so its namespaces (tenants included) and host TAP
# are recovered here, fail-closed, by derivation. Nothing outside these
# exact names is ever touched.
for _ns in "$A" "$B" \
    "$(working_set_netns_name "wsnt-$WS_OWNER_HASH-wrapper")" \
    "$(working_set_netns_name "wsnt-$WS_OWNER_HASH-sigterm")"; do
    working_set_netns_reclaim "$_ns" || fail "cannot reclaim pre-existing $_ns"
done
if ip link show dev "$HOST_TAP" >/dev/null 2>&1; then
    ip link del "$HOST_TAP" || fail "cannot delete leftover host TAP $HOST_TAP"
fi

# ---- 2-4. wrapper contract: exit status + teardown, invoked as executable --
# A fake smoke that records its namespace, so we can assert the wrapper tore
# it down in both success and failure cases.
FAKE="$WORK/fake-smoke.sh"
cat >"$FAKE" <<'EOF'
#!/usr/bin/env bash
echo "FAKE-NETNS=$(readlink /proc/self/ns/net)"
[ -n "${FAKE_SLEEP:-}" ] && sleep "$FAKE_SLEEP"
exit "${FAKE_EXIT:-0}"
EOF
chmod +x "$FAKE"

RUNNER_NAME="wsnt-$WS_OWNER_HASH-wrapper" bash "$REPO_ROOT/test/perf/working-set-netns.sh" "$FAKE" \
    >"$WORK/wrap-ok.out" 2>&1 \
    || fail "wrapper failed for a green smoke: $(cat "$WORK/wrap-ok.out")"
grep -q FAKE-NETNS "$WORK/wrap-ok.out" \
    || fail "green smoke did not run inside the namespace"
ok_netns="$(sed -n 's/^FAKE-NETNS=//p' "$WORK/wrap-ok.out")"
rc=0
FAKE_EXIT=42 RUNNER_NAME="wsnt-$WS_OWNER_HASH-wrapper" bash "$REPO_ROOT/test/perf/working-set-netns.sh" \
    "$FAKE" >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 42 ] \
    || fail "wrapper did not propagate smoke exit 42 (got $rc)"
[ -n "$ok_netns" ] || fail "missing green-run netns inode"
# both runs must have torn their namespace down (same deterministic name)
netns_exists "$(working_set_netns_name "wsnt-$WS_OWNER_HASH-wrapper")" \
    && fail "wrapper left its namespace behind after exit"

# smoke failure path also tears down: run once more exiting 1 and check
RUNNER_NAME="wsnt-$WS_OWNER_HASH-wrapper" FAKE_EXIT=1 bash "$REPO_ROOT/test/perf/working-set-netns.sh" "$FAKE" \
    >/dev/null 2>&1 && fail "wrapper swallowed smoke failure (exit 0)"
netns_exists "$(working_set_netns_name "wsnt-$WS_OWNER_HASH-wrapper")" \
    && fail "wrapper left its namespace behind after failed smoke"

# ---- 5. SIGTERM to the wrapper triggers teardown ---------------------------
WRAPPER="$REPO_ROOT/test/perf/working-set-netns.sh"
FAKE_SLEEP=30 FAKE_EXIT=0 RUNNER_NAME="wsnt-$WS_OWNER_HASH-sigterm" setsid bash "$WRAPPER" "$FAKE" 9>&- \
    >/dev/null 2>&1 &
WRAP_PID=$!
# give the wrapper time to create its namespace, then TERM it
sig_ns="$(working_set_netns_name "wsnt-$WS_OWNER_HASH-sigterm")"
for _ in $(seq 1 20); do netns_exists "$sig_ns" && break; sleep 0.1; done
netns_exists "$sig_ns" || fail "wrapper did not create its namespace before TERM"
kill -TERM -- -"$WRAP_PID" 2>/dev/null || kill -TERM "$WRAP_PID" 2>/dev/null || true
waited=0
while kill -0 "$WRAP_PID" 2>/dev/null && [ "$waited" -lt 30 ]; do sleep 0.2; waited=$((waited+1)); done
kill -0 "$WRAP_PID" 2>/dev/null && { kill -KILL "$WRAP_PID"; fail "wrapper survived SIGTERM"; }
sig_rc=0
wait "$WRAP_PID" 2>/dev/null || sig_rc=$?
[ "$sig_rc" -ne 0 ] || fail "wrapper exited 0 after SIGTERM"
netns_exists "$sig_ns" && fail "wrapper left its namespace behind after SIGTERM"

# ---- 6. simulated SIGKILL leftover -> exact-name reclaim -------------------
ip netns add "$sig_ns"; track "$sig_ns"
ip netns exec "$sig_ns" ip link set lo up
ip netns exec "$sig_ns" setsid sleep 300 >/dev/null 2>&1 < /dev/null 9>&- &
sleep 0.3
# (wrapper was SIGKILLed: nothing cleaned up; the next pre-job/pre-smoke
# reset reclaims by exact name)
working_set_netns_reclaim "$sig_ns" \
    || fail "reclaim after simulated SIGKILL failed"
netns_exists "$sig_ns" && fail "reclaim left the namespace behind"

# ---- 7. unkillable tenant: reclaim fails closed, name preserved ------------
# Run reclaim in a subshell with a fake ip: pids never empty. It must exit
# non-zero (fatal) and MUST NOT delete the namespace name (fake `netns del`
# records the call).
DELS="$WORK/del.calls"
if (
    ip() {
        case "$*" in
            "netns list"*) echo "wsnt-stubborn (id: 0)"; command ip netns list ;;
            *"netns pids wsnt-stubborn"*) echo 424242 ;;
            *"netns del wsnt-stubborn"*) echo called >>"$DELS"; exit 1 ;;
            *) command ip "$@" ;;
        esac
    }
    working_set_netns_reclaim wsnt-stubborn
) </dev/null >/dev/null 2>&1; then
    fail "reclaim succeeded although a process survived KILL"
fi
[ ! -e "$DELS" ] || fail "reclaim deleted the namespace name despite a survivor"

# ---- 8. deletion failure: reclaim fails closed ------------------------------
if (
    ip() {
        case "$*" in
            "netns list"*) echo "wsnt-delfail (id: 0)"; command ip netns list ;;
            *"netns pids wsnt-delfail"*) : ;;
            *"netns del wsnt-delfail"*) exit 1 ;;
            *) command ip "$@" ;;
        esac
    }
    working_set_netns_reclaim wsnt-delfail
) </dev/null >/dev/null 2>&1; then
    fail "reclaim succeeded although namespace deletion failed"
fi

# ---- 8a. initial enumeration failure: reclaim fails closed, no delete -------
# `ip netns pids` failing must never be read as "namespace is empty"; the
# name must survive for the next reset.
DELS_A="$WORK/del-a.calls"
rm -f "$DELS_A"
if (
    ip() {
        case "$*" in
            "netns list"*) echo "wsnt-pidsfail (id: 0)"; command ip netns list ;;
            *"netns pids wsnt-pidsfail"*) echo "Cannot open network namespace" >&2; exit 1 ;;
            *"netns del wsnt-pidsfail"*) echo called >>"$DELS_A"; exit 0 ;;
            *) command ip "$@" ;;
        esac
    }
    working_set_netns_reclaim wsnt-pidsfail
) </dev/null >/dev/null 2>&1; then
    fail "reclaim succeeded although the initial pids enumeration failed"
fi
[ ! -e "$DELS_A" ] || fail "reclaim deleted the namespace name despite failed enumeration"

# ---- 8b. post-KILL verification failure: reclaim fails closed, no delete ----
# Call 1 succeeds with a PID; every later query fails at the command level.
# The call count MUST live in a file: production reads pids through command
# substitution (working_set_netns_pids runs `output="$(ip netns pids ...)"`
# inside its own subshell), so a plain shell variable would reset on every
# call and the mock would degenerate into a permanent-survivor simulation.
DELS_B="$WORK/del-b.calls"
CALLS_B="$WORK/wsnt-vfyfail.calls"
rm -f "$DELS_B"
printf '0\n' >"$CALLS_B"
if (
    ip() {
        case "$*" in
            "netns list"*) echo "wsnt-vfyfail (id: 0)"; command ip netns list ;;
            *"netns pids wsnt-vfyfail"*)
                n="$(cat "$CALLS_B")"
                n=$((n + 1))
                printf '%s\n' "$n" >"$CALLS_B"
                if [ "$n" -eq 1 ]; then
                    echo 424242
                    return 0
                fi
                echo "Cannot open network namespace" >&2
                return 1
                ;;
            *"netns del wsnt-vfyfail"*) echo called >>"$DELS_B"; exit 0 ;;
            *) command ip "$@" ;;
        esac
    }
    working_set_netns_reclaim wsnt-vfyfail
) </dev/null >/dev/null 2>&1; then
    fail "reclaim succeeded although post-TERM/KILL verification failed"
fi
[ "$(cat "$CALLS_B")" -ge 2 ] \
    || fail "post-KILL failure scenario never reached a second enumeration (calls=$(cat "$CALLS_B"))"
[ ! -e "$DELS_B" ] || fail "reclaim deleted the namespace name despite failed verification"

# teardown with an unreliable query stays best-effort and keeps the name:
# it must return 0 (never touch a caller's exit status) and must not delete.
DELS_C="$WORK/del-c.calls"
rm -f "$DELS_C"
if ! (
    ip() {
        case "$*" in
            "netns list"*) echo "wsnt-tdfail (id: 0)"; command ip netns list ;;
            *"netns pids wsnt-tdfail"*) echo "Cannot open network namespace" >&2; exit 1 ;;
            *"netns del wsnt-tdfail"*) echo called >>"$DELS_C"; exit 0 ;;
            *) command ip "$@" ;;
        esac
    }
    working_set_netns_teardown wsnt-tdfail
) </dev/null >/dev/null 2>&1; then
    fail "teardown returned non-zero on enumeration failure (must stay best-effort)"
fi
[ ! -e "$DELS_C" ] || fail "teardown deleted the namespace name despite failed enumeration"

# ---- 9. reclaim A spares a concurrently live B and the host TAP ------------
# Both namespaces must exist with live processes for this to prove anything:
# reclaim(A) must kill A's tenant and delete only A, leaving B and its tenant
# untouched. Ownership comes solely from the deterministic name.
ip netns add "$A"; track "$A"
ip netns exec "$A" ip link set lo up
ip netns exec "$A" setsid sleep 300 >/dev/null 2>&1 < /dev/null 9>&- &
sleep 0.2
ip netns add "$B"; track "$B"
ip netns exec "$B" ip link set lo up
ip netns exec "$B" setsid sleep 300 >/dev/null 2>&1 < /dev/null 9>&- &
# host-side TAP on the shared subnet, with this run's unique name; the
# reclaim of A must neither see nor touch it (same-subnet invisibility)
create_host_tap "$HOST_TAP"
ip address add 169.254.1.0/31 dev "$HOST_TAP"
ip link set "$HOST_TAP" up
sleep 0.3
# both tenants live before the reclaim
[ -n "$(ip netns pids "$A")" ] || fail "A's tenant was not running before reclaim"
[ -n "$(ip netns pids "$B")" ] || fail "B's tenant was not running before reclaim"
working_set_netns_reclaim "$A" || fail "reclaim of a live A failed"
netns_exists "$A" && fail "reclaim left A's namespace behind"
[ -z "$(ip netns pids "$A" 2>/dev/null)" ] || fail "reclaim left A's tenant alive"
netns_exists "$B" || fail "reclaiming runner A deleted runner B's namespace"
[ -n "$(ip netns pids "$B")" ] || fail "reclaiming runner A killed runner B's process"
ip link show dev "$HOST_TAP" >/dev/null 2>&1 || fail "reclaiming runner A deleted the host TAP $HOST_TAP"
working_set_netns_kill_all "$B"
ip netns del "$B"
ip link del "$HOST_TAP"

# ---- 10. same-subnet host TAP invisible to namespaced runs ------------------
# The host TAP carries the shared working-set subnet under this run's unique
# name; a namespaced kws0 must route over its own stack regardless. (A host
# TAP literally named kws0 would be un-reclaimable state — see the owner
# identity above — and name-level invisibility is guaranteed by the netns
# boundary itself.)
create_host_tap "$HOST_TAP"
ip address add 169.254.1.0/31 dev "$HOST_TAP"
ip link set "$HOST_TAP" up
ip netns add "$A"; track "$A"
ip netns exec "$A" ip link set lo up
ip netns exec "$A" ip tuntap add dev kws0 mode tap
ip netns exec "$A" ip address add 169.254.1.0/31 dev kws0
ip netns exec "$A" ip link set kws0 up
ns_route="$(ip netns exec "$A" ip -o route get 169.254.1.1 2>/dev/null || true)"
case "$ns_route" in
    *dev\ kws0*) ;;
    *) fail "namespaced route does not use the namespaced TAP: $ns_route" ;;
esac
ip link show dev "$HOST_TAP" >/dev/null 2>&1 || fail "host TAP $HOST_TAP vanished after namespaced run"
working_set_netns_kill_all "$A"
ip netns del "$A"
ip link del "$HOST_TAP"

# ---- 11. flap keeps the /32 (working_set_reset_tap) --------------------------
ip netns add "$A"; track "$A"
ip netns exec "$A" ip link set lo up
ip netns exec "$A" ip tuntap add dev kws0 mode tap
ip netns exec "$A" ip address add 169.254.1.0/31 dev kws0
ip netns exec "$A" ip link set kws0 up
ip netns exec "$A" ip route add 169.254.1.1/32 dev kws0 src 169.254.1.0
ip netns exec "$A" bash -c '
    . "$1/test/lib/working_set_tap.sh"
    working_set_reset_tap kws0 169.254.1.0/31 169.254.1.0 169.254.1.1
' _ "$REPO_ROOT"
ip netns exec "$A" ip -o route show dev kws0 | grep -q "^169.254.1.1 " \
    || fail "guest /32 missing after flap+replace inside the namespace"
working_set_netns_kill_all "$A"
ip netns del "$A"

# ---- 12. non-root direct invocation sudo re-execs, no premature fatal ------
# Pin the production bootstrap: a non-root direct run must attempt
# `sudo -nE <self> <args>` BEFORE any root check or namespace mutation, and
# must not print the old "must run as root" fatal. Mock sudo (records argv,
# exits 0) via PATH injection; mock id to report a non-root uid.
MOCKBIN="$WORK/mockbin"
mkdir -p "$MOCKBIN"
cat >"$MOCKBIN/id" <<'EOF'
#!/usr/bin/env bash
case "$*" in
    -u) echo 1000 ;;
    *) echo "uid=1000(mock)" ;;
esac
EOF
cat >"$MOCKBIN/sudo" <<'EOF'
#!/usr/bin/env bash
echo "SUDO-REEXEC argv: $*"
exit 0
EOF
chmod +x "$MOCKBIN/id" "$MOCKBIN/sudo"
# The mock sudo exits 0, so the wrapper (exec'd into it) exits 0 and prints
# the recorded re-exec line. Env preservation is pinned by checking -nE.
out="$(PATH="$MOCKBIN:$PATH" bash "$REPO_ROOT/test/perf/working-set-netns.sh" /some/smoke.sh arg1 2>&1)" || true
case "$out" in
    *"SUDO-REEXEC argv: -nE $REPO_ROOT/test/perf/working-set-netns.sh /some/smoke.sh arg1"*) ;;
    *) fail "non-root wrapper did not sudo re-exec itself with -nE and original args: $out" ;;
esac
case "$out" in
    *"must run as root"*) fail "non-root wrapper hit the old premature root fatal" ;;
esac
# and no namespace may have been created before the re-exec
netns_exists "$(working_set_netns_name local)" \
    && fail "non-root wrapper mutated namespaces before privilege escalation"

# ---- 13. probe: owned collision is reclaimed, never a skip ------------------
# A SIGKILLed prior run's probe leftover (with a live tenant) must be
# reclaimed fail-closed and the suite must continue, not skip.
ip netns add "$WS_PROBE" >/dev/null 2>&1 || fail "cannot create probe leftover"
ip netns exec "$WS_PROBE" setsid sleep 300 >/dev/null 2>&1 < /dev/null 9>&- &
sleep 0.3
probe_netns_support || fail "probe treated an owned leftover as a capability skip"
netns_exists "$WS_PROBE" && fail "probe collision left the namespace behind"

# reclaim failure inside the probe is a FAIL, never a skip: an occupied
# probe that survives TERM+KILL must fatal (subshell) without deleting.
DELS_D="$WORK/del-d.calls"
rm -f "$DELS_D"
if (
    ip() {
        case "$*" in
            "netns list"*) echo "$WS_PROBE (id: 0)"; command ip netns list ;;
            *"netns pids $WS_PROBE"*) echo 424242 ;;
            *"netns del $WS_PROBE"*) echo called >>"$DELS_D"; return 0 ;;
            *) command ip "$@" ;;
        esac
    }
    probe_netns_support
) </dev/null >/dev/null 2>&1; then
    fail "probe succeeded although the owned leftover was unreclaimable"
fi
[ ! -e "$DELS_D" ] || fail "probe deleted an unreclaimable leftover's name"

# genuine capability lack (no leftover, add always fails) is the only skip.
# (mocks use `return`, not `exit`: these ip calls run directly in condition
# contexts, where an `exit` inside the function would kill the whole test
# subshell before the assertion could observe the probe's own return path)
if (
    ip() {
        case "$*" in
            "netns add $WS_PROBE"*) return 1 ;;
            *) command ip "$@" ;;
        esac
    }
    probe_netns_support
) </dev/null >/dev/null 2>&1; then
    fail "probe reported support although netns add always fails"
fi

# leftover reclaimed cleanly but the re-add fails: FAIL, never a skip
# (the leftover's existence demonstrated netns support).
probe_rc=0
probe_out="$( (
    ip() {
        case "$*" in
            "netns list"*) echo "$WS_PROBE (id: 0)"; command ip netns list ;;
            *"netns pids $WS_PROBE"*) : ;;
            "netns del $WS_PROBE"*) return 0 ;;
            "netns add $WS_PROBE"*) return 1 ;;
            *) command ip "$@" ;;
        esac
    }
    probe_netns_support
) </dev/null 2>&1 )" || probe_rc=$?
[ "$probe_rc" -ne 0 ] || fail "probe succeeded although the post-reclaim re-add failed"
case "$probe_out" in
    *FAIL*) ;;
    *) fail "post-reclaim re-add failure was misread as a capability skip: $probe_out" ;;
esac

# fresh probe created but the final delete fails: FAIL, never a skip —
# a cleanup failure is not a capability statement.
probe_rc=0
probe_out="$( (
    ip() {
        case "$*" in
            "netns add $WS_PROBE"*) return 0 ;;
            "netns del $WS_PROBE"*) echo "delete failed" >&2; return 1 ;;
            *) command ip "$@" ;;
        esac
    }
    probe_netns_support
) </dev/null 2>&1 )" || probe_rc=$?
[ "$probe_rc" -ne 0 ] || fail "probe succeeded although the fresh probe delete failed"
case "$probe_out" in
    *FAIL*) ;;
    *) fail "probe delete failure was misread as a capability skip: $probe_out" ;;
esac

# ---- 14. teardown with a surviving tenant stays best-effort, keeps the name -
# The cleanup path delegates to teardown; an unkillable tenant must not
# delete the deterministic name (it stays reclaimable by the next run).
DELS_E="$WORK/del-e.calls"
rm -f "$DELS_E"
if ! (
    ip() {
        case "$*" in
            "netns list"*) echo "wsnt-tsurv (id: 0)"; command ip netns list ;;
            *"netns pids wsnt-tsurv"*) echo 424242 ;;
            *"netns del wsnt-tsurv"*) echo called >>"$DELS_E"; return 0 ;;
            *) command ip "$@" ;;
        esac
    }
    working_set_netns_teardown wsnt-tsurv
) </dev/null >/dev/null 2>&1; then
    fail "teardown returned non-zero on a surviving tenant (must stay best-effort)"
fi
[ ! -e "$DELS_E" ] || fail "teardown deleted the namespace name despite a survivor"

echo "working_set_netns_test: PASS"
