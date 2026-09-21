#!/usr/bin/env bash
# BASH_ENV for two existing cases; all other shells retain their normal traps.
case "${0##*/}:${CLUSTER_STUB_CASE:-}" in
    e2e_sandbox_read_recovery.sh:*) _kd_case=read-recovery ;;
    e2e_cluster_stub.sh:registry-n3) _kd_case=registry-n3 ;;
    *) return 0 ;;
esac
[[ -n ${KUASAR_CI_DIR:-} ]] || return 0
_kd_reader="${BASH_SOURCE[0]%/*}/failure_diagnostics.py"
_kd_env=${BASH_SOURCE[0]}
_kd_argv=("$@")
_kd_phase=setup _kd_line=0 _kd_source="${0##*/}" _kd_error=0

_kd_debug() {
    # Inspect only unexpanded commands to select fixed labels. Never log them.
    [[ ${2##*/} = "${0##*/}" ]] || return 0
    _kd_line=$1 _kd_source=${2##*/}
    case "$3" in
        'exec sudo -nE bash "$0" "$@"')
            # Preserve this existing privileged re-exec despite sudo filtering
            # BASH_ENV. Never shadow exec: that changes {fd}>&- semantics.
            if [[ $_kd_case = read-recovery ]]; then
                builtin exec sudo -nE env BASH_ENV="$_kd_env" bash "$0" "${_kd_argv[@]}"
            fi ;;
        'launch cow '*) _kd_phase=cow-read ;;
        'launch seed '*) _kd_phase=seed-start ;;
        'SNAP='*) _kd_phase=seed-snapshot ;;
        'launch recovered '*) _kd_phase=restore-recovery ;;
        'CAPTURE_FAULT_BASE='*) _kd_phase=capture-outage ;;
        'RETRY_BASE='*) _kd_phase=cache-reconnect ;;
        'wait "$CAPTURE_PID"'*) _kd_phase=capture-completion ;;
        'launch verified '*) _kd_phase=restore-verification ;;
        'FATAL_FAULT_BASE='*) _kd_phase=fatal-source ;;
        'step "checking build_register"') _kd_phase=build-register ;;
        'step "checking build follow-up forwarding through the node API endpoint"') _kd_phase=build-status ;;
    esac
    return 0
}
_kd_err() {
    _kd_error=$1 _kd_error_line=$2 _kd_error_source=${3##*/}
    return 0
}
_kd_status() { return "$1"; }
_kd_exit() {
    local result=$1
    builtin trap - DEBUG ERR EXIT
    if (( result != 0 )); then
        if (( _kd_error != 0 )); then
            _kd_line=$_kd_error_line _kd_source=$_kd_error_source
        fi
        printf 'E2E failure: case=%s phase=%s source=%s line=%s exit=%s\n' \
            "$_kd_case" "$_kd_phase" "$_kd_source" "$_kd_line" "$result" >&2
        python3 "$_kd_reader" "$_kd_case" "$_kd_phase" "$_kd_source" "$_kd_line" "$result" \
            "${WORK:-}" "${KUASAR_E2E_DIAGNOSTICS_DIR:-$KUASAR_CI_DIR/e2e-failures}" "${code:-}" \
            || printf 'E2E failure diagnostics unavailable (original failure retained)\n' >&2
    fi
    # Give the original cleanup its original $?; a cleanup/collector failure
    # must not replace an already-failing test's status. Success still requires
    # cleanup to succeed. Neither test assertions nor their errexit change.
    if _kd_status "$result"; then
        cleanup
    else
        cleanup || :
    fi
    exit "$result"
}
trap() {
    # Both cases install this exact EXIT handler. Do not interpret arbitrary
    # trap bodies or change other signals, conditions, retries or timeouts.
    if [[ $# = 2 && $1 = cleanup && $2 = EXIT ]]; then
        builtin trap '_kd_exit "$?"' EXIT
    else
        builtin trap "$@"
    fi
}
set -E
builtin trap '_kd_err "$?" "$LINENO" "${BASH_SOURCE[0]}"' ERR
builtin trap '_kd_debug "$LINENO" "${BASH_SOURCE[0]}" "$BASH_COMMAND"' DEBUG
