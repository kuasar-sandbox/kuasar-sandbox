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
# The ip() mock above only covers the validate path. The live check below
# unsets it first: working_set_reset_tap calls plain `ip` internally, and
# with the mock still defined those calls would hit the mock, fail, and be
# swallowed by the helper's || true — the TAP would never flap, the
# already-installed /32 would remain, and the final route check would pass
# without testing anything.
real_ip() { command ip "$@"; }
# Deterministic owner identity (same model as working_set_netns_test): a
# SIGKILLed run leaks this TAP with no trap to clean it, and only the next
# run deriving the SAME name can reclaim it. Random names would leak forever.
FLAP_TAP="kws-flap-$(printf '%s' "${RUNNER_NAME:-$(hostname)}" | sha256sum | cut -c1-6)"
FLAP_LOCK="/tmp/wstt-$(printf '%s' "${RUNNER_NAME:-$(hostname)}" | sha256sum | cut -c1-8).lock"
if [ "$(id -u)" -ne 0 ]; then
    echo "working_set_tap_test: reset_tap check skipped (needs root TAP)" >&2
else
    # deterministic names are shared state: serialize same-owner runs
    exec 9>"$FLAP_LOCK"
    flock -w 60 9 || fail "another working_set_tap_test live run holds $FLAP_LOCK"
    # recover a SIGKILLed prior run's flap TAP by exact derived name
    if real_ip link show dev "$FLAP_TAP" >/dev/null 2>&1; then
        real_ip link del "$FLAP_TAP" || fail "cannot delete leftover flap TAP $FLAP_TAP"
    fi
    if ! real_ip tuntap add dev "$FLAP_TAP" mode tap >/dev/null 2>&1; then
        echo "working_set_tap_test: reset_tap check skipped (cannot create TAP)" >&2
    else
        # TAP now exists: every later failure is a real test failure, and the
        # trap guarantees the interface cannot leak even then.
        trap 'rm -rf "$WORK"; real_ip link del "$FLAP_TAP" 2>/dev/null || true' EXIT
        real_ip address add "$HOST_CIDR" dev "$FLAP_TAP" \
            || fail "cannot configure $HOST_CIDR on $FLAP_TAP"
        real_ip link set "$FLAP_TAP" up \
            || fail "cannot bring $FLAP_TAP UP"
        real_ip route add "$GUEST_IP/32" dev "$FLAP_TAP" src "${HOST_CIDR%/*}" \
            || fail "cannot install the guest /32 on $FLAP_TAP"

        unset -f ip

        # Control: a plain down/up flap must withdraw the admin-installed /32
        # (the kernel re-adds only the connected /31 on up). This is what gives
        # the final assertion teeth — without it, a no-op reset_tap would pass.
        real_ip link set "$FLAP_TAP" down
        real_ip link set "$FLAP_TAP" up
        if real_ip -o route show dev "$FLAP_TAP" | grep -q "^$GUEST_IP "; then
            fail "plain flap did not withdraw the guest /32; final check has no teeth"
        fi

        working_set_reset_tap "$FLAP_TAP" "$HOST_CIDR" "${HOST_CIDR%/*}" "$GUEST_IP"

        real_ip -o route show dev "$FLAP_TAP" | grep -q "^$GUEST_IP " \
            || fail "guest /32 route missing after working_set_reset_tap: $(real_ip -o route show dev "$FLAP_TAP")"
    fi
fi

echo "working_set_tap_test: PASS"
