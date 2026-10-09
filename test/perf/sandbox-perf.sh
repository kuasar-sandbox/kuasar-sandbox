#!/usr/bin/env bash
# Source-only characterization of the current file/Manifest lifecycle cases.
# Uses the one public runner and already prepared products; never rebuilds them.
# Case wall time includes case setup/assertions/cleanup, NOT create-to-ready time.
# Raw stats, runner results and logs are retained for successful and failed runs.
set -euo pipefail
ITERS="${PERF_ITERS:-5}"
[[ "$ITERS" =~ ^[1-9][0-9]*$ ]] || { echo 'PERF_ITERS must be positive' >&2; exit 2; }
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
: "${E2E_WORKDIR:?set E2E_WORKDIR to an immutable workspace prepared for sandbox.lifecycle.sh and image.manifest-boot.sh}"
PREPARED="$(readlink -f "$E2E_WORKDIR")"
OUT="${PERF_OUT:-$REPO_ROOT/test/results/sandbox-perf.txt}"
SCENARIOS="${PERF_SCENARIOS:-cold cold-manifest}"
[[ -f "$PREPARED/test/e2e/e2e" && -f "$PREPARED/provenance.json" ]] || { echo 'missing prepared public runner/inputs' >&2; exit 2; }
[[ "$SCENARIOS" =~ [^[:space:]] ]] || { echo 'select at least one scenario' >&2; exit 2; }
seen=' '
for scenario in $SCENARIOS; do
    [[ "$seen" != *" $scenario "* ]] || { echo "duplicate scenario: $scenario" >&2; exit 2; }
    seen+="$scenario "
    case "$scenario" in cold|cold-manifest) ;; *) echo "unknown scenario: $scenario" >&2; exit 2 ;; esac
done
python3 - "$PREPARED" "$OUT" <<'PYPATH'
from pathlib import Path
import sys
root, output = (Path(p).resolve() for p in sys.argv[1:])
if output.is_relative_to(root) or root.is_relative_to(output):
    raise SystemExit('performance output must be outside prepared inputs')
PYPATH
mkdir -p "$(dirname "$OUT")"
RAW_ROOT=$(mktemp -d "${OUT}.samples.XXXXXX")
# Socket paths are based on run-root. Keep mutable case state independent of
# arbitrary report names; workbench supplies a short, retained /build/t.
RUN_ROOT=$(mktemp -d "${TMPDIR:-/var/tmp}/kp-XXXXXX")
printf '%s\n' "$RUN_ROOT" >"$RAW_ROOT/run-root"
echo "Raw samples and failures: $RAW_ROOT; mutable state: $RUN_ROOT"

