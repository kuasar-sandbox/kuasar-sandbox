#!/usr/bin/env bash
# Bounded diagnostics for selected cases; every other shell is unchanged.
case "${0##*/}" in
    snapshot.read-recovery.sh|orchestrator.cluster-recovery.sh|sandbox.cgroup.sh|orchestrator.resource-startup.sh|orchestrator.proxy-inplace-restart.sh) _kd_case=${0##*/} ;;
    *) return 0 ;;
esac
[[ -n ${KUASAR_CI_DIR:-} ]] || return 0
_kd_reader="${BASH_SOURCE[0]%/*}/failure_diagnostics.py"
_kd_phase=setup _kd_line=0 _kd_source="${0##*/}" _kd_error=0 _kd_command=case-command

_kd_debug() {
    # Inspect only unexpanded commands to select fixed labels. Never log them.
    [[ ${2##*/} = "${0##*/}" ]] || return 0
    _kd_line=$1 _kd_source=${2##*/}
    case "$3" in
        'launch cow '*) _kd_phase=cow-read ;;
        'launch seed '*) _kd_phase=seed-start ;;
        'SNAP='*) _kd_phase=seed-snapshot ;;
        'launch recovered '*) _kd_phase=restore-recovery ;;
        'CAPTURE_FAULT_BASE='*) _kd_phase=capture-outage ;;
        'RETRY_BASE='*) _kd_phase=cache-reconnect ;;
        'wait "$CAPTURE_PID"'*) _kd_phase=capture-completion ;;
        'launch verified '*) _kd_phase=restore-verification ;;
        'FATAL_FAULT_BASE='*) _kd_phase=fatal-source ;;
        'REDIRECT_SELECTION='*) _kd_phase=registry-selection ;;
        'run_redirect_flow'*) _kd_phase=registry-recovery ;;
        'run_case '*) _kd_phase=cgroup-probe ;;
        'run_low_allocatable_case '*) _kd_phase=low-allocatable ;;
        'python3 "$SCRIPT_DIR/proxy_lifecycle_client.py"'*) _kd_phase=proxy-lifecycle ;;
    esac
    return 0
}
_kd_err() {
    _kd_error=$1 _kd_error_line=$2 _kd_error_source=${3##*/}
    # errexit can unwind a function before EXIT; keep only its task-local
    # selectors while ERR still has access to the failing function's locals.
    case "$_kd_case" in
        sandbox.cgroup.sh) _kd_failed_pid=${run_pid:-${_kd_failed_pid:-}} ;;
        orchestrator.resource-startup.sh)
            _kd_failed_unit=${LOW_UNIT:-${_kd_failed_unit:-}}
            _kd_failed_pid=${LOW_CTL_PID:-${_kd_failed_pid:-}}
            _kd_failed_cgroup=${LOW_CG:-${_kd_failed_cgroup:-}} ;;
    esac
    case "$4" in
        '"$BIN/sandbox-ctl" exec '*) _kd_command=sandbox-ctl-exec ;;
        'python3 "$SCRIPT_DIR/proxy_lifecycle_client.py" '*) _kd_command=proxy-lifecycle-client ;;
    esac
    return 0
}
_kd_status() { return "$1"; }
_kd_exit() {
    local result=$1 handler=$2
    builtin trap - DEBUG ERR EXIT
    if (( result != 0 )); then
        if (( _kd_error != 0 )); then
            _kd_line=$_kd_error_line _kd_source=$_kd_error_source
        fi
        printf 'E2E failure: case=%s phase=%s source=%s line=%s exit=%s\n' \
            "$_kd_case" "$_kd_phase" "$_kd_source" "$_kd_line" "$result" >&2
        local work=${WORK:-} unit= pid= cgroup=
        case "$_kd_case" in
            sandbox.cgroup.sh) work=${OUT:-}; pid=${run_pid:-${_kd_failed_pid:-}} ;;
            orchestrator.resource-startup.sh)
                unit=${LOW_UNIT:-${_kd_failed_unit:-}}; pid=${LOW_CTL_PID:-${_kd_failed_pid:-}}
                cgroup=${LOW_CG:-${_kd_failed_cgroup:-}} ;;
            orchestrator.proxy-inplace-restart.sh) unit=${service:-} ;;
        esac
        python3 "$_kd_reader" "$_kd_case" "$_kd_phase" "$_kd_source" "$_kd_line" "$result" \
            "$work" "$KUASAR_CI_DIR/e2e-failures" "${code:-}" "$unit" "$pid" "$cgroup" "$_kd_command" \
            || printf 'E2E failure diagnostics unavailable (original failure retained)\n' >&2
    fi
    # Give the original cleanup its original $?; a cleanup/collector failure
    # must not replace an already-failing test's status. Success still requires
    # cleanup to succeed. Neither test assertions nor their errexit change.
    if _kd_status "$result"; then
        "$handler"
    else
        "$handler" || :
    fi
    exit "$result"
}
trap() {
    # Intercept only these maintained EXIT handlers. Do not interpret arbitrary
    # trap bodies or change other signals, conditions, retries or timeouts.
    if [[ $# = 2 && $1 = cleanup && $2 = EXIT ]]; then
        builtin trap '_kd_exit "$?" cleanup' EXIT
    elif [[ $_kd_case = orchestrator.proxy-inplace-restart.sh && $# = 2 && $1 = lifecycle_cleanup && $2 = EXIT ]]; then
        builtin trap '_kd_exit "$?" lifecycle_cleanup' EXIT
    else
        builtin trap "$@"
    fi
}
set -E
builtin trap '_kd_err "$?" "$LINENO" "${BASH_SOURCE[0]}" "$BASH_COMMAND"' ERR
builtin trap '_kd_debug "$LINENO" "${BASH_SOURCE[0]}" "$BASH_COMMAND"' DEBUG
