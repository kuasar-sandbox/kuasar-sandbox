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

# ---- reset_tap /32 restoration (#43) ------------------------------------
# The ip() mock above only covers the validate path; the flap/replace check
# below needs the real command and root TAP creation, otherwise skip it.
real_ip() { command ip "$@"; }
if [ "$(id -u)" -ne 0 ]; then
    echo "working_set_tap_test: reset_tap check skipped (needs root TAP)" >&2
elif ! real_ip tuntap add dev kws-flap0 mode tap >/dev/null 2>&1; then
    echo "working_set_tap_test: reset_tap check skipped (cannot create TAP)" >&2
else
    # TAP now exists: every later failure is a real test failure, and the
    # trap guarantees the interface cannot leak even then.
    trap 'rm -rf "$WORK"; real_ip link del kws-flap0 2>/dev/null || true' EXIT
    real_ip address add "$HOST_CIDR" dev kws-flap0 \
        || fail "cannot configure $HOST_CIDR on kws-flap0"
    real_ip link set kws-flap0 up \
        || fail "cannot bring kws-flap0 UP"
    real_ip route add "$GUEST_IP/32" dev kws-flap0 src "${HOST_CIDR%/*}" \
        || fail "cannot install the guest /32 on kws-flap0"

    working_set_reset_tap kws-flap0 "$HOST_CIDR" "${HOST_CIDR%/*}" "$GUEST_IP"

    real_ip -o route show dev kws-flap0 | grep -q "^$GUEST_IP " \
        || fail "guest /32 route missing after working_set_reset_tap: $(real_ip -o route show dev kws-flap0)"
fi

echo "working_set_tap_test: PASS"
