#!/usr/bin/env bash

# Fast, host-independent ownership regressions. Real switch, systemd, KVM and
# failure-boundary coverage remains part of the Integration E2E Demo run.

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=demo_common.sh
. "$SCRIPT_DIR/demo_common.sh"

command -v grep >/dev/null 2>&1 || {
    echo "grep is required to inspect the Demo scripts" >&2
    exit 1
}

TEST_ROOT="$(mktemp -d /tmp/kuasar-demo-safety.XXXXXX)"
cleanup() {
    case "$TEST_ROOT" in /tmp/kuasar-demo-safety.*) rm -rf -- "$TEST_ROOT" ;;
esac
}
trap cleanup EXIT

DEMO_OWNER_UID="$(id -u)"
DEMO_OWNER_GID="$(id -g)"
export DEMO_OWNER_UID DEMO_OWNER_GID

DEMO_DATA_DIR="$TEST_ROOT/owned"
export DEMO_DATA_DIR
demo_init_data_dir
demo_init_data_dir
[ "$(stat -c %a "$DEMO_DATA_DIR")" = 700 ]
[ "$(stat -c %a "$DEMO_DATA_DIR/.kuasar-demo-owner")" = 600 ]

printf 'extra\n' >>"$DEMO_DATA_DIR/.kuasar-demo-owner"
if (demo_init_data_dir) >/dev/null 2>&1; then
    echo "ownership marker with trailing data was accepted" >&2
    exit 1
fi

DEMO_DATA_DIR="$TEST_ROOT/foreign"
export DEMO_DATA_DIR
mkdir "$DEMO_DATA_DIR"
printf 'sentinel\n' >"$DEMO_DATA_DIR/foreign-data"
if (demo_init_data_dir) >/dev/null 2>&1; then
    echo "nonempty unmarked directory was adopted" >&2
    exit 1
fi
grep -Fxq sentinel "$DEMO_DATA_DIR/foreign-data"

mkdir "$TEST_ROOT/real-parent"
ln -s "$TEST_ROOT/real-parent" "$TEST_ROOT/link-parent"
DEMO_DATA_DIR="$TEST_ROOT/link-parent/data"
export DEMO_DATA_DIR
if (demo_validate_data_path) >/dev/null 2>&1; then
    echo "symlinked parent path was accepted" >&2
    exit 1
fi

DEMO_DATA_DIR="$TEST_ROOT/handoff"
export DEMO_DATA_DIR
demo_init_data_dir
printf 'value\n' >"$DEMO_DATA_DIR/prep.env"
chmod 0644 "$DEMO_DATA_DIR/prep.env"
if (demo_validate_handoff "$DEMO_DATA_DIR/prep.env") >/dev/null 2>&1; then
    echo "world-readable handoff was accepted" >&2
    exit 1
fi
chmod 0600 "$DEMO_DATA_DIR/prep.env"
demo_validate_handoff "$DEMO_DATA_DIR/prep.env"

cat >"$TEST_ROOT/manifest.json" <<'EOF'
{"schemaVersion":2,"config":{"digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}
EOF
[ "$(demo_registry_manifest_config_digest "$TEST_ROOT/manifest.json")" = \
    sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ]
printf '{"schemaVersion":2,"config":{"digest":"sha256:not-a-digest"}}\n' \
    >"$TEST_ROOT/invalid-manifest.json"
if demo_registry_manifest_config_digest "$TEST_ROOT/invalid-manifest.json" >/dev/null 2>&1; then
    echo "invalid registry config digest was accepted" >&2
    exit 1
fi
printf '{"errors":[{"code":"MANIFEST_UNKNOWN"}]}\n' >"$TEST_ROOT/manifest-unknown.json"
demo_registry_error_is_absent "$TEST_ROOT/manifest-unknown.json"
printf '{"errors":[{"code":"DENIED"}]}\n' >"$TEST_ROOT/registry-denied.json"
if demo_registry_error_is_absent "$TEST_ROOT/registry-denied.json"; then
    echo "registry denial was accepted as an absent tag" >&2
    exit 1
fi

DEMO_DATA_DIR="$TEST_ROOT/path with spaces"
export DEMO_DATA_DIR
if (demo_validate_data_path) >/dev/null 2>&1; then
    echo "data path requiring unsafe config escaping was accepted" >&2
    exit 1
fi
if (demo_require_single_line TEST_VALUE $'first\nsecond') >/dev/null 2>&1; then
    echo "multiline credential value was accepted" >&2
    exit 1
fi
if (demo_require_yaml_single_quoted TEST_VALUE "unsafe'value") >/dev/null 2>&1; then
    echo "unsafe YAML credential value was accepted" >&2
    exit 1
fi

if grep -En "/tmp/demo-e2b-cli-env|systemctl stop 'sandbox-(runner|builder)@\\\*|0\\|4\\).*SWITCH_OWNED=1|printf 'export (REGISTRY|VGW_)" \
    "$SCRIPT_DIR/demo_e2b.sh" "$SCRIPT_DIR/demo_prep.sh"; then
    echo "unsafe Demo cleanup pattern remains" >&2
    exit 1
else
    grep_status=$?
    if [ "$grep_status" -ne 1 ]; then
        echo "cannot inspect Demo scripts for unsafe cleanup patterns (grep exit $grep_status)" >&2
        exit 1
    fi
fi
# shellcheck disable=SC2016 # The literal source pattern must not expand here.
if grep -Fq 'local name="$1" record="$PID_DIR/$name.pid"' \
    "$SCRIPT_DIR/demo_prep.sh"; then
    echo "record_state expands its local name before assignment under set -u" >&2
    exit 1
fi
[[ "$DEMO_DEFAULT_E2E_IMAGE" =~ ^library/python:3\.12-slim@sha256:[0-9a-f]{64}$ ]] || {
    echo "canonical Demo source image must be the immutable Python linux/amd64 manifest" >&2
    exit 1
}
if grep -Eq 'library/python:3\.12-slim@sha256:[0-9a-f]{64}' \
    "$SCRIPT_DIR/demo_prep.sh" "$SCRIPT_DIR/../e2e/platform/e2e_demo.sh"; then
    echo "Demo source image digest is duplicated outside demo_common.sh" >&2
    exit 1
fi

echo "PASS: Demo ownership and private-handoff safety"
