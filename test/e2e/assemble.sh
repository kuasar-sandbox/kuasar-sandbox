#!/usr/bin/env bash
# Source-time assembly only. Execution consumes this flat, prebuilt release.
set -euo pipefail
[[ "$#" == 7 ]] || {
    echo 'usage: assemble.sh OUTPUT PLATFORM ACCELERATOR CONNECTOR GUEST_RUNTIME SANDBOXER ORCHESTRATOR' >&2
    exit 2
}
OUTPUT=$1 PLATFORM=$2
shift 2
COMPONENTS=(accelerator connector guest-runtime sandboxer orchestrator)
SOURCES=("$@")
[[ ! -e "$OUTPUT" && -d "$PLATFORM/docs" && -d "$PLATFORM/test" ]] || {
    echo 'assembly needs a fresh output and platform docs/test inputs' >&2; exit 1;
}
mkdir -p "$OUTPUT"
cp -a "$PLATFORM/test" "$OUTPUT/"
rm -rf "$OUTPUT/test/e2e"
mkdir -p "$OUTPUT/test/e2e/cases"
install -m 0755 "$PLATFORM/test/e2e/e2e" "$OUTPUT/test/e2e/e2e"
cp -a "$PLATFORM/test/e2e/lib" "$OUTPUT/test/e2e/lib"

copy_cases() {
    local owner=$1 suite=$2
    python3 -B - "$owner" "$suite" "$OUTPUT/test/e2e" <<'PY'
import importlib.machinery, importlib.util, pathlib, shutil, sys
owner, source, target = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
loader = importlib.machinery.SourceFileLoader('assembled_runner', str(target / 'e2e'))
spec = importlib.util.spec_from_loader(loader.name, loader)
runner = importlib.util.module_from_spec(spec)
loader.exec_module(runner)
if not source.is_dir() or source.is_symlink() or any(path.is_symlink() for path in source.rglob('*')):
    raise SystemExit(f'{owner} E2E source is missing or contains symbolic links')
if (source / 'run_all.sh').exists():
    raise SystemExit(f'{owner} still has a superseded owner runner')
cases = runner.discover(source / 'cases')
if not cases:
    raise SystemExit(f'{owner} has no product cases')
for case in cases:
    destination = target / 'cases' / case.name
    if destination.exists():
        raise SystemExit(f'duplicate E2E case ID: {case.name} ({owner})')
    shutil.copy2(case, destination)
if (source / 'lib').is_dir():
    shutil.copytree(source / 'lib', target / 'lib' / owner)
PY
}

for index in "${!COMPONENTS[@]}"; do
    component=${COMPONENTS[$index]}
    source_root=${SOURCES[$index]}
    [[ -f "$source_root/README.md" && -d "$source_root/docs" ]] || {
        echo "$component source is missing README.md or docs/" >&2; exit 1;
    }
    source_suite="$source_root/test/e2e"
    if [[ -n "${E2E_SOURCE_ROOT:-}" ]]; then source_suite="$E2E_SOURCE_ROOT/$component/test/e2e"; fi
    copy_cases "$component" "$source_suite"
done
copy_cases platform "$PLATFORM/test/e2e/platform"
if [[ -n "${E2E_HELPER_ROOT:-}" ]]; then
    cp -a "$E2E_HELPER_ROOT" "$OUTPUT/test/e2e/helpers"
fi
python3 -B "$(dirname "${BASH_SOURCE[0]}")/assemble_docs.py" \
    "$OUTPUT" "$PLATFORM" "${SOURCES[@]}"
python3 -B "$OUTPUT/test/e2e/e2e" list --all
