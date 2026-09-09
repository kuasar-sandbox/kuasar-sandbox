#!/usr/bin/env bash

# Safety helpers shared only by the two Demo entry points. This is deliberately
# not a general host resource manager: the caller still owns the concrete
# services, processes, network objects, and teardown order.

DEMO_OWNER_MARKER_VERSION=kuasar-demo-owner-v1

demo_die() {
    echo "  x $*" >&2
    exit 1
}

demo_select_owner() {
    [ "$(id -u)" -eq 0 ] || demo_die "run this Demo entry point as root (use sudo -n env with explicit paths)"
    DEMO_OWNER_UID=0
    DEMO_OWNER_GID=0
    export DEMO_OWNER_UID DEMO_OWNER_GID
}

demo_default_data_dir() {
    if [ -n "${DEMO_DATA_DIR:-}" ]; then
        return
    fi
    DEMO_DATA_DIR=/var/lib/kuasar-demo
    export DEMO_DATA_DIR
}

demo_validate_data_path() {
    case "$DEMO_DATA_DIR" in
        /*) ;;
        *) demo_die "DEMO_DATA_DIR must be an absolute path: $DEMO_DATA_DIR" ;;
    esac
    case "$DEMO_DATA_DIR" in
        /|/tmp|/var/tmp|/run|/home|/root) demo_die "unsafe DEMO_DATA_DIR: $DEMO_DATA_DIR" ;;
    esac
    [[ "$DEMO_DATA_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] \
        || demo_die "DEMO_DATA_DIR may contain only letters, digits, slash, dot, underscore, and hyphen"
    [ ! -L "$DEMO_DATA_DIR" ] || demo_die "DEMO_DATA_DIR must not be a symbolic link: $DEMO_DATA_DIR"
    [ "$(readlink -m "$DEMO_DATA_DIR")" = "$DEMO_DATA_DIR" ] \
        || demo_die "DEMO_DATA_DIR must be canonical and must not traverse symbolic-link parents: $DEMO_DATA_DIR"
}

demo_require_single_line() {
    local name="$1" value="$2"
    case "$value" in
        *$'\n'*|*$'\r'*) demo_die "$name must be a single-line value" ;;
    esac
}

demo_require_yaml_single_quoted() {
    local name="$1" value="$2"
    demo_require_single_line "$name" "$value"
    case "$value" in
        *"'"*) demo_die "$name must not contain a single quote in this Demo" ;;
    esac
}

demo_secure_owned_file() {
    local path="$1" mode="${2:-600}"
    if [ ! -f "$path" ] || [ -L "$path" ]; then
        demo_die "expected a regular private file: $path"
    fi
    chmod "$mode" "$path"
    if [ "$(id -u)" -eq 0 ]; then
        chown "$DEMO_OWNER_UID:$DEMO_OWNER_GID" "$path"
    fi
}

demo_init_data_dir() {
    demo_validate_data_path
    local marker="$DEMO_DATA_DIR/.kuasar-demo-owner" created=0
    if [ ! -e "$DEMO_DATA_DIR" ]; then
        install -d -m 0700 "$DEMO_DATA_DIR"
        created=1
        if [ "$(id -u)" -eq 0 ]; then
            chown "$DEMO_OWNER_UID:$DEMO_OWNER_GID" "$DEMO_DATA_DIR"
        fi
    fi
    [ -d "$DEMO_DATA_DIR" ] || demo_die "DEMO_DATA_DIR is not a directory: $DEMO_DATA_DIR"
    [ "$(stat -c %u "$DEMO_DATA_DIR")" = "$DEMO_OWNER_UID" ] \
        || demo_die "DEMO_DATA_DIR is owned by uid $(stat -c %u "$DEMO_DATA_DIR"), expected $DEMO_OWNER_UID"
    chmod 0700 "$DEMO_DATA_DIR"

    if [ ! -e "$marker" ]; then
        if [ "$created" -eq 0 ] && find "$DEMO_DATA_DIR" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
            demo_die "$DEMO_DATA_DIR is nonempty but has no Kuasar Demo ownership marker; refusing to adopt it"
        fi
        local marker_tmp="$DEMO_DATA_DIR/.owner.$$"
        (umask 077; printf '%s\nuid=%s\ngid=%s\n' \
            "$DEMO_OWNER_MARKER_VERSION" "$DEMO_OWNER_UID" "$DEMO_OWNER_GID" >"$marker_tmp")
        demo_secure_owned_file "$marker_tmp"
        if ! (set -o noclobber; : >"$marker") 2>/dev/null; then
            rm -f "$marker_tmp"
            demo_die "could not reserve ownership marker $marker"
        fi
        mv -f "$marker_tmp" "$marker"
    fi
    if [ ! -f "$marker" ] || [ -L "$marker" ]; then
        demo_die "invalid ownership marker: $marker"
    fi
    [ "$(stat -c %u "$marker")" = "$DEMO_OWNER_UID" ] \
        || demo_die "ownership marker has unexpected uid"
    [ "$(stat -c %a "$marker")" = 600 ] || demo_die "ownership marker must have mode 0600"
    local marker_version marker_uid marker_gid marker_extra=0
    {
        IFS= read -r marker_version
        IFS= read -r marker_uid
        IFS= read -r marker_gid
        if IFS= read -r _; then marker_extra=1; fi
    } <"$marker"
    if [ "$marker_version" != "$DEMO_OWNER_MARKER_VERSION" ] \
        || [ "$marker_uid" != "uid=$DEMO_OWNER_UID" ] \
        || [ "$marker_gid" != "gid=$DEMO_OWNER_GID" ] \
        || [ "$marker_extra" -ne 0 ]; then
        demo_die "ownership marker does not match this Demo owner"
    fi
}

demo_validate_handoff() {
    local path="$1"
    if [ ! -f "$path" ] || [ -L "$path" ]; then
        demo_die "missing private Demo handoff: $path"
    fi
    [ "$(stat -c %u "$path")" = "$DEMO_OWNER_UID" ] \
        || demo_die "Demo handoff is not owned by uid $DEMO_OWNER_UID: $path"
    [ "$(stat -c %a "$path")" = 600 ] \
        || demo_die "Demo handoff must have mode 0600: $path"
}
