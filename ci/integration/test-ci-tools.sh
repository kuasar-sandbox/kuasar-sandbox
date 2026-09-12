#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
SYSTEM_PATH="$PATH"
cleanup() {
    chmod -R u+w "$TMP" 2>/dev/null || true
    rm -rf "$TMP"
}
trap cleanup EXIT

fail() {
    echo "test-ci-tools: $*" >&2
    exit 1
}

workflow="$SCRIPT_DIR/../../.github/workflows/integration-tests.yml"
entry_workflow="$SCRIPT_DIR/../../.github/workflows/ci-entry.yml"
daily_workflow="$SCRIPT_DIR/../../.github/workflows/daily-preview-branch.yml"
legacy_repository="kuasar-sandbox/platform"
candidate_pattern='^kuasar-sandbox/(accelerator|connector|guest-runtime|kuasar-sandbox|orchestrator|sandboxer)$'
working_set_perf="$SCRIPT_DIR/../../test/perf/sandbox-perf-working-set.sh"
private_control_runner="    runs-on: \${{ github.event.repository.private && 'kuasar-control' || 'ubuntu-latest' }}"

# These positive fixtures must remain compatible with the runtime's strict
# disk/restore input contract. Deliberate rejection tests live in sandboxer
# and are not subject to a blanket repository-wide ban on size fields.
python3 - "$SCRIPT_DIR/../.." <<'PY'
import pathlib
import re
import subprocess
import sys
import tempfile

root = pathlib.Path(sys.argv[1])
warm = (root / 'test/e2e/platform/e2e_warmpool_dedup.sh').read_text()
perf = (root / 'test/perf/sandbox-perf-manifest.sh').read_text()

def fields(document):
    stack = []
    result = set()
    for line in document.splitlines():
        match = re.match(r'^( *)([A-Za-z_][A-Za-z0-9_]*):(?:\s|$)', line)
        if not match:
            continue
        indent, key = len(match[1]), match[2]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        result.add('.'.join(part for _, part in stack))
    return result

def heredoc(text, label):
    matches = re.findall(r'<<EOF\n(.*?)\nEOF', text, re.S)
    assert len(matches) == 1, f'{label}: expected exactly one YAML fixture'
    return fields(matches[0])

warm_match = re.search(r'cat > "\$WORK/sb-\$i.yaml" <<EOF\n.*?\nEOF', warm, re.S)
assert warm_match, 'warm-pool sandbox YAML fixture was not found'
fixtures = {'warm-pool': heredoc(warm_match[0], 'warm-pool')}
for name in ('write_sandbox_yaml', 'write_host_yaml'):
    match = re.search(r'^' + name + r'\(\) \{\n.*?^\}', perf, re.S | re.M)
    assert match, f'{name}: writer was not found'
    fixtures[name] = heredoc(match[0], name)

for name, paths in fixtures.items():
    assert 'boot.root.overlay.diff' in paths, f'{name}: active diff binding missing'
    retired = {f'{base}.{key}' for base in ('boot.root', 'boot.root.overlay')
               for key in ('size', 'diff_size')}
    assert not paths & retired, f'{name}: retired disk input {paths & retired}'
    if name == 'write_host_yaml':
        forbidden = {'boot.cmdline', 'boot.root.base', 'launch'}
        assert not paths & forbidden, f'{name}: cold-only restore input {paths & forbidden}'
    else:
        assert {'boot.cmdline', 'boot.root.base', 'launch'} <= paths, f'{name}: cold workload lost'

assert 'truncate -s 1G "$BLK1_BASE"' in warm, 'warm-pool real 1 GiB capacity lost'
assert perf.count('truncate -s 1G "$diff"') == 3, 'cold/long/restore real 1 GiB capacity lost'
assert '\nchunker:\n  mode: cdc\n' in perf, 'manifest workload does not explicitly select CDC'
assert '"${SANDBOX_FILES[@]}"' in warm, 'aggregate excludes uploaded Sandbox E artifacts'
assert '"$SNAP_SANDBOX_REF" = "manifest://$SANDBOX_MKEY"' in warm, 'S -> E accounting identity unchecked'
print('test-ci-tools: positive disk/restore fixtures PASS')

# Execute the actual parser bodies without starting any VM or Store process.
count_fn = re.search(r'^nonzero_chunk_count\(\) \{\n.*?^\}', warm, re.S | re.M)
assert count_fn, 'nonzero chunk parser missing'
for sample, expected in (
    ('chunk count:   7\nzero chunks:   2 (8.0 KiB)\n', '5'),
    ('chunk count:   0\nzero chunks:   0 (0 B)\n', '0'),
    ('chunk count:   7\n', None),
    ('chunk count:   1\nzero chunks:   2 (8.0 KiB)\n', None),
    ('chunk count:   7\nchunk count:   7\nzero chunks:   2 (8.0 KiB)\n', None),
):
    result = subprocess.run(['bash', '-c', count_fn[0] + '\nnonzero_chunk_count'],
                            input=sample, text=True, capture_output=True)
    assert (result.returncode == 0) == (expected is not None), (sample, result)
    if expected is not None:
        assert result.stdout.strip() == expected, result

upload = re.search(r'    local upload_line snapshot_line\n.*?    local mem_resident="\$\{BASH_REMATCH\[1\]\}"', perf, re.S)
assert upload, 'upload statistics parser missing'
parse = ('parse() {\n local snap_log="$1" tag=regression i=1\n' + upload[0] +
         '\n printf "%s %s %s %s %s\\n" "$sandbox_total" "$sandbox_dedup" "$snap_total" "$snap_dedup" "$mem_resident"\n}\nparse "$1"')
valid = ('snapshot upload done: memory_size=8192 resident=4096 pause_ms=1 dump_ms=2\n'
         '  upload OK; Sandbox E stored=2 dedup=3, Snapshot S stored=5 dedup=7\n')
with tempfile.TemporaryDirectory(prefix='manifest-stats-') as directory:
    sample_path = pathlib.Path(directory) / 'snapshot.log'
    for sample, expected in (
        (valid, '5 3 12 7 4096'),
        (valid.replace('Sandbox E', 'overlay').replace('Snapshot S', 'snapshot'), None),
        (valid.replace('dedup=7', 'dedup=bad'), None),
        (valid.replace(' resident=4096', ''), None),
        ('', None),
    ):
        sample_path.write_text(sample)
        result = subprocess.run(['bash', '-eu', '-c', parse, 'test-upload-parser', str(sample_path)],
                                text=True, capture_output=True)
        assert (result.returncode == 0) == (expected is not None), (sample, result)
        if expected is not None:
            assert result.stdout.strip() == expected, result
print('test-ci-tools: manifest upload and nonzero-entry accounting parsers PASS')
PY