run_one() {
    local case_id="$1" tag="$2" i="$3" stats_name="$4"
    local stats_dir="$RAW_ROOT/${case_id%.sh}-$i"
    mkdir "$stats_dir"
    local log="$stats_dir/run.log"
    local stats="$stats_dir/out/$case_id/$stats_name"
    if ! python3 -B "$PREPARED/test/e2e/e2e" run --workdir "$PREPARED" \
        --include "$case_id" --run-root "$RUN_ROOT/${case_id%%.*}-$i" --out-root "$stats_dir/out" \
        --result "$stats_dir/result.json" >"$log" 2>&1; then
        echo "[$tag] iter $i FAILED (retained $log)" >&2
        return 1
    fi
    [[ -s "$stats" ]] || { echo "missing case stats: $stats" >&2; return 1; }
    local wall_ms
    wall_ms=$(python3 - "$stats_dir/result.json" "$case_id" <<'PYWALL'
import json, sys
result = json.load(open(sys.argv[1]))
assert result['conclusion'] == 'success' and result['cases'] == [sys.argv[2]]
assert len(result['timings']) == 1 and result['timings'][0]['exit_code'] == 0
print(result['timings'][0]['wall_seconds'] * 1000)
PYWALL
    ) || return 1
    # One JSON line per iter so the aggregator can see all metrics.
    python3 - "$stats" "$tag" "$i" "$wall_ms" <<'PY'
import json, sys
path, tag, i, wall = sys.argv[1:]
with open(path) as f:
    r = json.load(f)
u = r.get("uffd") or {}
rt = r.get("runtime") or {}
backs = {b["name"]: b for b in r.get("backends", [])}
b0 = backs.get("blk0", {})
read = b0.get("read") or {}
out = {
    "tag": tag,
    "timing_scope": "public-case-envelope",
    "iter": int(i),
    "wall_ms": float(wall) if wall else 0,
    "internal_ms": (r.get("wallclock") or {}).get("duration_ms"),
    "uffd_faults": u.get("faults_absent"),
    "uffd_errors": u.get("errors"),
    "uffd_queue_p95_us": (u.get("fault_queue_wait_p95") or 0) / 1000.0,
    "uffd_queue_p99_us": (u.get("fault_queue_wait_p99") or 0) / 1000.0,
    "uffd_source_calls": u.get("source_read_calls"),
    "uffd_source_bytes": u.get("source_read_bytes"),
    "uffd_source_ms": (u.get("source_read_ns") or 0) / 1e6,
    "uffd_urgent_copy_calls": u.get("urgent_copy_calls"),
    "uffd_urgent_copy_ms": (u.get("urgent_copy_ns") or 0) / 1e6,
    "uffd_urgent_zero_calls": u.get("urgent_zero_calls"),
    "uffd_urgent_zero_ms": (u.get("urgent_zero_ns") or 0) / 1e6,
    "uffd_tail_submitted": u.get("tail_submitted"),
    "uffd_tail_dropped": u.get("tail_dropped_busy"),
    "uffd_tail_buffered": u.get("tail_buffered_data"),
    "uffd_tail_deferred": u.get("tail_deferred_data"),
    "uffd_tail_zero": u.get("tail_zero"),
    "uffd_tail_completed": u.get("tail_pages_completed"),
    "uffd_tail_conflicts": u.get("tail_conflicts"),
    "uffd_tail_partial": u.get("tail_partial"),
    "uffd_pages_zeroed": u.get("pages_zeroed"),
    "uffd_pages_copied": u.get("pages_copied"),
    "uffd_total_pages": u.get("total_pages"),
    "lazy_load_ratio": u.get("lazy_load_ratio"),
    "blk0_reqs": read.get("count"),
    "blk0_p50_us": (read.get("p50_ns") or 0) / 1000.0,
    "blk0_p99_us": (read.get("p99_ns") or 0) / 1000.0,
    "blk0_load_pct": 100.0 * (b0.get("loaded_blocks") or 0) / max(1, b0.get("total_blocks") or 1),
    "go_num_gc": rt.get("num_gc"),
    "go_gc_pause_ms": (rt.get("gc_pause_total_ns") or 0) / 1e6,
    "go_heap_alloc_kb": (rt.get("heap_alloc_bytes") or 0) / 1024,
    "go_total_alloc_mb": (rt.get("total_alloc_bytes") or 0) / 1024 / 1024,
    "go_mallocs": rt.get("mallocs"),
    "go_goroutines": rt.get("num_goroutine"),
}
print(json.dumps(out))
PY
}

aggregate() {
    local tag="$1"
    shift
    python3 - "$tag" "$@" <<'PY'
import json, statistics, sys
tag = sys.argv[1]
rows = [json.loads(l) for l in sys.argv[2:]]
if not rows:
    print(f"[{tag}] no data")
    sys.exit(1)

def med(key, default=0):
    vs = [r.get(key) for r in rows if r.get(key) is not None]
    return statistics.median(vs) if vs else default

def fmt(v, unit=""):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.1f}{unit}"
    return f"{v}{unit}"

print(f"\n[{tag}] N={len(rows)}")
print(f"  case envelope:    median={med('wall_ms'):.0f}ms  min={min(r['wall_ms'] for r in rows):.0f}ms  max={max(r['wall_ms'] for r in rows):.0f}ms")
internal = [r['internal_ms'] for r in rows if r.get('internal_ms') is not None]
if internal:
    print(f"  sandbox-ctl lifetime: median={statistics.median(internal):.0f}ms (excl. bash + exec)")
