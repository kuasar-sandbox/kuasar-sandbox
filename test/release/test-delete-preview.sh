#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python3 -B "$ROOT/test/release/delete_preview_test.py"
echo 'test-delete-preview: PASS'
