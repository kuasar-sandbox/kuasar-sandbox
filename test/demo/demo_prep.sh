#!/usr/bin/env bash
#
# Persistent, run-owned prerequisites for demo_e2b.sh:
#   store-ctl + cache-ctl over private Unix sockets, an OCI registry, and
#   optional versitygw storage for the complete Demo's COPY build step.
#
# Every stateful file lives below DEMO_DATA_DIR. The directory has an explicit
# owner marker and mode 0700; the credential handoff has mode 0600. Service PID
# records include both executable and Linux process start time, so a stale PID
# is never used to kill an unrelated process.
#
#   sudo -n env DEMO_DATA_DIR="$PWD/.kuasar-demo" bash test/demo/demo_prep.sh
#   sudo -n env DEMO_DATA_DIR="$PWD/.kuasar-demo" bash test/demo/demo_prep.sh stop
#   sudo -n env DEMO_DATA_DIR="$PWD/.kuasar-demo" bash test/demo/demo_prep.sh reset

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=demo_common.sh
. "$SCRIPT_DIR/demo_common.sh"

demo_select_owner
demo_default_data_dir
demo_validate_data_path

BIN="${BIN:-$REPO_ROOT/bin}"
case "$BIN" in /*) ;; *) BIN="$PWD/$BIN" ;; esac
RUN_DIR="${RUN_DIR:-$DEMO_DATA_DIR/run}"
case "$RUN_DIR/" in "$DEMO_DATA_DIR"/*) ;; *) demo_die "RUN_DIR must stay below DEMO_DATA_DIR" ;; esac

ENV_FILE="$DEMO_DATA_DIR/prep.env"
PID_DIR="$DEMO_DATA_DIR/pids"
LOG_DIR="$DEMO_DATA_DIR/logs"
STORE_SOCK="${STORE_SOCK:-$RUN_DIR/store.sock}"
CACHE_SOCK="${CACHE_SOCK:-$RUN_DIR/cache.sock}"
CACHE_HEALTH_SOCK="${CACHE_HEALTH_SOCK:-$RUN_DIR/cache-health.sock}"
for socket_path in "$STORE_SOCK" "$CACHE_SOCK" "$CACHE_HEALTH_SOCK"; do
    case "$socket_path" in "$RUN_DIR"/*) ;; *) demo_die "Demo socket must stay below $RUN_DIR: $socket_path" ;; esac
done

# This linux/amd64 manifest digest was resolved when this script was updated.
# The acceptance run must exercise it with the pinned SDK pair in
# requirements.txt. Overrides should likewise use immutable digest references
# rather than a moving tag.
E2E_IMAGE="${E2E_IMAGE:-$DEMO_DEFAULT_E2E_IMAGE}"
REGISTRY_NS="${REGISTRY_NS:-e2b}"
ZOT_BIN="${ZOT_BIN:-$(command -v zot 2>/dev/null || true)}"
VGW_BIN="${VGW_BIN:-$(command -v versitygw 2>/dev/null || true)}"
VGW_PORT="${VGW_PORT:-5050}"
VGW_BUCKET="${VGW_BUCKET:-build-files}"
VGW_ACCESS_KEY="${VGW_ACCESS_KEY:-demoaccesskey}"
VGW_SECRET_KEY="${VGW_SECRET_KEY:-demosecretkey0123456}"
VGW_ENDPOINT=""

# Values received through sudo/env may carry Bash's export attribute. Keep
# credentials in non-exported shell variables so unrelated long-lived services
# cannot inherit them through /proc/<pid>/environ.
REGISTRY_USER_VALUE="${REGISTRY_USER:-}"
REGISTRY_PASS_VALUE="${REGISTRY_PASS:-}"
VGW_ACCESS_KEY_VALUE="$VGW_ACCESS_KEY"
VGW_SECRET_KEY_VALUE="$VGW_SECRET_KEY"
unset REGISTRY_USER REGISTRY_PASS VGW_ACCESS_KEY VGW_SECRET_KEY

demo_require_single_line REGISTRY_USER "$REGISTRY_USER_VALUE"
demo_require_single_line REGISTRY_PASS "$REGISTRY_PASS_VALUE"
demo_require_yaml_single_quoted VGW_ACCESS_KEY "$VGW_ACCESS_KEY_VALUE"
demo_require_yaml_single_quoted VGW_SECRET_KEY "$VGW_SECRET_KEY_VALUE"
[[ "$REGISTRY_NS" =~ ^[a-z0-9]+([._/-][a-z0-9]+)*$ ]] \
    || demo_die "REGISTRY_NS is not a safe lowercase registry path"
[[ "$VGW_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] \
    || demo_die "VGW_BUCKET must be a 3-63 character lowercase S3 bucket name"

c_ok=$'\e[1;32m'; c_dim=$'\e[2m'; c_off=$'\e[0m'
[ -t 1 ] || { c_ok=; c_dim=; c_off=; }
say() { echo "${c_dim}  - $*${c_off}"; }
ok()  { echo "${c_ok}  + $*${c_off}"; }

proc_start_time() {
    local pid="$1" stat rest
    local -a fields
    IFS= read -r stat <"/proc/$pid/stat" || return 1
    rest="${stat##*) }"
    read -r -a fields <<<"$rest"
    [ "${#fields[@]}" -ge 20 ] || return 1
    printf '%s\n' "${fields[19]}"
}

record_state() { # name; 0=owned live, 1=absent/dead, 2=invalid or live mismatch
    local name="$1" record current_start current_exe
    record="$PID_DIR/$name.pid"
    REC_PID="" REC_START="" REC_EXE=""
    [ -e "$record" ] || return 1
    [ -f "$record" ] && [ ! -L "$record" ] || return 2
    IFS=$'\t' read -r REC_PID REC_START REC_EXE <"$record" || return 2
    [[ "$REC_PID" =~ ^[1-9][0-9]*$ && "$REC_START" =~ ^[0-9]+$ && "$REC_EXE" = /* ]] || return 2
    if ! kill -0 "$REC_PID" 2>/dev/null; then
        return 1
    fi
    current_start="$(proc_start_time "$REC_PID" 2>/dev/null || true)"
    current_exe="$(readlink -f "/proc/$REC_PID/exe" 2>/dev/null || true)"
    [ "$current_start" = "$REC_START" ] && [ "$current_exe" = "$REC_EXE" ] || return 2
    return 0
}

declare -a STARTED_NOW=()
start_owned() { # name command...
    local name="$1" requested_exe state pid start_time; shift
    requested_exe="$(readlink -f "$1" 2>/dev/null || true)"
    [ -n "$requested_exe" ] || demo_die "cannot resolve executable for $name: $1"
    if record_state "$name"; then
        [ "$REC_EXE" = "$requested_exe" ] \
            || demo_die "$name is already owned by another executable: $REC_EXE"
        say "$name already running (pid $REC_PID)"
        return 0
    else
        state=$?
    fi
    [ "$state" -eq 1 ] || demo_die "$PID_DIR/$name.pid does not identify an owned process; refusing to replace or kill it"
    rm -f "$PID_DIR/$name.pid"
    setsid "$@" >"$LOG_DIR/$name.log" 2>&1 </dev/null 200>&- &
    pid=$!
    sleep 0.1
    kill -0 "$pid" 2>/dev/null || { wait "$pid" 2>/dev/null || true; demo_die "$name exited during startup (see $LOG_DIR/$name.log)"; }
    start_time="$(proc_start_time "$pid")" || demo_die "cannot read $name process identity"
    printf '%s\t%s\t%s\n' "$pid" "$start_time" "$requested_exe" >"$PID_DIR/$name.pid"
    chmod 0600 "$PID_DIR/$name.pid"
    STARTED_NOW+=("$name")
    ok "$name started (pid $pid)"
}

stop_owned() {
    local name="$1" state sid
    if record_state "$name"; then
        sid="$(ps -o sid= -p "$REC_PID" 2>/dev/null | tr -d ' ')"
        if [ "$sid" = "$REC_PID" ]; then
            kill -TERM -- "-$REC_PID" 2>/dev/null || true
        else
            kill -TERM "$REC_PID" 2>/dev/null || true
        fi
        for _ in $(seq 1 100); do kill -0 "$REC_PID" 2>/dev/null || break; sleep 0.1; done
        if kill -0 "$REC_PID" 2>/dev/null; then
            if [ "$sid" = "$REC_PID" ]; then kill -KILL -- "-$REC_PID" 2>/dev/null || true; else kill -KILL "$REC_PID" 2>/dev/null || true; fi
        fi
        wait "$REC_PID" 2>/dev/null || true
    else
        state=$?
        [ "$state" -eq 1 ] || demo_die "$PID_DIR/$name.pid does not identify an owned process; refusing to kill it"
    fi
    rm -f "$PID_DIR/$name.pid"
}

rollback_started() {
    local index
    set +e
    for ((index=${#STARTED_NOW[@]}-1; index>=0; index--)); do stop_owned "${STARTED_NOW[index]}"; done
}
prep_exit() {
    local status=$?
    trap - EXIT
    if [ "$status" -ne 0 ]; then rollback_started; fi
    exit "$status"
}
trap prep_exit EXIT

# shellcheck disable=SC2016 # $1/$2 belong to the inner shell.
tcp_open() { timeout 1 bash -c 'exec 9<>"/dev/tcp/$1/$2"' _ "$1" "$2" 2>/dev/null; }
wait_tcp_owned() {
    local name="$1" host="$2" port="$3" state
    for _ in $(seq 1 80); do
        if tcp_open "$host" "$port"; then return 0; fi
        if record_state "$name"; then
            :
        else
            state=$?
            [ "$state" -eq 1 ] && demo_die "$name exited before listening on $host:$port"
            demo_die "$name process identity changed during startup"
        fi
        sleep 0.25
    done
    demo_die "$name did not listen on $host:$port (see $LOG_DIR/$name.log)"
}

stop_all() {
    local unknown
    unknown="$(find "$PID_DIR" -mindepth 1 -maxdepth 1 -type f -name '*.pid' \
        ! -name zot.pid ! -name vgw.pid ! -name store.pid ! -name cache.pid -print -quit 2>/dev/null || true)"
    [ -z "$unknown" ] || demo_die "unknown service ownership record under $PID_DIR: $unknown"
    stop_owned vgw
    stop_owned cache
    stop_owned store
    stop_owned zot
    rm -f "$STORE_SOCK" "$CACHE_SOCK" "$CACHE_HEALTH_SOCK" "$ENV_FILE"
    ok "stopped Demo-owned persistent services"
}

demo_init_data_dir
command -v flock >/dev/null 2>&1 || demo_die "flock is required"
DEMO_LOCK_FD=200
exec 200>"$DEMO_DATA_DIR/.lock"
chmod 0600 "$DEMO_DATA_DIR/.lock"
flock -n "$DEMO_LOCK_FD" || demo_die "another Demo preparation or run is using $DEMO_DATA_DIR"
for directory in "$PID_DIR" "$LOG_DIR" "$RUN_DIR" "$DEMO_DATA_DIR/store" \
    "$DEMO_DATA_DIR/cache-rocks" "$DEMO_DATA_DIR/zot" "$DEMO_DATA_DIR/vgw" \
    "$DEMO_DATA_DIR/docker"; do
    install -d -m 0700 "$directory"
done

case "${1:-up}" in
    stop)
        stop_all
        STARTED_NOW=()
        exit 0
        ;;
    reset)
        stop_all
        case "$(readlink -m "$DEMO_DATA_DIR")" in /var/lib/kuasar-demo|*/.kuasar-*|*/kuasar-demo-*) ;; *) demo_die "reset requires an explicitly Demo-named canonical directory: $DEMO_DATA_DIR" ;; esac
        [ "$(readlink -m "$DEMO_DATA_DIR")" = "$DEMO_DATA_DIR" ] \
            || demo_die "reset requires canonical DEMO_DATA_DIR without symlinked parents: $DEMO_DATA_DIR"
        rm -rf -- "$DEMO_DATA_DIR"
        STARTED_NOW=()
        ok "removed owned Demo data $DEMO_DATA_DIR"
        exit 0
        ;;
    up) ;;
    *) demo_die "usage: demo_prep.sh [up|stop|reset]" ;;
