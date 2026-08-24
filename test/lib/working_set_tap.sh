#!/usr/bin/env bash

# Helpers shared by the working-set performance harness and its unprivileged
# tests. The caller owns TAP creation and deletion.

working_set_tap_name() { # $1=mktemp work directory
    local suffix="${1##*/}"
    suffix="${suffix##*-}"
    suffix="${suffix//[^[:alnum:]]/}"
    [ -n "$suffix" ] || suffix="${BASHPID:-$$}"
    printf 'kws-%.11s\n' "$suffix"
}

working_set_validate_tap() { # $1=name, $2=host CIDR, $3=guest IP
    local name="$1" host_cidr="$2" guest_ip="$3"
    local link details addresses route flags
    local -a ipv4_addresses=()

    if ! link="$(ip -o link show dev "$name" 2>&1)"; then
        echo "working-set TAP $name is missing: $link" >&2
        return 1
    fi
    if ! details="$(ip -details link show dev "$name" 2>&1)"; then
        echo "cannot inspect working-set interface type for $name: $details" >&2
        return 1
    fi
    if ! grep -Eq '(^|[[:space:]])tun type tap([[:space:]]|$)' <<<"$details"; then
        echo "working-set interface $name is not TAP mode: $details" >&2
        return 1
    fi

    flags="${link#*<}"
    flags="${flags%%>*}"
    case ",$flags," in
        *,UP,*) ;;
        *)
            echo "working-set TAP $name is not UP: $link" >&2
            return 1
            ;;
    esac

    if ! addresses="$(ip -o -4 address show dev "$name" 2>&1)"; then
        echo "cannot inspect IPv4 addresses on working-set TAP $name: $addresses" >&2
        return 1
    fi
    mapfile -t ipv4_addresses < <(awk '$3 == "inet" { print $4 }' <<<"$addresses")
    if [ "${#ipv4_addresses[@]}" -ne 1 ] || [ "${ipv4_addresses[0]:-}" != "$host_cidr" ]; then
        echo "working-set TAP $name IPv4 addresses=${ipv4_addresses[*]:-(none)}, want exactly $host_cidr" >&2
        return 1
    fi

    if ! route="$(ip -o route get "$guest_ip" 2>&1)"; then
        echo "cannot resolve route to working-set guest $guest_ip: $route" >&2
        return 1
    fi
    case " $route " in
        *" dev $name "*) ;;
        *)
            echo "route to working-set guest $guest_ip does not use $name: $route" >&2
            return 1
            ;;
    esac
}

working_set_tap_counters() { # $1=name, $2=optional sysfs network root
    local name="$1" network_root="${2:-/sys/class/net}"
    local metric value first=1
    for metric in rx_packets rx_bytes tx_packets tx_bytes; do
        value=unavailable
        if [ -r "$network_root/$name/statistics/$metric" ]; then
            IFS= read -r value <"$network_root/$name/statistics/$metric"
        fi
        [ "$first" = 1 ] || printf ' '
        printf '%s=%s' "$metric" "$value"
        first=0
    done
    printf '\n'
}

working_set_dump_network_state() { # $1=name, $2=guest IP
    local name="$1" guest_ip="$2"

    echo "== TAP link and packet counters =="
    ip -details -statistics link show dev "$name" 2>&1 || true
    echo "== TAP IPv4 addresses =="
    ip -4 address show dev "$name" 2>&1 || true
    echo "== TAP neighbours =="
    ip neighbour show dev "$name" 2>&1 || true
    echo "== IPv4 routes =="
    ip -4 route show table all 2>&1 || true
    echo "== Route to guest =="
    ip route get "$guest_ip" 2>&1 || true
}

# Interfaces that hold the working-set host subnet. A stale TAP from an
# interrupted earlier run keeps its connected 169.254.1.0/31 route alive and
# can win route selection against the current run's TAP even though the
# current run owns a more specific /32 guest route (#43). Both reset points
# (pre-job reset, smoke startup) run before the current TAP exists, so every
# match is a leftover by definition.
# Uses `command ip` so callers that override ip() for other helpers (the
# unprivileged test mock) cannot break these host-mutating paths.
working_set_stale_taps() { # $1=host CIDR; prints stale names
    local host_cidr="$1" line
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        printf '%s\n' "$line"
    done < <(command ip -o -4 address show scope global 2>/dev/null \
        | awk -v cidr="$host_cidr" '$4 == cidr { print $2 }')
}

# Delete leftover TAPs that hold the working-set host subnet as their only
# IPv4 address, so unrelated host networking is preserved. Prints one
# "removed <name>" line per successful deletion.
working_set_remove_stale_taps() { # $1=host CIDR
    local host_cidr="$1" name details addresses
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        details="$(command ip -details link show dev "$name" 2>/dev/null || true)"
        grep -Eq '(^|[[:space:]])tun type tap([[:space:]]|$)' <<<"$details" || continue
        addresses="$(command ip -o -4 address show dev "$name" 2>/dev/null || true)"
        awk -v cidr="$host_cidr" '
            $3 == "inet" { count++; if ($4 == cidr) found = 1 }
            END { exit !(count == 1 && found) }
        ' <<<"$addresses" || continue
        if command ip link del "$name" 2>/dev/null; then
            printf 'removed %s\n' "$name"
        else
            printf 'failed to remove %s\n' "$name" >&2
        fi
    done < <(working_set_stale_taps "$host_cidr")
}

# Names of TAP interfaces created by this repository's test suites. Used by
# the pre-job reset to delete leftovers from interrupted runs; each carries a
# 169.254.1.0/31 connected route that can win route selection later (#43).
working_set_list_test_taps() { # prints matching interface names
    command ip -details -o link show 2>/dev/null \
        | awk '$2 ~ /^(sb-|kws-|etf)[^:]*:$/ && /tun type tap/ { sub(/:$/, "", $2); print $2 }'
}
