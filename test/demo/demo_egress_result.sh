#!/usr/bin/env bash
# Shared strict result handling for the guest egress probe.
egress_handle_result() {
    local status="$1" output="$2" proxy="$3"
    if [ -n "$output" ]; then
        printf '%s\n' "$output"
    fi
    if [ "$status" -eq 0 ]; then
        ok "guest egress probe passed"
        return 0
    fi
    if [ -n "${DEMO_NETDIAG:-}" ]; then
        say "Guest egress probe failed (NETDIAG: continuing)"
        return 0
    fi
    if [ -n "$proxy" ]; then
        die "guest outbound proxy validation failed"
    else
        die "guest outbound NAT validation failed"
    fi
}
