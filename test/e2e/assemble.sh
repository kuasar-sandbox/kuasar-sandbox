#!/usr/bin/env bash

set -euo pipefail

if [ "$#" -ne 7 ]; then
    echo "usage: assemble.sh OUTPUT PLATFORM ACCELERATOR CONNECTOR GUEST_RUNTIME SANDBOXER ORCHESTRATOR" >&2
    exit 2
fi

OUTPUT="$1"
PLATFORM="$2"
shift 2
COMPONENTS=(accelerator connector guest-runtime sandboxer orchestrator)
SOURCES=("$@")

[ ! -e "$OUTPUT" ] || {
    echo "platform content output already exists: $OUTPUT" >&2
    exit 1
}
if [ ! -d "$PLATFORM/docs" ] || [ ! -d "$PLATFORM/test" ]; then
    echo "platform source is missing docs/ or test/" >&2
    exit 1
fi

mkdir -p "$OUTPUT"
cp -a "$PLATFORM/test" "$OUTPUT/"
rm -rf "$OUTPUT/test/e2e"
mkdir -p "$OUTPUT/test/e2e"
install -m 0755 "$PLATFORM/test/e2e/run_all.sh" "$OUTPUT/test/e2e/run_all.sh"

for index in "${!COMPONENTS[@]}"; do
    component="${COMPONENTS[$index]}"
    source_root="${SOURCES[$index]}"
    source_suite="$source_root/test/e2e"
    [ -x "$source_suite/run_all.sh" ] || {
        echo "$component source is missing executable test/e2e/run_all.sh" >&2
        exit 1
    }
    if find "$source_suite" -type l -print -quit | grep -q .; then
        echo "$component e2e suite contains a symbolic link" >&2
        exit 1
    fi
    [ -f "$source_root/README.md" ] && [ -d "$source_root/docs" ] || {
        echo "$component source is missing README.md or docs/" >&2
        exit 1
    }
    cp -a "$source_suite" "$OUTPUT/test/e2e/$component"
done

platform_suite="$PLATFORM/test/e2e/platform"
[ -x "$platform_suite/run_all.sh" ] || {
    echo "platform source is missing executable test/e2e/platform/run_all.sh" >&2
    exit 1
}
if find "$platform_suite" -type l -print -quit | grep -q .; then
    echo "platform e2e suite contains a symbolic link" >&2
    exit 1
fi
cp -a "$platform_suite" "$OUTPUT/test/e2e/platform"

# Keep the existing flat design-document entry points, include both languages
# and out-of-docs guides, then rebase only documentation links for this layout.
python3 "$(dirname "${BASH_SOURCE[0]}")/assemble_docs.py" \
    "$OUTPUT" "$PLATFORM" "${SOURCES[@]}"

for component in "${COMPONENTS[@]}" platform; do
    [ -x "$OUTPUT/test/e2e/$component/run_all.sh" ] || {
        echo "assembled suite is missing executable $component/run_all.sh" >&2
        exit 1
    }
done
[ ! -e "$OUTPUT/test/e2e/assemble.sh" ] || {
    echo "source-only E2E assembler leaked into deliverable content" >&2
    exit 1
}
