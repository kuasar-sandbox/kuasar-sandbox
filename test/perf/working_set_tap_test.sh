#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=test/lib/working_set_tap.sh
. "$REPO_ROOT/test/lib/working_set_tap.sh"

WORK="$(mktemp -d /tmp/working-set-tap-test-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

fail() {
    echo "working_set_tap_test: $*" >&2
    exit 1
}

TAP=kws-ABC123
HOST_CIDR=169.254.1.0/31
GUEST_IP=169.254.1.1
SCENARIO=healthy

ip() {
    case "$*" in
        "-o link show dev $TAP")
            if [ "$SCENARIO" = down ]; then
                echo "9: $TAP: <BROADCAST,MULTICAST> mtu 1500 state DOWN"
            else
                echo "9: $TAP: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP"
            fi
            ;;
        "-details link show dev $TAP")
            if [ "$SCENARIO" = tun ]; then
                echo "    tun type tun pi off persist on"
            else
                echo "    tun type tap pi off persist on"
            fi
            ;;
        "-o -4 address show dev $TAP")
            if [ "$SCENARIO" = address ]; then
                echo "9: $TAP inet 192.0.2.1/31 scope global $TAP"
            else
                echo "9: $TAP inet $HOST_CIDR scope global $TAP"
            fi
            ;;
        "-o route get $GUEST_IP")
            if [ "$SCENARIO" = route ]; then
                echo "$GUEST_IP dev stale-tap src 169.254.1.0"
            else
                echo "$GUEST_IP dev $TAP src 169.254.1.0"
            fi
            ;;
        *)
            echo "unexpected ip invocation: $*" >&2
            return 64
            ;;
    esac
}

[ "$(working_set_tap_name /tmp/perf-working-set-ABC123)" = "$TAP" ] \
    || fail "default TAP name is not derived from the unique work directory"
working_set_validate_tap "$TAP" "$HOST_CIDR" "$GUEST_IP" \
    || fail "healthy TAP did not validate"

for scenario in tun down address route; do
    SCENARIO="$scenario"
    if working_set_validate_tap "$TAP" "$HOST_CIDR" "$GUEST_IP" \
        >"$WORK/$scenario.log" 2>&1; then
        fail "$scenario scenario unexpectedly validated"
    fi
    [ -s "$WORK/$scenario.log" ] || fail "$scenario failure was not actionable"
done

mkdir -p "$WORK/sys/$TAP/statistics"
for metric in rx_packets rx_bytes tx_packets tx_bytes; do
    printf '%s\n' "${#metric}" >"$WORK/sys/$TAP/statistics/$metric"
done
counters="$(working_set_tap_counters "$TAP" "$WORK/sys")"
for metric in rx_packets rx_bytes tx_packets tx_bytes; do
    case " $counters " in
        *" $metric=${#metric} "*) ;;
        *) fail "counter output is missing $metric: $counters" ;;
    esac
done

# ---- stale TAP enumeration and reset (#43) ------------------------------
# The ip() mock above only covers the validate path; the list/remove checks
# below need the real command and root TAP creation, otherwise skip them.
real_ip() { command ip "$@"; }
if [ "$(id -u)" -ne 0 ]; then
    echo "working_set_tap_test: stale TAP reset check skipped (needs root TAP)" >&2
elif ! real_ip tuntap add dev kws-stale9 mode tap >/dev/null 2>&1; then
    echo "working_set_tap_test: stale TAP reset check skipped (cannot create TAP)" >&2
else
    # TAP now exists: every later failure is a real test failure, and the
    # trap guarantees the interface cannot leak even then.
    trap 'rm -rf "$WORK"; real_ip link del kws-stale9 2>/dev/null || true' EXIT
    real_ip address add "$HOST_CIDR" dev kws-stale9 \
        || fail "cannot configure $HOST_CIDR on kws-stale9"
    real_ip link set kws-stale9 up \
        || fail "cannot bring kws-stale9 UP"

    case " $(working_set_list_test_taps | tr '\n' ' ') " in
        *" kws-stale9 "*) ;;
        *) fail "working_set_list_test_taps did not list stale kws-stale9" ;;
    esac

    removed="$(working_set_remove_stale_taps "$HOST_CIDR")"
    case "$removed" in
        *"kws-stale9"*) ;;
        *) fail "working_set_remove_stale_taps did not remove kws-stale9: ${removed:-(none)}" ;;
    esac
    real_ip link show dev kws-stale9 >/dev/null 2>&1 \
        && fail "kws-stale9 still exists after working_set_remove_stale_taps"
fi

echo "working_set_tap_test: PASS"