esac

for binary in store-ctl cache-ctl; do
    [ -x "$BIN/$binary" ] || demo_die "missing $BIN/$binary; use the matching source build or release archive"
done
for tool in curl docker flock timeout setsid python3; do command -v "$tool" >/dev/null 2>&1 || demo_die "$tool is required"; done

REGISTRY_INSECURE="${REGISTRY_INSECURE:-}"
REGISTRY_AUTH_FILE=""
OWNED_ZOT=0
select_docker_config() {
    local registry_hash
    registry_hash="$(printf '%s' "$REGISTRY" | sha256sum | awk '{print substr($1,1,16)}')"
    DOCKER_CONFIG="$DEMO_DATA_DIR/docker/$registry_hash"
    install -d -m 0700 "$DOCKER_CONFIG"
    export DOCKER_CONFIG
}
if [ -n "${REGISTRY:-}" ]; then
    demo_require_single_line REGISTRY "$REGISTRY"
    select_docker_config
    say "using configured registry: $REGISTRY"
    case "$REGISTRY" in 127.0.0.1:*|localhost:*) REGISTRY_INSECURE=1 ;; esac
    if [ -n "$REGISTRY_USER_VALUE" ]; then
        [ -n "$REGISTRY_PASS_VALUE" ] || demo_die "REGISTRY_PASS is required with REGISTRY_USER"
        printf '%s' "$REGISTRY_PASS_VALUE" | docker login "$REGISTRY" -u "$REGISTRY_USER_VALUE" --password-stdin >/dev/null \
            || demo_die "docker login failed for $REGISTRY"
        REGISTRY_AUTH_FILE="$DOCKER_CONFIG/config.json"
        demo_secure_owned_file "$REGISTRY_AUTH_FILE"
    elif [ -f "$DOCKER_CONFIG/config.json" ]; then
        REGISTRY_AUTH_FILE="$DOCKER_CONFIG/config.json"
        demo_validate_handoff "$REGISTRY_AUTH_FILE"
        say "reusing the private Docker auth file for $REGISTRY"
    fi
