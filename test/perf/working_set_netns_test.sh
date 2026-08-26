#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

fail() {
    echo "working_set_netns_test: FAIL: $*" >&2
    exit 1
}

# Non-root / no-netns environments: the skip must be reachable WITHOUT the
# wrapper's own root check firing first (the root check lives below the
# SOURCED guard precisely for this).
if [ "$(id -u)" -ne 0 ] || ! ip netns add wsnt-probe >/dev/null 2>&1; then
    echo "working_set_netns_test: skipped (needs root network namespaces)" >&2
    exit 0
fi
ip netns del wsnt-probe

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

track() { CLEANED_NETNSES+=("$1"); }

netns_exists() { ip netns list | awk '{print $1}' | grep -Fxq "$1"; }

# ---- 1. deterministic naming ----------------------------------------------
A="$(working_set_netns_name runner-alpha)"
B="$(working_set_netns_name runner-beta)"
[ "$A" != "$B" ] || fail "two runners derived the same namespace name"
[ "$(working_set_netns_name runner-alpha)" = "$A" ] \
    || fail "namespace name is not deterministic across calls"
case "$A" in kuasar-ws-????????) ;; *) fail "unexpected namespace name format: $A" ;; esac

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

RUNNER_NAME=wsnt-wrapper-a bash "$REPO_ROOT/test/perf/working-set-netns.sh" "$FAKE" \
    >"$WORK/wrap-ok.out" 2>&1 \
    || fail "wrapper failed for a green smoke: $(cat "$WORK/wrap-ok.out")"
grep -q FAKE-NETNS "$WORK/wrap-ok.out" \
    || fail "green smoke did not run inside the namespace"
ok_netns="$(sed -n 's/^FAKE-NETNS=//p' "$WORK/wrap-ok.out")"
rc=0
FAKE_EXIT=42 RUNNER_NAME=wsnt-wrapper-a bash "$REPO_ROOT/test/perf/working-set-netns.sh" \
    "$FAKE" >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 42 ] \
    || fail "wrapper did not propagate smoke exit 42 (got $rc)"
[ -n "$ok_netns" ] || fail "missing green-run netns inode"
# both runs must have torn their namespace down (same deterministic name)
netns_exists "$(working_set_netns_name wsnt-wrapper-a)" \
    && fail "wrapper left its namespace behind after exit"

# smoke failure path also tears down: run once more exiting 1 and check
RUNNER_NAME=wsnt-wrapper-a FAKE_EXIT=1 bash "$REPO_ROOT/test/perf/working-set-netns.sh" "$FAKE" \
    >/dev/null 2>&1 && fail "wrapper swallowed smoke failure (exit 0)"
netns_exists "$(working_set_netns_name wsnt-wrapper-a)" \
    && fail "wrapper left its namespace behind after failed smoke"

# ---- 5. SIGTERM to the wrapper triggers teardown ---------------------------
WRAPPER="$REPO_ROOT/test/perf/working-set-netns.sh"
FAKE_SLEEP=30 FAKE_EXIT=0 RUNNER_NAME=wsnt-sigterm setsid bash "$WRAPPER" "$FAKE" \
    >/dev/null 2>&1 &
WRAP_PID=$!
# give the wrapper time to create its namespace, then TERM it
sig_ns="$(working_set_netns_name wsnt-sigterm)"
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
ip netns exec "$sig_ns" setsid sleep 300 >/dev/null 2>&1 < /dev/null &
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
ip netns exec "$A" setsid sleep 300 >/dev/null 2>&1 < /dev/null &
sleep 0.2
ip netns add "$B"; track "$B"
ip netns exec "$B" ip link set lo up
ip netns exec "$B" setsid sleep 300 >/dev/null 2>&1 < /dev/null &
# host-side stale TAP on the shared subnet, same name the smoke uses inside
ip tuntap add dev kws0 mode tap
ip address add 169.254.1.0/31 dev kws0
ip link set kws0 up
sleep 0.3
# both tenants live before the reclaim
[ -n "$(ip netns pids "$A")" ] || fail "A's tenant was not running before reclaim"
[ -n "$(ip netns pids "$B")" ] || fail "B's tenant was not running before reclaim"
working_set_netns_reclaim "$A" || fail "reclaim of a live A failed"
netns_exists "$A" && fail "reclaim left A's namespace behind"
[ -z "$(ip netns pids "$A" 2>/dev/null)" ] || fail "reclaim left A's tenant alive"
netns_exists "$B" || fail "reclaiming runner A deleted runner B's namespace"
[ -n "$(ip netns pids "$B")" ] || fail "reclaiming runner A killed runner B's process"
ip link show dev kws0 >/dev/null 2>&1 || fail "reclaiming runner A deleted the host kws0"
working_set_netns_kill_all "$B"
ip netns del "$B"
ip link del kws0

# ---- 10. host same-name same-subnet TAP invisible to namespaced runs --------
ip tuntap add dev kws0 mode tap
ip address add 169.254.1.0/31 dev kws0
ip link set kws0 up
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
ip link show dev kws0 >/dev/null 2>&1 || fail "host kws0 vanished after namespaced run"
working_set_netns_kill_all "$A"
ip netns del "$A"
ip link del kws0

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

echo "working_set_netns_test: PASS"
