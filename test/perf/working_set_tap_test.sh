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

echo "working_set_tap_test: PASS"
