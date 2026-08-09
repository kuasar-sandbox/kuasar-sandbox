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
cp -a "$PLATFORM/docs" "$PLATFORM/test" "$OUTPUT/"
rm -rf "$OUTPUT/test/e2e"
mkdir -p "$OUTPUT/test/e2e"
install -m 0755 "$PLATFORM/test/e2e/run_all.sh" "$OUTPUT/test/e2e/run_all.sh"

copy_component_docs() {
    local component="$1" source_root="$2" source_docs="$source_root/docs"
    if [ ! -f "$source_root/README.md" ] || [ ! -d "$source_docs" ]; then
        echo "$component source is missing README.md or docs/" >&2
        exit 1
    fi
    if find "$source_docs" -type l -print -quit | grep -q .; then
        echo "$component docs contain a symbolic link" >&2
        exit 1
    fi

    local source_file relative destination
    while IFS= read -r -d '' source_file; do
        relative="${source_file#"$source_docs/"}"
        destination="$OUTPUT/docs/$relative"
        [ ! -e "$destination" ] || {
            echo "component docs collide at docs/$relative" >&2
            exit 1
        }
        mkdir -p "$(dirname "$destination")"
        cp -a "$source_file" "$destination"
    done < <(find "$source_docs" -type f -print0 | LC_ALL=C sort -z)

    destination="$OUTPUT/docs/$component.md"
    [ ! -e "$destination" ] || {
        echo "component README collides at docs/$component.md" >&2
        exit 1
    }
    install -m 0644 "$source_root/README.md" "$destination"
}

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
    copy_component_docs "$component" "$source_root"
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