print(f"  uffd faults/errors:   faults={int(med('uffd_faults'))} errors={int(med('uffd_errors'))} queue_p95={med('uffd_queue_p95_us'):.1f}µs queue_p99={med('uffd_queue_p99_us'):.1f}µs")
print(f"  uffd source:          calls={int(med('uffd_source_calls'))} bytes={int(med('uffd_source_bytes'))} time={med('uffd_source_ms'):.1f}ms")
print(f"  uffd urgent:          copy={int(med('uffd_urgent_copy_calls'))}/{med('uffd_urgent_copy_ms'):.1f}ms zero={int(med('uffd_urgent_zero_calls'))}/{med('uffd_urgent_zero_ms'):.1f}ms")
print(f"  uffd tail:            submitted={int(med('uffd_tail_submitted'))} dropped={int(med('uffd_tail_dropped'))} buffered/deferred/zero={int(med('uffd_tail_buffered'))}/{int(med('uffd_tail_deferred'))}/{int(med('uffd_tail_zero'))} completed={int(med('uffd_tail_completed'))} conflicts={int(med('uffd_tail_conflicts'))} partial={int(med('uffd_tail_partial'))}")
zeroed = int(med('uffd_pages_zeroed'))
copied = int(med('uffd_pages_copied'))
total = int(med('uffd_total_pages')) or 1
ratio = med('lazy_load_ratio') * 100
print(f"  uffd lazy-load:       resident={zeroed+copied}/{total} pages = {ratio:.2f}%  (zeroed={zeroed} copied={copied})")
print(f"  blk0 read:            reqs={int(med('blk0_reqs'))} p50={med('blk0_p50_us'):.1f}µs p99={med('blk0_p99_us'):.1f}µs load_coverage={med('blk0_load_pct'):.2f}%")
print(f"  go runtime:           goroutines={int(med('go_goroutines'))} numGC={int(med('go_num_gc'))} pause_total={med('go_gc_pause_ms'):.2f}ms heap_alloc={med('go_heap_alloc_kb'):.0f}KiB total_alloc={med('go_total_alloc_mb'):.1f}MiB mallocs={int(med('go_mallocs'))}")
PY
}

run_scenario() {
    local case_id="$1"
    local tag="$2" stats_name="$3"
    local rows=()
    for i in $(seq 1 "$ITERS"); do
        echo "==> [$tag] iter $i/$ITERS"
        local row
        row=$(run_one "$case_id" "$tag" "$i" "$stats_name") || { echo "  iter $i failed; samples retained" >&2; return 1; }
        rows+=("$row")
        # Brief one-line per iter for live visibility.
        echo "$row" | python3 -c "
import json, sys
r = json.loads(sys.stdin.read())
print('    wall={:.0f}ms internal={}ms faults={} errors={} tail={}/{} drop={} lazy={:.1f}% blk0_p50={:.1f}us gc={} alloc={:.1f}MiB'.format(
    r['wall_ms'], r.get('internal_ms',0), r['uffd_faults'], r.get('uffd_errors',0),
    r.get('uffd_tail_completed',0), r.get('uffd_tail_submitted',0), r.get('uffd_tail_dropped',0),
    (r.get('lazy_load_ratio') or 0)*100, r['blk0_p50_us'], r['go_num_gc'], r['go_total_alloc_mb']))
"
    done
    aggregate "$tag" "${rows[@]}"
}

{
    echo "# sandbox lifecycle case characterization — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "# repo: $REPO_ROOT"
    echo "# prepared inputs: $PREPARED"
    echo "# provenance: $(sha256sum "$PREPARED/provenance.json" | cut -d' ' -f1)"
    echo "# timing: public case envelope; not isolated startup or historical T0-to-exit"
    echo "# raw samples: $RAW_ROOT"
    echo "# mutable state: $RUN_ROOT"
    echo "# iters: $ITERS"
    echo
    for s in $SCENARIOS; do
        case "$s" in
            cold)
                run_scenario sandbox.lifecycle.sh "file://" lifecycle-stats.json
                ;;
            cold-manifest)
                run_scenario image.manifest-boot.sh "manifest://" stats.json
                ;;
            *)
                echo "unknown scenario: $s" >&2; exit 2
                ;;
        esac
    done
} | tee "$OUT"

echo
echo "==> report saved to $OUT"