else
    if [ -z "$ZOT_BIN" ] || [ ! -x "$ZOT_BIN" ]; then
        demo_die "set REGISTRY=<host:port> or provide an executable ZOT_BIN"
    fi
    ZOT_PORT="${ZOT_PORT:-5000}"
    [[ "$ZOT_PORT" =~ ^[0-9]+$ ]] || demo_die "ZOT_PORT must be numeric"
    ((10#$ZOT_PORT >= 1 && 10#$ZOT_PORT <= 65535)) || demo_die "ZOT_PORT must be in 1-65535"
    REGISTRY="127.0.0.1:$ZOT_PORT"
    OWNED_ZOT=1
    select_docker_config
    REGISTRY_INSECURE=1
    if record_state zot; then
        :
    else
        state=$?
        [ "$state" -eq 1 ] || demo_die "zot PID record conflicts with a foreign process"
        tcp_open 127.0.0.1 "$ZOT_PORT" && demo_die "127.0.0.1:$ZOT_PORT is already in use by a process not owned by this Demo"
    fi
    cat >"$DEMO_DATA_DIR/zot.json.new" <<EOF
{ "storage": { "rootDirectory": "$DEMO_DATA_DIR/zot", "dedupe": false, "gc": false },
  "http": { "address": "127.0.0.1", "port": "$ZOT_PORT", "compat": ["docker2s2"] },
  "log": { "level": "warn" } }
EOF
    if record_state zot; then
        if [ ! -f "$DEMO_DATA_DIR/zot.json" ] \
            || ! cmp -s "$DEMO_DATA_DIR/zot.json" "$DEMO_DATA_DIR/zot.json.new"; then
            rm -f "$DEMO_DATA_DIR/zot.json.new"
            demo_die "zot configuration is missing or changed while the owned service is running; stop it before reconfiguration"
        fi
    fi
    mv "$DEMO_DATA_DIR/zot.json.new" "$DEMO_DATA_DIR/zot.json"
    start_owned zot "$ZOT_BIN" serve "$DEMO_DATA_DIR/zot.json"
    wait_tcp_owned zot 127.0.0.1 "$ZOT_PORT"
    curl -fsS --max-time 5 --noproxy '*' "http://127.0.0.1:$ZOT_PORT/v2/" >/dev/null \
        || demo_die "owned zot did not pass its registry API health check"
    ok "owned local zot on $REGISTRY"
fi

STORE_CONFIG="$DEMO_DATA_DIR/store.yaml"
cat >"$STORE_CONFIG.new" <<EOF
listen: unix://$STORE_SOCK
backend: fs
fs: { root: $DEMO_DATA_DIR/store, verify_content_key: true }
EOF
if record_state store && { [ ! -f "$STORE_CONFIG" ] || ! cmp -s "$STORE_CONFIG" "$STORE_CONFIG.new"; }; then
    rm -f "$STORE_CONFIG.new"
    demo_die "store configuration is missing or changed while the owned service is running; stop it before reconfiguration"
fi
mv "$STORE_CONFIG.new" "$STORE_CONFIG"
if record_state store; then
    :
else
    state=$?
    [ "$state" -eq 1 ] || demo_die "store PID record conflicts with a foreign process"
    [ ! -e "$STORE_SOCK" ] || demo_die "$STORE_SOCK exists without a live owned store process"
    if [ -f "$DEMO_DATA_DIR/store/__meta/generations" ]; then
        "$BIN/store-ctl" info --config "$STORE_CONFIG" >"$LOG_DIR/store-info.log" 2>&1 \
            || demo_die "existing Store generation metadata is invalid"
    else
        "$BIN/store-ctl" init --config "$STORE_CONFIG" --generation G1 >"$LOG_DIR/store-init.log" 2>&1 \
            || demo_die "store initialization failed"
    fi
fi
start_owned store "$BIN/store-ctl" serve --config "$STORE_CONFIG"
for _ in $(seq 1 80); do [ -S "$STORE_SOCK" ] && break; record_state store || demo_die "store exited before binding $STORE_SOCK"; sleep 0.25; done
[ -S "$STORE_SOCK" ] || demo_die "store did not bind $STORE_SOCK"

CACHE_CONFIG="$DEMO_DATA_DIR/cache.yaml"
cat >"$CACHE_CONFIG.new" <<EOF
mode: tiered
listen: unix://$CACHE_SOCK
health_listen: unix://$CACHE_HEALTH_SOCK
rpc_timeout: 5s
freq: { counters: 1M, reset_after: 100K }
tiers:
  - type: embedded
    rocks: { path: $DEMO_DATA_DIR/cache-rocks, disk_bytes: 4GiB, mem_ratio: 0.1, direct_reads: true, bloom_bits: 10 }
origin:
  type: store
  store: { endpoint: unix://$STORE_SOCK, pool: 4, timeout: 5s }
  max_inflight: 32
EOF
if record_state cache && { [ ! -f "$CACHE_CONFIG" ] || ! cmp -s "$CACHE_CONFIG" "$CACHE_CONFIG.new"; }; then
    rm -f "$CACHE_CONFIG.new"
    demo_die "cache configuration is missing or changed while the owned service is running; stop it before reconfiguration"
fi
mv "$CACHE_CONFIG.new" "$CACHE_CONFIG"
if record_state cache; then
    :
else
    state=$?
    [ "$state" -eq 1 ] || demo_die "cache PID record conflicts with a foreign process"
    if [ -e "$CACHE_SOCK" ] || [ -e "$CACHE_HEALTH_SOCK" ]; then
        demo_die "cache socket exists without a live owned cache process"
    fi
fi
start_owned cache "$BIN/cache-ctl" serve --config "$CACHE_CONFIG"
for _ in $(seq 1 80); do [ -S "$CACHE_SOCK" ] && break; record_state cache || demo_die "cache exited before binding $CACHE_SOCK"; sleep 0.25; done
[ -S "$CACHE_SOCK" ] || demo_die "cache did not bind $CACHE_SOCK"
for _ in $(seq 1 40); do
    if "$BIN/cache-ctl" ping --endpoint "unix://$CACHE_HEALTH_SOCK" >"$LOG_DIR/cache-ping.log" 2>&1; then
        break
    fi
    record_state cache || demo_die "cache exited before passing its health check"
    sleep 0.25
done
"$BIN/cache-ctl" ping --endpoint "unix://$CACHE_HEALTH_SOCK" >"$LOG_DIR/cache-ping.log" 2>&1 \
    || demo_die "cache did not pass its health check"

if [ -z "$VGW_BIN" ]; then
    if record_state vgw; then
        VGW_BIN="$REC_EXE"
    else
        state=$?
        [ "$state" -eq 1 ] || demo_die "versitygw PID record is invalid; refusing to ignore it"
    fi
fi
if [ -n "$VGW_BIN" ]; then
    [[ "$VGW_PORT" =~ ^[0-9]+$ ]] || demo_die "VGW_PORT must be numeric"
    ((10#$VGW_PORT >= 1 && 10#$VGW_PORT <= 65535)) || demo_die "VGW_PORT must be in 1-65535"
    VGW_CONFIG="$DEMO_DATA_DIR/vgw.config"
    {
        printf 'port=%s\n' "$VGW_PORT"
        printf 'bucket=%s\n' "$VGW_BUCKET"
        printf 'access_key=%s\n' "$VGW_ACCESS_KEY_VALUE"
        printf 'secret_key=%s\n' "$VGW_SECRET_KEY_VALUE"
    } >"$VGW_CONFIG.new"
    chmod 0600 "$VGW_CONFIG.new"
    if record_state vgw; then
        if [ ! -f "$VGW_CONFIG" ] || ! cmp -s "$VGW_CONFIG" "$VGW_CONFIG.new"; then
            rm -f "$VGW_CONFIG.new"
            demo_die "versitygw configuration is missing or changed while the owned service is running; stop it before reconfiguration"
        fi
        :
    else
        state=$?
        [ "$state" -eq 1 ] || demo_die "versitygw PID record conflicts with a foreign process"
        tcp_open 127.0.0.1 "$VGW_PORT" && demo_die "127.0.0.1:$VGW_PORT is already in use by a process not owned by this Demo"
    fi
    mv "$VGW_CONFIG.new" "$VGW_CONFIG"
    if record_state vgw; then
        :
    else
        ROOT_ACCESS_KEY="$VGW_ACCESS_KEY_VALUE" ROOT_SECRET_KEY="$VGW_SECRET_KEY_VALUE" \
            start_owned vgw "$VGW_BIN" --port "127.0.0.1:$VGW_PORT" posix "$DEMO_DATA_DIR/vgw"
    fi
    wait_tcp_owned vgw 127.0.0.1 "$VGW_PORT"
    mkdir -p "$DEMO_DATA_DIR/vgw/$VGW_BUCKET"
    VGW_ENDPOINT="http://127.0.0.1:$VGW_PORT"
    VGW_HEALTH_CODE="$(curl -sS --max-time 5 --noproxy '*' -o /dev/null -w '%{http_code}' "$VGW_ENDPOINT/" 2>/dev/null || true)"
    [[ "$VGW_HEALTH_CODE" =~ ^[1-5][0-9][0-9]$ ]] \
        || demo_die "owned versitygw did not answer an HTTP health probe"
    ok "owned versitygw on $VGW_ENDPOINT"
else
    say "versitygw is absent; Quick Start may omit COPY, while the complete Demo will refuse to skip it"
fi

case "$E2E_IMAGE" in *@sha256:????????????????????????????????????????????????????????????????) ;; *)
    demo_die "E2E_IMAGE must be an immutable sha256 digest reference: $E2E_IMAGE"
esac
IMAGE_DIGEST="${E2E_IMAGE##*@sha256:}"
[[ "$IMAGE_DIGEST" =~ ^[0-9a-fA-F]{64}$ ]] || demo_die "invalid E2E_IMAGE digest"
BASE_TAG="sha-${IMAGE_DIGEST,,}"
BASE_TAG_REF="$REGISTRY/$REGISTRY_NS/base:$BASE_TAG"

if ! docker image inspect "$E2E_IMAGE" >/dev/null 2>&1; then
    if ! docker pull --platform linux/amd64 "$E2E_IMAGE" >"$LOG_DIR/source-pull.log" 2>&1; then
        echo "docker pull output for public source image $E2E_IMAGE:" >&2
        sed -n 'p' "$LOG_DIR/source-pull.log" >&2
        demo_die "docker pull failed for $E2E_IMAGE"
    fi
fi
SOURCE_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$E2E_IMAGE")"
BASE_REF=""

probe_owned_zot_manifest() {
    local body="$LOG_DIR/destination-manifest.json"
    local headers="$LOG_DIR/destination-manifest.headers"
    local status config_digest manifest_digest
    status="$(curl -sS --max-time 10 --noproxy '*' \
        -H 'Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
        -D "$headers" -o "$body" -w '%{http_code}' \
        "http://127.0.0.1:$ZOT_PORT/v2/$REGISTRY_NS/base/manifests/$BASE_TAG")" \
        || demo_die "could not query the owned Zot manifest for $BASE_TAG_REF"
    case "$status" in
        200)
            config_digest="$(demo_registry_manifest_config_digest "$body")" \
                || demo_die "owned Zot returned an invalid manifest for $BASE_TAG_REF"
            [ "$config_digest" = "$SOURCE_IMAGE_ID" ] \
                || demo_die "$BASE_TAG_REF already names different content; refusing to overwrite it"
            manifest_digest="$(awk '
                tolower($1) == "docker-content-digest:" {
                    value=$2; sub(/\r$/, "", value); print value; exit
                }
            ' "$headers")"
            [[ "$manifest_digest" =~ ^sha256:[0-9a-f]{64}$ ]] \
                || demo_die "owned Zot omitted the immutable digest for $BASE_TAG_REF"
            BASE_REF="${BASE_TAG_REF%:*}@$manifest_digest"
            return 0
            ;;
        404)
            demo_registry_error_is_absent "$body" \
                || demo_die "owned Zot returned an ambiguous 404 for the unclaimed tag $BASE_TAG_REF"
            return 1
            ;;
        *) demo_die "owned Zot manifest query for $BASE_TAG_REF returned HTTP $status" ;;
    esac
}