[ "$(grep -Fxc "$private_control_runner" "$entry_workflow")" -eq 2 ] \
    || fail "Integration E2E admission and finalization do not select the private caller runner pool"
if grep -Fqx '    runs-on: ubuntu-latest' "$entry_workflow"; then
    fail "Integration E2E entry still unconditionally bills control jobs to hosted runners"
fi

provisioner="$SCRIPT_DIR/../runner/provision.sh"
bash "$SCRIPT_DIR/test-runner-native-materials.sh" "$provisioner"
for slot in 1 2 4 6; do
    actual=$(bash -c '. "$1"; printf "%s\t%s\t%s\t%s" "$(slot_role "$2")" "$(runner_name "$2")" "$(runner_group "$2")" "$(runner_labels "$2")"' \
        test-runner-role "$provisioner" "$slot")
    expected=$(printf 'e2e\tbms-tmp-kuasar-e2e-%s\tkuasar-e2e\tkuasar-e2e,kvm,cgroup-v2,bms-slot-%s' \
        "$slot" "$slot")
    [ "$actual" = "$expected" ] \
        || fail "slot $slot does not retain the E2E runner identity"
done
for slot in 3 5; do
    actual=$(bash -c '. "$1"; printf "%s\t%s\t%s\t%s" "$(slot_role "$2")" "$(runner_name "$2")" "$(runner_group "$2")" "$(runner_labels "$2")"' \
        test-runner-role "$provisioner" "$slot")
    expected=$(printf 'control\tbms-tmp-kuasar-control-%s\tkuasar-control\tkuasar-control,control-slot-%s' \
        "$slot" "$slot")
    [ "$actual" = "$expected" ] \
        || fail "slot $slot does not use the isolated control runner identity"
done
grep -Fq 'rebuild) shift; rebuild_slot "${1:-}" ;;' "$provisioner" \
    || fail "provisioner does not expose the explicit slot rebuild operation"

grep -Fq "[ \"\$TRUSTED_WORKFLOW_REPOSITORY\" = kuasar-sandbox/kuasar-sandbox ]" "$workflow" \
    || fail "trusted workflow repository does not use the project repository identity"
grep -Fq "$candidate_pattern" "$workflow" \
    || fail "candidate repository allowlist does not contain the project repository slug"
[[ kuasar-sandbox/kuasar-sandbox =~ $candidate_pattern ]] \
    || fail "project repository is rejected by the candidate allowlist"
if [[ $legacy_repository =~ $candidate_pattern ]]; then
    fail "legacy repository slug is still accepted by the candidate allowlist"
fi
grep -Fq 'repositories: kuasar-sandbox' "$workflow" \
    || fail "platform tooling token does not select the project repository slug"
grep -Fq 'repositories: accelerator,connector,guest-runtime,kuasar-sandbox,orchestrator,sandboxer' \
    "$workflow" \
    || fail "source token repository list does not contain the project repository slug"
grep -Fq 'repositories: accelerator,connector,guest-runtime,kuasar-sandbox,orchestrator,sandboxer' \
    "$daily_workflow" \
    || fail "release token repository list does not contain the project repository slug"
grep -Fq '"kuasar-sandbox": "platform"' "$working_set_perf" \
    || fail "working-set revision reports do not map the renamed repository identity to the platform report key"
if grep -Fq '"platform": "platform"' "$working_set_perf"; then
    fail "working-set revision reports still accept the legacy repository slug"
fi
if grep -Fq "[ \"\$TRUSTED_WORKFLOW_REPOSITORY\" = $legacy_repository ]" "$workflow"; then
    fail "legacy repository identity is still accepted as the trusted Integration E2E implementation"
fi

grep -Fq 'companion_candidates:' "$workflow" \
    || fail "Integration E2E execution workflow does not accept the resolved companion set"
# shellcheck disable=SC2016 # Match a literal GitHub Actions expression.
grep -Fq 'companion_candidates: ${{ needs.admission.outputs.companion_candidates }}' \
    "$entry_workflow" \
    || fail "Integration E2E entry does not pass the admitted companion set to execution"
[ "$(grep -Fc 'permission-pull-requests: read' "$entry_workflow")" -eq 2 ] \
    || fail "companion admission and finalization tokens cannot read companion pull requests"
[ "$(grep -Fc 'permission-pull-requests: read' "$workflow")" -eq 1 ] \
    || fail "Integration E2E source token cannot revalidate companion pull requests"
if grep -Fq 'permission-pull-requests: write' "$workflow" "$entry_workflow"; then
    fail "companion validation requests pull request write access"
fi
# shellcheck disable=SC2016 # Match the literal workflow-local variable.
grep -Fq 'git/ref/pull/$companion_pr/merge' "$entry_workflow" \
    || fail "companion admission does not require a live integration ref"
grep -Fq 'git/ref/heads/$companion_base_ref' "$entry_workflow" \
    || fail "companion admission does not bind the integration to its current target branch"
grep -Fq 'supported_base_ref()' "$entry_workflow" \
    || fail "Integration E2E admission does not allow the maintained branch contract"
grep -Fq 'base_ref: ${{ needs.admission.outputs.base_ref }}' "$entry_workflow" \
    || fail "Integration E2E entry does not pass the admitted target branch to execution"
grep -Fq 'CANDIDATE_BASE_REF' "$workflow" \
    || fail "Integration E2E execution does not retain the exact primary target branch"
grep -Fq 'release-units.tsv' "$workflow" \
    || fail "Integration E2E execution does not record independent Daily release-unit selection"
grep -Fq 'preview-selection.py source-ref' "$workflow" \
    || fail "Integration E2E execution does not derive maintenance component source branches"
grep -Fq 'branches-where-head' "$entry_workflow" \
    || fail "companion admission does not require an organization repository branch head"
grep -Fq 'branches-where-head' "$workflow" \
    || fail "runner validation does not recheck the companion branch provenance"
runner_companion_validation=$(sed -n \
    '/^[[:space:]]*validate_companion() {$/,/^[[:space:]]*validate_primary \\/p' \
    "$workflow")
[ -n "$runner_companion_validation" ] \
    || fail "cannot extract the runner companion validation function"
[ "$(grep -Fc -- '--arg repository "$repository"' <<< "$runner_companion_validation")" -eq 1 ] \
    || fail "runner companion validation does not bind the repository identity"
[ "$(grep -Fc -- '--argjson number "$number"' <<< "$runner_companion_validation")" -eq 1 ] \
    || fail "runner companion validation does not bind the pull request number"
finalizer_companion_validation=$(sed -n \
    '/^[[:space:]]*validate_companion() {$/,/^[[:space:]]*post_status() {$/p' \
    "$entry_workflow")
[ -n "$finalizer_companion_validation" ] \
    || fail "cannot extract the finalizer companion validation function"
[ "$(grep -Fc -- '--arg repository "$repository"' <<< "$finalizer_companion_validation")" -eq 1 ] \
    || fail "finalizer companion validation does not bind the repository identity"
