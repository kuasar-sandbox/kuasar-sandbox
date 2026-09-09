#!/usr/bin/env bash

set -euo pipefail

usage() {
    echo "usage: $0 ARCHIVE DESTINATION" >&2
    exit 2
}

[ "$#" -eq 2 ] || usage

archive=$1
destination=$2

[ -s "$archive" ] || { echo "source archive is missing or empty: $archive" >&2; exit 1; }
case "$destination" in
    ""|/|.|..) echo "unsafe source destination: $destination" >&2; exit 1 ;;
esac

destination_parent=$(dirname -- "$destination")
destination_name=$(basename -- "$destination")
case "$destination_name" in
    ""|.|..) echo "unsafe source destination: $destination" >&2; exit 1 ;;
esac

install -d -m 0755 "$destination_parent"
destination_parent=$(cd "$destination_parent" && pwd -P)
destination="$destination_parent/$destination_name"
[ ! -L "$destination" ] \
    || { echo "source destination must not be a symbolic link: $destination" >&2; exit 1; }
[ ! -e "$destination" ] || [ -d "$destination" ] \
    || { echo "source destination must be a directory: $destination" >&2; exit 1; }

tar -tzf "$archive" >/dev/null

mapfile -t archive_roots < <(
    tar -tzf "$archive" | awk -F/ 'NF { print $1 }' | sort -u
)
if [ "${#archive_roots[@]}" -ne 1 ] || [ -z "${archive_roots[0]}" ]; then
    echo "source archive must contain one top-level directory" >&2
    exit 1
fi

if tar -tzf "$archive" | awk '
    {
        if ($0 ~ /^\//) {
            unsafe = 1
        }
        count = split($0, component, "/")
        for (i = 1; i <= count; i++) {
            if (component[i] == "." || component[i] == "..") {
                unsafe = 1
            }
        }
    }
    END { exit unsafe ? 0 : 1 }
'; then
    echo "source archive contains an unsafe path" >&2
    exit 1
fi

entry_type=$(tar -tvzf "$archive" | awk '
    substr($1, 1, 1) !~ /^[-d]$/ && bad == "" { bad = substr($1, 1, 1) }
    END { print bad }
')
[ -z "$entry_type" ] \
    || { echo "source archive contains unsupported entry type $entry_type" >&2; exit 1; }

stage=$(mktemp -d "$destination_parent/.${destination_name}.stage.XXXXXX")
chmod 0755 "$stage"
previous=""
promoted=0
cleanup() {
    local status=$?
    if [ -n "${stage:-}" ]; then
        rm -rf -- "$stage"
    fi
    if [ -n "${previous:-}" ] && [ -e "$previous" ]; then
        if [ "$promoted" -eq 1 ]; then
            rm -rf -- "$previous"
        elif [ ! -e "$destination" ]; then
            if ! mv -T -- "$previous" "$destination"; then
                echo "failed to restore prior source directory; retained at $previous" >&2
            fi
        else
            echo "prior source directory retained after failed promotion: $previous" >&2
        fi
    fi
    return "$status"
}
trap cleanup EXIT

tar -xzf "$archive" --strip-components=1 \
    --no-same-owner --no-same-permissions -C "$stage"

if [ -e "$destination" ]; then
    previous=$(mktemp -d "$destination_parent/.${destination_name}.previous.XXXXXX")
    rmdir "$previous"
    mv -T -- "$destination" "$previous"
fi

if ! mv -T -- "$stage" "$destination"; then
    echo "failed to promote staged source directory: $destination" >&2
    exit 1
fi
promoted=1
stage=""

if [ -n "$previous" ]; then
    rm -rf -- "$previous"
    previous=""
fi
