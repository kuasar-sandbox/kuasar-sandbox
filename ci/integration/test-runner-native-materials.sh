#!/usr/bin/env bash
set -euo pipefail

# Isolated files only: never install packages or touch real runner roots.
source "$1"
test_root="$(mktemp -d)"
trap 'rm -rf "$test_root"' EXIT
TEMPLATE_ROOT="$test_root/template"
source_root="$test_root/source"
test_build_id="$(printf 'fixture build\n' | sha256sum | awk '{print $1}')"
catalog="$TEMPLATE_ROOT/usr/share/kuasar-ci/native-libuuid/$test_build_id"
library="$TEMPLATE_ROOT/usr/lib64/libuuid.a"
mkdir -p "$source_root/libuuid" "$source_root/Documentation/licenses" "$(dirname "$library")"
printf 'fixture util-linux copyright\n' > "$source_root/COPYING"
printf 'fixture libuuid copyright\n' > "$source_root/libuuid/COPYING"
printf 'fixture referenced license text\n' > "$source_root/Documentation/licenses/BSD-3-Clause"
printf 'fixture static library\n' > "$library"
printf '%s\n' "$test_build_id" > "$TEMPLATE_ROOT/usr/lib64/.kuasar-libuuid-build-id"
record_static_libuuid_materials "$source_root" "$library" "$catalog"
static_libuuid_materials_valid "$catalog" "$library" || die "valid native catalog was rejected"
copy_static_libuuid "$test_root/slot"
copy_static_libuuid "$test_root/slot"
cmp "$library" "$test_root/slot/usr/lib64/libuuid.a"
cmp "$catalog/SOURCES.tsv" "$test_root/slot/usr/share/kuasar-ci/native-libuuid/$test_build_id/SOURCES.tsv"
[ "$(stat -c %a "$catalog/licenses/libuuid/COPYING")" = 644 ]
[ "$(stat -c %a "$catalog/licenses/libuuid")" = 755 ]
for mutation in payload license unlisted symlink missing traversal identity; do
    candidate="$test_root/$mutation"
    cp -a "$catalog" "$candidate"
    cp "$library" "$test_root/$mutation.a"
    case "$mutation" in
        payload) printf 'different payload\n' >> "$test_root/$mutation.a" ;;
        license) printf 'different notice\n' >> "$candidate/licenses/libuuid/COPYING" ;;
        unlisted) printf 'unlisted\n' > "$candidate/licenses/UNLISTED" ;;
        symlink) ln -s "$source_root/COPYING" "$candidate/licenses/LINK" ;;
        missing) rm "$candidate/licenses/libuuid/COPYING" ;;
        traversal) printf '%064d  ../source/COPYING\n' 0 >> "$candidate/MATERIALS.sha256" ;;
        identity)
            sed -i '2s/libuuid.a/other.a/' "$candidate/SOURCES.tsv"
            (cd "$candidate" && find licenses SOURCES.tsv -type f -print | LC_ALL=C sort \
                | while IFS= read -r file; do sha256sum "$file"; done) > "$candidate/MATERIALS.sha256"
            ;;
    esac
    if static_libuuid_materials_valid "$candidate" "$test_root/$mutation.a" >/dev/null 2>&1; then
        die "native catalog accepted $mutation"
    fi
done
printf 'test-ci-tools: native libuuid source catalog and repeated slot copy PASS\n'