push_base_image() {
    if ! docker push "$BASE_TAG_REF" >"$LOG_DIR/push.log" 2>&1; then
        echo "docker push output for $BASE_TAG_REF:" >&2
        sed -n 'p' "$LOG_DIR/push.log" >&2
        demo_die "docker push failed for $BASE_TAG_REF"
    fi
}

if [ "$OWNED_ZOT" -eq 1 ]; then
    if probe_owned_zot_manifest; then
        say "base image already seeded and content-matched: $BASE_TAG_REF"
    else
        say "seeding immutable base image $E2E_IMAGE"
        docker tag "$E2E_IMAGE" "$BASE_TAG_REF"
        push_base_image
        probe_owned_zot_manifest \
            || demo_die "owned Zot still reports $BASE_TAG_REF absent after push"
    fi
elif docker pull --platform linux/amd64 "$BASE_TAG_REF" >"$LOG_DIR/destination-pull.log" 2>&1; then
    [ "$(docker image inspect --format '{{.Id}}' "$BASE_TAG_REF")" = "$SOURCE_IMAGE_ID" ] \
        || demo_die "$BASE_TAG_REF already names different content; refusing to overwrite it"
    say "base image already seeded and content-matched: $BASE_TAG_REF"
else
    if ! grep -Eqi 'manifest unknown|manifest.*not found|not found: manifest' "$LOG_DIR/destination-pull.log"; then
        demo_die "could not determine whether $BASE_TAG_REF is absent; refusing to overwrite it"
    fi
    say "seeding immutable base image $E2E_IMAGE"
    docker tag "$E2E_IMAGE" "$BASE_TAG_REF"
    push_base_image
    docker pull --platform linux/amd64 "$BASE_TAG_REF" >>"$LOG_DIR/destination-pull.log" 2>&1 \
        || demo_die "could not read back $BASE_TAG_REF after push"
    [ "$(docker image inspect --format '{{.Id}}' "$BASE_TAG_REF")" = "$SOURCE_IMAGE_ID" ] \
        || demo_die "$BASE_TAG_REF content does not match $E2E_IMAGE after push"