[ "$(grep -Fc -- '--argjson number "$number"' <<< "$finalizer_companion_validation")" -eq 1 ] \
    || fail "finalizer companion validation does not bind the pull request number"
grep -Fq 'source-set.tsv' "$workflow" \
    || fail "exact source-set metadata is not recorded"
grep -Fq 'platform_role=companion' "$workflow" \
    || fail "platform companion revision is not distinguished in the revision manifest"
grep -Fq 'role=companion' "$workflow" \
    || fail "component companion revisions are not distinguished in the revision manifest"
grep -Fq 'Pull request or companion source set changed while Integration E2E was running' "$entry_workflow" \
    || fail "finalization does not fail closed when a companion changes"
grep -Fq 'companion_count=' "$entry_workflow" \
    || fail "companion admission does not enforce its limit before resolving refs"
grep -Fq "grep -Fq '<!-- kuasar-ci-companions'" "$entry_workflow" \
    || fail "a malformed companion marker prefix can be treated as an absent marker"
if grep -Fq '.base.ref == "main"' "$workflow" "$entry_workflow"; then
    fail "Integration E2E still hard-codes main as the only PR target branch"
fi

grep -Fq 'id: platform-writer-token' "$daily_workflow" \
    || fail "Daily manifest writes do not use a dedicated App token"
grep -Fq 'repositories: kuasar-sandbox' "$daily_workflow" \
    || fail "Daily manifest writer token is not limited to the platform repository"
grep -Fq 'permission-contents: write' "$daily_workflow" \
    || fail "Daily manifest writer token cannot update the maintained branch"
grep -Fq 'PLATFORM_TOKEN: ${{ steps.platform-writer-token.outputs.token }}' "$daily_workflow" \
    || fail "Daily coordinator does not use the dedicated platform writer token"
if grep -Fq 'PLATFORM_TOKEN: ${{ github.token }}' "$daily_workflow"; then
    fail "Daily coordinator still writes with the generic GitHub Actions token"
fi

companion_parser="$TMP/companion-parser.awk"
sed -n '/# BEGIN kuasar-ci-companion-parser/,/# END kuasar-ci-companion-parser/p' \
    "$entry_workflow" | sed '1d;$d' >"$companion_parser"
[ -s "$companion_parser" ] || fail "cannot extract the production companion marker parser"