fi
if [ -z "$BASE_REF" ]; then
    while IFS= read -r repo_digest; do
        case "$repo_digest" in
            "${BASE_TAG_REF%:*}"@sha256:*) BASE_REF="$repo_digest"; break ;;
        esac
    done < <(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$BASE_TAG_REF")
fi
[ -n "$BASE_REF" ] || demo_die "could not resolve the immutable destination digest for $BASE_TAG_REF"
ok "base image fixed to $BASE_REF"

ENV_TMP="$DEMO_DATA_DIR/.prep.env.$$"
{
    printf 'REGISTRY=%q\n' "$REGISTRY"
    printf 'REGISTRY_NS=%q\n' "$REGISTRY_NS"
    printf 'REGISTRY_INSECURE=%q\n' "$REGISTRY_INSECURE"
    printf 'REGISTRY_AUTH_FILE=%q\n' "$REGISTRY_AUTH_FILE"
    printf 'STORE_SOCK=%q\n' "$STORE_SOCK"
    printf 'CACHE_SOCK=%q\n' "$CACHE_SOCK"
    printf 'CACHE_HEALTH_SOCK=%q\n' "$CACHE_HEALTH_SOCK"
    printf 'BASE_REF=%q\n' "$BASE_REF"
    printf 'E2E_IMAGE=%q\n' "$E2E_IMAGE"
    printf 'VGW_ENDPOINT=%q\n' "$VGW_ENDPOINT"
    printf 'VGW_BUCKET=%q\n' "$VGW_BUCKET"
    printf 'VGW_REGION=%q\n' us-east-1
    printf 'VGW_ACCESS_KEY=%q\n' "$VGW_ACCESS_KEY_VALUE"
    printf 'VGW_SECRET_KEY=%q\n' "$VGW_SECRET_KEY_VALUE"
} >"$ENV_TMP"
demo_secure_owned_file "$ENV_TMP"
mv -f "$ENV_TMP" "$ENV_FILE"
demo_secure_owned_file "$ENV_FILE"

STARTED_NOW=()
trap - EXIT
echo
ok "preparation ready; private handoff: $ENV_FILE (uid $DEMO_OWNER_UID, mode 0600)"
printf '    sudo -n env DEMO_DATA_DIR=%q BIN=%q PYTHON_BIN=/path/to/venv/bin/python3 bash %q\n' \
    "$DEMO_DATA_DIR" "$BIN" "$SCRIPT_DIR/demo_e2b.sh"
printf '    stop:  sudo -n env DEMO_DATA_DIR=%q bash %q stop\n' "$DEMO_DATA_DIR" "$SCRIPT_DIR/demo_prep.sh"
printf '    reset: sudo -n env DEMO_DATA_DIR=%q bash %q reset\n' "$DEMO_DATA_DIR" "$SCRIPT_DIR/demo_prep.sh"