valid_companions="$TMP/valid-companions.md"
cat >"$valid_companions" <<'EOF'
ordinary pull request text
<!-- kuasar-ci-companions
kuasar-sandbox/orchestrator#227
kuasar-sandbox/guest-runtime#34
-->
more prose
EOF
mapfile -t parsed_companions < <(awk -f "$companion_parser" "$valid_companions")
if [ "${#parsed_companions[@]}" -ne 2 ] \
    || [ "${parsed_companions[0]}" != kuasar-sandbox/orchestrator#227 ] \
    || [ "${parsed_companions[1]}" != kuasar-sandbox/guest-runtime#34 ]; then
    fail "valid companion marker did not produce the exact ordered references"
fi

printf 'ordinary pull request text\n' >"$TMP/no-companions.md"
set +e
awk -f "$companion_parser" "$TMP/no-companions.md" >/dev/null
parser_status=$?
set -e
[ "$parser_status" -eq 3 ] || fail "missing companion marker did not return the absence status"

printf '<!-- kuasar-bms-companions\nkuasar-sandbox/orchestrator#227\n-->\n' \
    >"$TMP/retired-companions.md"
set +e
awk -f "$companion_parser" "$TMP/retired-companions.md" >/dev/null
parser_status=$?
set -e
[ "$parser_status" -eq 2 ] || fail "retired companion declaration was silently ignored"

python3 - "$SCRIPT_DIR/../.." "$workflow" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
execution = pathlib.Path(sys.argv[2]).read_text()
# The canonical checks are required after the staged caller/rule migration.
# Retired entry points must not remain executable or synthesize old successes.
for retired in ("platform-bms.yml", "bms-entry.yml", "bms-e2e.yml"):
    assert not (root / ".github/workflows" / retired).exists()
assert not (root / "ci/bms").exists()
caller = (root / ".github/workflows/ci.yml").read_text()
assert "  ci:\n    uses: kuasar-sandbox/kuasar-sandbox/.github/workflows/ci-entry.yml@main" in caller
assert "previous-required-check" not in caller
assert "bms / finalize" not in caller
aggregate = (root / ".github/workflows/aggregate-release.yml").read_text()
assert "uses: ./.github/workflows/integration-tests.yml" in aggregate
assert "needs: [prepare, release-asset-validation]" in aggregate
assert "bms-e2e.yml" not in aggregate
assert (root / ".github/workflows/ci-entry.yml").is_file()
revoke = execution.index("- name: Revoke platform tooling token before candidate execution")
assert revoke < execution.index("- name: Finalize source workspace")
assert revoke < execution.index("- name: Test exact published assets")
assert "skip-token-revoke: true" in execution[
    execution.index("- name: Create read-only platform tooling token"):revoke]
print("test-ci-tools: canonical-only callers and read-token revocation PASS")
PY

cat >"$TMP/duplicate-companions.md" <<'EOF'
<!-- kuasar-ci-companions
kuasar-sandbox/orchestrator#227
-->
<!-- kuasar-ci-companions
kuasar-sandbox/guest-runtime#34
-->
EOF
set +e
awk -f "$companion_parser" "$TMP/duplicate-companions.md" >/dev/null
parser_status=$?
set -e
[ "$parser_status" -eq 2 ] || fail "duplicate companion markers were accepted"

cat >"$TMP/unclosed-companions.md" <<'EOF'
<!-- kuasar-ci-companions
kuasar-sandbox/orchestrator#227
EOF
set +e
awk -f "$companion_parser" "$TMP/unclosed-companions.md" >/dev/null
parser_status=$?
set -e
[ "$parser_status" -eq 2 ] || fail "unclosed companion marker was accepted"

cat >"$TMP/malformed-prefix-companions.md" <<'EOF'
prose <!-- kuasar-ci-companions
kuasar-sandbox/orchestrator#227
-->
EOF
set +e
awk -f "$companion_parser" "$TMP/malformed-prefix-companions.md" >/dev/null
parser_status=$?
set -e
[ "$parser_status" -eq 3 ] \
    || fail "malformed marker prefix must take the production absence branch"

setup_workspace() {
    local root=$1
    mkdir -p "$root/guest-runtime/native-deps/deps"
    printf 'envd target fixture\n' >"$root/guest-runtime/native-deps/Makefile"
    printf 'common fixture\n' >"$root/guest-runtime/native-deps/deps/common.sh"
    printf 'build envd fixture\n' >"$root/guest-runtime/native-deps/deps/build-envd.sh"
}

setup_erofs_workspace() {
    local root=$1
    mkdir -p "$root/guest-runtime/native-deps/deps"
    printf 'erofs target fixture\n' >"$root/guest-runtime/native-deps/Makefile"
    printf 'common fixture\n' >"$root/guest-runtime/native-deps/deps/common.sh"
    printf 'build erofs fixture\n' >"$root/guest-runtime/native-deps/deps/build-erofs.sh"
}

setup_vmlinux_workspace() {
    local root=$1
    mkdir -p "$root/guest-runtime/native-deps/deps/vmlinux" \
        "$root/guest-runtime/native-deps/deps/linux-patches"
    cat >"$root/guest-runtime/native-deps/Makefile" <<'EOF'
TARGET_ARCH ?= x86_64
VMLINUX_BIN := $(abspath bin/$(TARGET_ARCH)/vmlinux)
VMLINUX_INPUTS := deps/build-vmlinux.sh deps/common.sh \
    deps/vmlinux/test.config \
    deps/linux-patches \
    $(wildcard deps/linux-patches/*.patch)

.PHONY: vmlinux
vmlinux: $(VMLINUX_BIN)
$(VMLINUX_BIN): $(VMLINUX_INPUTS)
	@mkdir -p "$(dir $@)"
	@mkdir -p build/src/linux/LICENSES/preferred
	@printf 'build\n' >>"$(FAKE_BUILD_COUNTER)"
	@printf 'fake-vmlinux\n' >"$@"
	@chmod +x "$@"
	@printf 'linux copying fixture\n' >build/src/linux/COPYING
	@printf 'linux credits fixture\n' >build/src/linux/CREDITS
	@printf 'linux license fixture\n' >build/src/linux/LICENSES/preferred/GPL-2.0
EOF
    printf 'common fixture\n' >"$root/guest-runtime/native-deps/deps/common.sh"
    printf 'build vmlinux fixture\n' >"$root/guest-runtime/native-deps/deps/build-vmlinux.sh"
    printf 'config fixture\n' >"$root/guest-runtime/native-deps/deps/vmlinux/test.config"
    printf 'patch fixture\n' >"$root/guest-runtime/native-deps/deps/linux-patches/test.patch"
}

setup_cloud_hypervisor_workspace() {
    local root=$1
    mkdir -p "$root/sandboxer/native-deps/deps/ch-patches"
    printf 'cloud-hypervisor target fixture\n' >"$root/sandboxer/native-deps/Makefile"
    printf 'common fixture\n' >"$root/sandboxer/native-deps/deps/common.sh"
    printf 'build cloud-hypervisor fixture\n' \
        >"$root/sandboxer/native-deps/deps/build-cloud-hypervisor.sh"
    printf 'patch fixture\n' >"$root/sandboxer/native-deps/deps/ch-patches/test.patch"
}

setup_rocksdb_workspace() {
    local root=$1
    mkdir -p "$root/accelerator/deps"
    printf 'rocksdb target fixture\n' >"$root/accelerator/Makefile"
    printf 'common fixture\n' >"$root/accelerator/deps/common.sh"
    printf 'build rocksdb fixture\n' >"$root/accelerator/deps/build-rocksdb.sh"
}

mkdir -p "$TMP/bin"
cat >"$TMP/bin/make" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
workdir=""
arch=x86_64
target=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -C) workdir=$2; shift 2 ;;
        TARGET_ARCH=*) arch=${1#*=}; shift ;;
        CROSS_PREFIX=*) shift ;;
        *) target=$1; shift ;;
    esac
done
[ -n "$workdir" ]
exec 9>>"$FAKE_BUILD_COUNTER"
flock 9
printf 'build\n' >&9
flock -u 9
sleep "${FAKE_BUILD_SLEEP:-0}"
case "$target" in
    envd)
        mkdir -p "$workdir/bin/$arch" "$workdir/build/src/e2b-infra"
        printf 'fake-envd\n' >"$workdir/bin/$arch/envd"
        chmod +x "$workdir/bin/$arch/envd"
        printf 'envd license fixture\n' >"$workdir/build/src/e2b-infra/LICENSE"
        ;;
    erofs)
        mkdir -p "$workdir/bin/$arch" "$workdir/build/$arch/src/erofs-utils"
        printf 'fake-mkfs.erofs\n' >"$workdir/bin/$arch/mkfs.erofs"
        printf 'fake-fsck.erofs\n' >"$workdir/bin/$arch/fsck.erofs"
        chmod +x "$workdir/bin/$arch/mkfs.erofs" "$workdir/bin/$arch/fsck.erofs"
        printf 'erofs authors fixture\n' >"$workdir/build/$arch/src/erofs-utils/AUTHORS"
        printf 'erofs copying fixture\n' >"$workdir/build/$arch/src/erofs-utils/COPYING"
        ;;
    deps-rocksdb)
        mkdir -p "$workdir/build/$arch/rocksdb/include/rocksdb" \
            "$workdir/build/$arch/rocksdb/lib" "$workdir/build/src/rocksdb"
        printf 'rocksdb header fixture\n' >"$workdir/build/$arch/rocksdb/include/rocksdb/db.h"
        printf 'rocksdb library fixture\n' >"$workdir/build/$arch/rocksdb/lib/librocksdb.a"
        for file in AUTHORS COPYING LICENSE.Apache LICENSE.leveldb; do
            printf 'rocksdb %s fixture\n' "$file" >"$workdir/build/src/rocksdb/$file"
        done
        ;;
    cloud-hypervisor)
        mkdir -p "$workdir/bin/$arch" \
            "$workdir/build/src/cloud-hypervisor/LICENSES"
        printf 'fake-cloud-hypervisor\n' >"$workdir/bin/$arch/cloud-hypervisor"
        chmod +x "$workdir/bin/$arch/cloud-hypervisor"
        printf 'cloud credits fixture\n' \
            >"$workdir/build/src/cloud-hypervisor/CREDITS.md"
        printf 'cloud license fixture\n' \
            >"$workdir/build/src/cloud-hypervisor/LICENSES/Apache-2.0.txt"
        printf 'cloud lock fixture\n' \
            >"$workdir/build/src/cloud-hypervisor/Cargo.lock"
        ;;
    *) exit 2 ;;
esac
EOF
chmod +x "$TMP/bin/make"

cat >"$TMP/bin/go" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
    version) printf 'go version go1.test linux/amd64\n' ;;
    env)
        shift
        for name in "$@"; do
            case "$name" in
                GOFLAGS) printf '%s\n' "${GOFLAGS:-}" ;;
                GOAMD64) printf '%s\n' "${GOAMD64:-v1}" ;;
                GOARM64) printf '%s\n' "${GOARM64:-v8.0}" ;;
                *) printf '%s=test\n' "$name" ;;
            esac
        done
        ;;
    *) exit 2 ;;
esac
EOF
chmod +x "$TMP/bin/go"

timings="$TMP/timings.tsv"
KUASAR_CI_TIMINGS="$timings" "$SCRIPT_DIR/ci-timed.sh" fixture/timing bash -c 'sleep 0.01'
[ "$(wc -l <"$timings")" -eq 2 ] || fail "timing file must contain one header and one row"
awk -F '\t' 'NR == 1 && NF == 10 && $1 == "stage" { ok=1 } END { exit !ok }' "$timings" \
    || fail "timing header is malformed"
awk -F '\t' 'NR == 2 && NF == 10 && $1 == "fixture/timing" && $10 == 0 { ok=1 } END { exit !ok }' "$timings" \
    || fail "timing row is malformed"

parallel_timings="$TMP/timings-parallel.tsv"
timing_pids=()
for i in $(seq 1 8); do
    KUASAR_CI_TIMINGS="$parallel_timings" "$SCRIPT_DIR/ci-timed.sh" \
        "fixture/parallel-$i" bash -c 'sleep 0.02' &
    timing_pids+=("$!")
done
for pid in "${timing_pids[@]}"; do
    wait "$pid"
done
awk -F '\t' '
    NR == 1 {
        if (NF != 10 || $1 != "stage") {
            exit 1
        }
        next
    }
    NF != 10 || $1 !~ /^fixture\/parallel-[1-8]$/ || $10 != 0 {
        exit 1
    }
    !seen[$1]++ {
        stages++
    }
    END {
        if (NR != 9 || stages != 8) {
            exit 1
        }
    }
' "$parallel_timings" || fail "parallel timing rows are malformed or incomplete"

workspace="$TMP/workspace"
cache="$TMP/cache"
counter="$TMP/build-counter"
metrics="$TMP/cache-metrics.tsv"
setup_workspace "$workspace"

plain_key="$(env PATH="$TMP/bin:$PATH" KUASAR_WORKSPACE_ROOT="$workspace" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" key envd | cut -f2)"
tagged_key="$(env PATH="$TMP/bin:$PATH" GOFLAGS=-tags=ci KUASAR_WORKSPACE_ROOT="$workspace" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" key envd | cut -f2)"
[ "$plain_key" != "$tagged_key" ] || fail "effective GOFLAGS did not invalidate the envd key"
microarch_key="$(env PATH="$TMP/bin:$PATH" GOAMD64=v3 KUASAR_WORKSPACE_ROOT="$workspace" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" key envd | cut -f2)"
[ "$plain_key" != "$microarch_key" ] || fail "GOAMD64 did not invalidate the envd key"

cross_workspace="$TMP/cross-workspace"
setup_erofs_workspace "$cross_workspace"
for tool in gcc g++ ar ld; do
    cat >"$TMP/bin/custom-$tool" <<EOF
#!/usr/bin/env bash
printf 'custom-$tool v1\\n'
EOF
    chmod +x "$TMP/bin/custom-$tool"
done
cross_key_v1="$(env PATH="$TMP/bin:$PATH" CROSS_PREFIX="$TMP/bin/custom-" \
    KUASAR_WORKSPACE_ROOT="$cross_workspace" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" key erofs | cut -f2)"
printf '# toolchain update\n' >>"$TMP/bin/custom-gcc"
cross_key_v2="$(env PATH="$TMP/bin:$PATH" CROSS_PREFIX="$TMP/bin/custom-" \
    KUASAR_WORKSPACE_ROOT="$cross_workspace" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" key erofs | cut -f2)"
[ "$cross_key_v1" != "$cross_key_v2" ] || fail "custom cross compiler did not invalidate the key"

pkg_workspace="$TMP/pkg-workspace"
pkg_fixture="$TMP/pkg-fixture"
pkg_bin="$TMP/pkg-bin"
setup_erofs_workspace "$pkg_workspace"
mkdir -p "$pkg_fixture/lib" "$pkg_bin"
printf 'Name: uuid fixture\nVersion: 1\n' >"$pkg_fixture/uuid.pc"
printf 'static uuid v1\n' >"$pkg_fixture/lib/libuuid.a"
cat >"$pkg_bin/pkg-config" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
: "${PKG_CONFIG_FIXTURE:?}"
case "${1:-}" in
    --version) printf 'pkg-config fixture 1\n' ;;
    --exists) exit 0 ;;
    --path) printf '%s/uuid.pc\n' "$PKG_CONFIG_FIXTURE" ;;
    --modversion) printf '1\n' ;;
    --cflags) printf '%s\n' "-I$PKG_CONFIG_FIXTURE/include" ;;
    --libs) printf '%s\n' "-L$PKG_CONFIG_FIXTURE/lib -luuid" ;;
    --variable=libdir) printf '%s/lib\n' "$PKG_CONFIG_FIXTURE" ;;
    *) exit 2 ;;
esac
EOF
chmod +x "$pkg_bin/pkg-config"
pkg_key_v1="$(env PATH="$TMP/bin:$PATH" PKG_CONFIG="$pkg_bin/pkg-config" \
    PKG_CONFIG_FIXTURE="$pkg_fixture" KUASAR_WORKSPACE_ROOT="$pkg_workspace" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" key erofs | cut -f2)"
printf 'static uuid v2\n' >"$pkg_fixture/lib/libuuid.a"
pkg_key_v2="$(env PATH="$TMP/bin:$PATH" PKG_CONFIG="$pkg_bin/pkg-config" \
    PKG_CONFIG_FIXTURE="$pkg_fixture" KUASAR_WORKSPACE_ROOT="$pkg_workspace" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" key erofs | cut -f2)"
[ "$pkg_key_v1" != "$pkg_key_v2" ] \
    || fail "pkg-config selected library did not invalidate the erofs key"

vmlinux_workspace="$TMP/vmlinux-workspace"
setup_vmlinux_workspace "$vmlinux_workspace"
vmlinux_key_plain="$(env PATH="$TMP/bin:$PATH" KUASAR_WORKSPACE_ROOT="$vmlinux_workspace" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" key vmlinux | cut -f2)"
vmlinux_key_versioned="$(env PATH="$TMP/bin:$PATH" LOCALVERSION=-ci KBUILD_BUILD_USER=builder \
    KUASAR_WORKSPACE_ROOT="$vmlinux_workspace" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" key vmlinux | cut -f2)"
[ "$vmlinux_key_plain" != "$vmlinux_key_versioned" ] \
    || fail "Kbuild overrides did not invalidate the vmlinux key"

vmlinux_cache="$TMP/vmlinux-cache"
vmlinux_counter="$TMP/vmlinux-build-counter"
vmlinux_metrics="$TMP/vmlinux-cache-metrics.tsv"
env PATH="$SYSTEM_PATH" FAKE_BUILD_COUNTER="$vmlinux_counter" \
    KUASAR_WORKSPACE_ROOT="$vmlinux_workspace" KUASAR_NATIVE_CACHE_ROOT="$vmlinux_cache" \
    KUASAR_NATIVE_CACHE_METRICS="$vmlinux_metrics" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build vmlinux
[ "$(wc -l <"$vmlinux_counter")" -eq 1 ] || fail "cold vmlinux cache must build once"
[ "$(cat "$vmlinux_workspace/guest-runtime/native-deps/build/src/linux/COPYING")" \
    = 'linux copying fixture' ] || fail "cold vmlinux cache omitted source license material"
env PATH="$SYSTEM_PATH" FAKE_BUILD_COUNTER="$vmlinux_counter" \
    make -C "$vmlinux_workspace/guest-runtime/native-deps" \
    TARGET_ARCH=x86_64 vmlinux >/dev/null
[ "$(wc -l <"$vmlinux_counter")" -eq 1 ] \
    || fail "cold vmlinux cache restore left the Make target stale"

rm -f "$vmlinux_workspace/guest-runtime/native-deps/bin/x86_64/vmlinux" \
    "$vmlinux_workspace/guest-runtime/native-deps/build/src/linux/COPYING"
env PATH="$SYSTEM_PATH" FAKE_BUILD_COUNTER="$vmlinux_counter" \
    KUASAR_WORKSPACE_ROOT="$vmlinux_workspace" KUASAR_NATIVE_CACHE_ROOT="$vmlinux_cache" \
    KUASAR_NATIVE_CACHE_METRICS="$vmlinux_metrics" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build vmlinux
env PATH="$SYSTEM_PATH" FAKE_BUILD_COUNTER="$vmlinux_counter" \
    make -C "$vmlinux_workspace/guest-runtime/native-deps" \
    TARGET_ARCH=x86_64 vmlinux >/dev/null
[ "$(wc -l <"$vmlinux_counter")" -eq 1 ] \
    || fail "hot vmlinux cache restore left the Make target stale"
[ "$(cat "$vmlinux_workspace/guest-runtime/native-deps/build/src/linux/COPYING")" \
    = 'linux copying fixture' ] || fail "hot vmlinux cache did not restore source license material"
grep -q $'vmlinux\thit\t' "$vmlinux_metrics" || fail "hot vmlinux cache metric is missing"

cloud_workspace="$TMP/cloud-workspace"
cargo_home="$TMP/cargo-home"
setup_cloud_hypervisor_workspace "$cloud_workspace"
mkdir -p "$cargo_home"
printf '[build]\nrustflags = ["-Cdebuginfo=0"]\n' >"$cargo_home/config.toml"
cloud_key_plain="$(env PATH="$TMP/bin:$PATH" CARGO_HOME="$cargo_home" \
    KUASAR_WORKSPACE_ROOT="$cloud_workspace" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" key cloud-hypervisor | cut -f2)"
cloud_key_encoded="$(env PATH="$TMP/bin:$PATH" CARGO_HOME="$cargo_home" \
    CARGO_ENCODED_RUSTFLAGS=-Ctarget-cpu=x86-64-v3 \
    KUASAR_WORKSPACE_ROOT="$cloud_workspace" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" key cloud-hypervisor | cut -f2)"
[ "$cloud_key_plain" != "$cloud_key_encoded" ] \
    || fail "CARGO_ENCODED_RUSTFLAGS did not invalidate the Cloud Hypervisor key"
cloud_key_target_flags="$(env PATH="$TMP/bin:$PATH" CARGO_HOME="$cargo_home" \
    CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS=-Ctarget-cpu=x86-64-v3 \
    KUASAR_WORKSPACE_ROOT="$cloud_workspace" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" key cloud-hypervisor | cut -f2)"
[ "$cloud_key_plain" != "$cloud_key_target_flags" ] \
    || fail "target-specific Cargo rustflags did not invalidate the Cloud Hypervisor key"
printf '[build]\nrustflags = ["-Cdebuginfo=1"]\n' >"$cargo_home/config.toml"
cloud_key_config="$(env PATH="$TMP/bin:$PATH" CARGO_HOME="$cargo_home" \
    KUASAR_WORKSPACE_ROOT="$cloud_workspace" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" key cloud-hypervisor | cut -f2)"
[ "$cloud_key_plain" != "$cloud_key_config" ] \
    || fail "Cargo config did not invalidate the Cloud Hypervisor key"

rocks_workspace="$TMP/rocks-workspace"
setup_rocksdb_workspace "$rocks_workspace"
for tool in clang clang++; do
    cat >"$TMP/bin/$tool" <<EOF
#!/usr/bin/env bash
printf '$tool v1\\n'
EOF
    chmod +x "$TMP/bin/$tool"
done
rocks_key_v1="$(env PATH="$TMP/bin:$PATH" CC=clang CXX=clang++ \
    KUASAR_WORKSPACE_ROOT="$rocks_workspace" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" key rocksdb | cut -f2)"
printf '# compiler update\n' >>"$TMP/bin/clang"
rocks_key_v2="$(env PATH="$TMP/bin:$PATH" CC=clang CXX=clang++ \
    KUASAR_WORKSPACE_ROOT="$rocks_workspace" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" key rocksdb | cut -f2)"
[ "$rocks_key_v1" != "$rocks_key_v2" ] \
    || fail "selected RocksDB compiler did not invalidate the key"

material_cache="$TMP/material-cache"
material_counter="$TMP/material-build-counter"
env PATH="$TMP/bin:$PATH" FAKE_BUILD_COUNTER="$material_counter" \
    KUASAR_WORKSPACE_ROOT="$cross_workspace" KUASAR_NATIVE_CACHE_ROOT="$material_cache" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build erofs
env PATH="$TMP/bin:$PATH" FAKE_BUILD_COUNTER="$material_counter" \
    KUASAR_WORKSPACE_ROOT="$rocks_workspace" KUASAR_NATIVE_CACHE_ROOT="$material_cache" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build rocksdb
env PATH="$TMP/bin:$PATH" FAKE_BUILD_COUNTER="$material_counter" CARGO_HOME="$cargo_home" \
    KUASAR_WORKSPACE_ROOT="$cloud_workspace" KUASAR_NATIVE_CACHE_ROOT="$material_cache" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build cloud-hypervisor
[ "$(wc -l <"$material_counter")" -eq 3 ] \
    || fail "cold native material fixtures did not build exactly once per component"
[ "$(cat "$cross_workspace/guest-runtime/native-deps/build/x86_64/src/erofs-utils/COPYING")" \
    = 'erofs copying fixture' ] || fail "erofs cache omitted source license material"
[ "$(cat "$rocks_workspace/accelerator/build/src/rocksdb/LICENSE.Apache")" \
    = 'rocksdb LICENSE.Apache fixture' ] || fail "RocksDB cache omitted source license material"
[ "$(cat "$cloud_workspace/sandboxer/native-deps/build/src/cloud-hypervisor/CREDITS.md")" \
    = 'cloud credits fixture' ] || fail "Cloud Hypervisor cache omitted source credit material"

rm -f \
    "$cross_workspace/guest-runtime/native-deps/bin/x86_64/mkfs.erofs" \
    "$cross_workspace/guest-runtime/native-deps/build/x86_64/src/erofs-utils/COPYING" \
    "$rocks_workspace/accelerator/build/x86_64/rocksdb/lib/librocksdb.a" \
    "$rocks_workspace/accelerator/build/src/rocksdb/LICENSE.Apache" \
    "$cloud_workspace/sandboxer/native-deps/bin/x86_64/cloud-hypervisor" \
    "$cloud_workspace/sandboxer/native-deps/build/src/cloud-hypervisor/CREDITS.md"
env PATH="$TMP/bin:$PATH" FAKE_BUILD_COUNTER="$material_counter" \
    KUASAR_WORKSPACE_ROOT="$cross_workspace" KUASAR_NATIVE_CACHE_ROOT="$material_cache" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build erofs
env PATH="$TMP/bin:$PATH" FAKE_BUILD_COUNTER="$material_counter" \
    KUASAR_WORKSPACE_ROOT="$rocks_workspace" KUASAR_NATIVE_CACHE_ROOT="$material_cache" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build rocksdb
env PATH="$TMP/bin:$PATH" FAKE_BUILD_COUNTER="$material_counter" CARGO_HOME="$cargo_home" \
    KUASAR_WORKSPACE_ROOT="$cloud_workspace" KUASAR_NATIVE_CACHE_ROOT="$material_cache" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build cloud-hypervisor
[ "$(wc -l <"$material_counter")" -eq 3 ] \
    || fail "hot native material fixtures rebuilt a component"
[ -s "$cross_workspace/guest-runtime/native-deps/build/x86_64/src/erofs-utils/COPYING" ] \
    || fail "hot erofs cache did not restore source license material"
[ -s "$rocks_workspace/accelerator/build/src/rocksdb/LICENSE.Apache" ] \
    || fail "hot RocksDB cache did not restore source license material"
[ -s "$cloud_workspace/sandboxer/native-deps/build/src/cloud-hypervisor/CREDITS.md" ] \
    || fail "hot Cloud Hypervisor cache did not restore source credit material"

env PATH="$TMP/bin:$PATH" FAKE_BUILD_COUNTER="$counter" \
    KUASAR_WORKSPACE_ROOT="$workspace" KUASAR_NATIVE_CACHE_ROOT="$cache" \
    KUASAR_NATIVE_CACHE_METRICS="$metrics" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build envd
[ "$(wc -l <"$counter")" -eq 1 ] || fail "cold cache must build once"
[ "$(cat "$workspace/guest-runtime/native-deps/build/src/e2b-infra/LICENSE")" \
    = 'envd license fixture' ] || fail "cold envd cache omitted source license material"

rm -f "$workspace/guest-runtime/native-deps/bin/x86_64/envd" \
    "$workspace/guest-runtime/native-deps/build/src/e2b-infra/LICENSE"
env PATH="$TMP/bin:$PATH" FAKE_BUILD_COUNTER="$counter" \
    KUASAR_WORKSPACE_ROOT="$workspace" KUASAR_NATIVE_CACHE_ROOT="$cache" \
    KUASAR_NATIVE_CACHE_METRICS="$metrics" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build envd
[ "$(wc -l <"$counter")" -eq 1 ] || fail "hot cache rebuilt the component"
[ "$(cat "$workspace/guest-runtime/native-deps/build/src/e2b-infra/LICENSE")" \
    = 'envd license fixture' ] || fail "hot envd cache did not restore source license material"
grep -q $'envd\thit\t' "$metrics" || fail "hot cache metric is missing"

# A different input key is exercised in the same fixture directory. Real
# source-mode assembly starts from a clean component tree as required by the
# cache; remove the source tree created by the preceding mocked build to model
# that boundary exactly.
rm -rf "$workspace/guest-runtime/native-deps/build/src/e2b-infra"
printf 'changed input\n' >>"$workspace/guest-runtime/native-deps/deps/build-envd.sh"
env PATH="$TMP/bin:$PATH" FAKE_BUILD_COUNTER="$counter" \
    KUASAR_WORKSPACE_ROOT="$workspace" KUASAR_NATIVE_CACHE_ROOT="$cache" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build envd
[ "$(wc -l <"$counter")" -eq 2 ] || fail "input change did not invalidate the cache"

current_key="$(env PATH="$TMP/bin:$PATH" KUASAR_WORKSPACE_ROOT="$workspace" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" key envd | cut -f2)"
entry="$cache/v2/x86_64/envd/$current_key"
chmod u+w "$entry/payload.tar"
printf 'tampered\n' >>"$entry/payload.tar"
if env PATH="$TMP/bin:$PATH" FAKE_BUILD_COUNTER="$counter" \
    KUASAR_WORKSPACE_ROOT="$workspace" KUASAR_NATIVE_CACHE_ROOT="$cache" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build envd >/dev/null 2>&1; then
    fail "tampered payload was accepted"
fi

concurrent_cache="$TMP/concurrent-cache"
concurrent_counter="$TMP/concurrent-counter"
setup_workspace "$TMP/workspace-a"
setup_workspace "$TMP/workspace-b"
env PATH="$TMP/bin:$PATH" FAKE_BUILD_COUNTER="$concurrent_counter" FAKE_BUILD_SLEEP=0.5 \
    KUASAR_WORKSPACE_ROOT="$TMP/workspace-a" KUASAR_NATIVE_CACHE_ROOT="$concurrent_cache" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build envd >"$TMP/a.log" 2>&1 &
pid_a=$!
env PATH="$TMP/bin:$PATH" FAKE_BUILD_COUNTER="$concurrent_counter" FAKE_BUILD_SLEEP=0.5 \
    KUASAR_WORKSPACE_ROOT="$TMP/workspace-b" KUASAR_NATIVE_CACHE_ROOT="$concurrent_cache" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build envd >"$TMP/b.log" 2>&1 &
pid_b=$!
wait "$pid_a"
wait "$pid_b"
[ "$(wc -l <"$concurrent_counter")" -eq 1 ] || fail "same-key concurrent misses built more than once"
grep -q 'miss-built' "$TMP/a.log" "$TMP/b.log" || fail "concurrent miss was not recorded"
grep -q 'hit-after-wait' "$TMP/a.log" "$TMP/b.log" || fail "concurrent waiter did not restore the published entry"

staging_cache="$TMP/staging-cache"
staging_workspace="$TMP/staging-workspace"
staging_counter="$TMP/staging-counter"
setup_workspace "$staging_workspace"
orphan_stage="$staging_cache/v2/.tmp/envd.orphan.test"
orphan_lock="$staging_cache/v2/.locks/staging/${orphan_stage##*/}.lock"
mkdir -p "$orphan_stage/payload" "$(dirname "$orphan_lock")"
exec 8>"$orphan_lock"
flock 8
env PATH="$TMP/bin:$PATH" FAKE_BUILD_COUNTER="$staging_counter" \
    KUASAR_WORKSPACE_ROOT="$staging_workspace" KUASAR_NATIVE_CACHE_ROOT="$staging_cache" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build envd >/dev/null
[ -d "$orphan_stage" ] || fail "active native staging directory was pruned"
flock -u 8
exec 8>&-
env PATH="$TMP/bin:$PATH" FAKE_BUILD_COUNTER="$staging_counter" \
    KUASAR_WORKSPACE_ROOT="$staging_workspace" KUASAR_NATIVE_CACHE_ROOT="$staging_cache" \
    "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build envd >/dev/null
[ ! -e "$orphan_stage" ] || fail "orphaned native staging directory was not pruned"
[ ! -e "$orphan_lock" ] || fail "orphaned native staging lock was not pruned"

retained_cache="$TMP/retained-cache"
retained_workspace="$TMP/retained-workspace"
retained_counter="$TMP/retained-counter"
setup_workspace "$retained_workspace"
for n in 1 2 3 4; do
    rm -rf "$retained_workspace/guest-runtime/native-deps/build/src/e2b-infra"
    printf 'input-%s\n' "$n" >"$retained_workspace/guest-runtime/native-deps/deps/build-envd.sh"
    env PATH="$TMP/bin:$PATH" FAKE_BUILD_COUNTER="$retained_counter" \
        KUASAR_WORKSPACE_ROOT="$retained_workspace" KUASAR_NATIVE_CACHE_ROOT="$retained_cache" \
        KUASAR_NATIVE_CACHE_MAX_ENTRIES=2 KUASAR_NATIVE_CACHE_MIN_AGE_SECONDS=0 \
        "$SCRIPT_DIR/../native-cache/native-cache.sh" restore-or-build envd >/dev/null
done
[ "$(find "$retained_cache/v2/x86_64/envd" -mindepth 1 -maxdepth 1 -type d | wc -l)" -eq 2 ] \
    || fail "native cache retention limit was not enforced"

source_cache="$TMP/source-cache"
mkdir -p "$source_cache"
for n in 1 2 3 4; do
    printf 'archive-%s\n' "$n" >"$source_cache/$n.tar.gz"
    printf 'checksum-%s\n' "$n" >"$source_cache/$n.tar.gz.sha256"
    touch -d "2026-01-0$n 00:00:00 UTC" "$source_cache/$n.tar.gz" "$source_cache/$n.tar.gz.sha256"
done
touch "$source_cache/stale.lock"
printf 'orphan\n' >"$source_cache/orphan.tar.gz.sha256"
KUASAR_SOURCE_CACHE_MAX_ENTRIES=2 \
    "$SCRIPT_DIR/source-cache-prune.sh" "$source_cache" "$source_cache/1.tar.gz"
[ -f "$source_cache/1.tar.gz" ] || fail "source cache pruned the protected archive"
[ -f "$source_cache/4.tar.gz" ] || fail "source cache pruned the newest archive"
[ "$(find "$source_cache" -maxdepth 1 -type f -name '*.tar.gz' | wc -l)" -eq 2 ] \
    || fail "source cache retention limit was not enforced"
[ ! -e "$source_cache/stale.lock" ] || fail "legacy source cache lock was not pruned"
[ ! -e "$source_cache/orphan.tar.gz.sha256" ] || fail "orphan source checksum was not pruned"

source_archives="$TMP/source-archives"
source_destination="$TMP/source-workspace/component"
mkdir -p "$source_archives/v1/repository/subdir" "$source_archives/v2/repository"
printf 'first\n' >"$source_archives/v1/repository/current.txt"
printf 'stale\n' >"$source_archives/v1/repository/subdir/stale.txt"
printf 'second\n' >"$source_archives/v2/repository/current.txt"
tar -czf "$source_archives/v1.tar.gz" -C "$source_archives/v1" repository
tar -czf "$source_archives/v2.tar.gz" -C "$source_archives/v2" repository

"$SCRIPT_DIR/materialize-source.sh" "$source_archives/v1.tar.gz" "$source_destination"
[ "$(cat "$source_destination/current.txt")" = first ] \
    || fail "initial source archive was not materialized"
[ -f "$source_destination/subdir/stale.txt" ] \
    || fail "initial source archive fixture is incomplete"

"$SCRIPT_DIR/materialize-source.sh" "$source_archives/v2.tar.gz" "$source_destination"
[ "$(cat "$source_destination/current.txt")" = second ] \
    || fail "replacement source archive was not materialized"
[ ! -e "$source_destination/subdir/stale.txt" ] \
    || fail "replacement source archive retained a stale file"

mkdir -p "$source_archives/invalid/repository"
ln -s current.txt "$source_archives/invalid/repository/unsupported-link"
tar -czf "$source_archives/invalid.tar.gz" -C "$source_archives/invalid" repository
if "$SCRIPT_DIR/materialize-source.sh" \
    "$source_archives/invalid.tar.gz" "$source_destination" >/dev/null 2>&1; then
    fail "source materializer accepted an unsupported archive entry"
fi
[ "$(cat "$source_destination/current.txt")" = second ] \
    || fail "failed source materialization modified the existing destination"

mkdir -p "$source_archives/conflict-file/repository" \
    "$source_archives/conflict-child/repository/conflict"
printf 'file\n' >"$source_archives/conflict-file/repository/conflict"
printf 'child\n' >"$source_archives/conflict-child/repository/conflict/child"
tar -cf "$source_archives/extract-failure.tar" \
    -C "$source_archives/conflict-file" repository/conflict
tar -rf "$source_archives/extract-failure.tar" \
    -C "$source_archives/conflict-child" repository/conflict/child
gzip -c "$source_archives/extract-failure.tar" >"$source_archives/extract-failure.tar.gz"
if "$SCRIPT_DIR/materialize-source.sh" \
    "$source_archives/extract-failure.tar.gz" "$source_destination" >/dev/null 2>&1; then
    fail "source materializer unexpectedly completed a conflicting extraction"
fi
[ "$(cat "$source_destination/current.txt")" = second ] \
    || fail "failed staged extraction modified the existing destination"
if find "$TMP/source-workspace" -maxdepth 1 \
    \( -name '.component.stage.*' -o -name '.component.previous.*' \) -print -quit \
    | grep -q .; then
    fail "source materializer left a staging directory behind"
fi

echo "test-ci-tools: PASS"
