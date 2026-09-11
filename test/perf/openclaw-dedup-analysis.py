#!/usr/bin/env python3
"""
openclaw-dedup-analysis.py — Content-dedup analysis for Kuasar Sandbox snapshots

Measures how much content is shared across a fleet of sandboxes running
identical workloads:
  * 4 KiB page-granularity chunk dedup (fleet-wide unique-chunk ratio)
  * 64 KiB super-chunk dedup (aggregated from 4 KiB hashes — no re-read)
  * all-zero page ratio (the zero-page optimization baseline)
  * per-sandbox similarity vs. a reference sandbox (pairwise Jaccard)
  * allocated vs. apparent file sizes (sparsity)

Snapshot layout notes (empirical):
  * <sha>.snapshot  — packed snapshot artifact (real data)
  * <sid>.snapshot  — sparse TAR wrapper of the same data (skip: duplicates it)
  * <sha>.overlay   — full copy of the memory+rootfs backing (1.5 GiB class)
  * <sha>.sandbox   — sandbox E graph (~112 KiB)

Usage:
  python3 openclaw-dedup-analysis.py <snapshots_dir> [--workers N]
"""

import argparse
import hashlib
import os
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

CHUNK = 4096
SUPER = 64 * 1024
GROUP = SUPER // CHUNK
ZERO_CHUNK = b"\0" * CHUNK
ZERO_HASH = hashlib.blake2b(ZERO_CHUNK, digest_size=16).digest()


def hash_file(path):
    """Stream a file; return (total_chunks, zero_chunks, [4k hashes|None])."""
    total = zero = 0
    hashes = []
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb", buffering=CHUNK * 256) as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            total += 1
            if b == ZERO_CHUNK:
                zero += 1
                hashes.append(None)
            else:
                h.update(b)
                hashes.append(h.digest())
                h = hashlib.blake2b(digest_size=16)
    return total, zero, hashes


def super_hashes(hashes4k):
    """Aggregate GROUP consecutive 4K hashes into 64K identities. All-zero
    groups hash to a canonical zero marker so sparse regions compare equal
    but are reported separately."""
    out = []
    zero_groups = 0
    for i in range(0, len(hashes4k) - GROUP + 1, GROUP):
        grp = hashes4k[i:i + GROUP]
        if all(x is None for x in grp):
            zero_groups += 1
            continue
        h = hashlib.blake2b(digest_size=16)
        for x in grp:
            h.update(x if x else ZERO_HASH)
        out.append(h.digest())
    return out, zero_groups


def collect(snapshots_dir):
    targets = {"snapshot": [], "overlay": []}
    for sid in sorted(os.listdir(snapshots_dir)):
        d = os.path.join(snapshots_dir, sid)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            p = os.path.join(d, f)
            st = os.stat(p)
            # skip sparse tar wrappers of the packed artifact (allocated < 1 MiB
            # but large apparent size) and anything without real content
            if st.st_blocks * 512 < 1024 * 1024:
                continue
            if f.endswith(".snapshot") and not f.startswith("oc-"):
                targets["snapshot"].append((sid, p))
            elif f.endswith(".overlay"):
                targets["overlay"].append((sid, p))
    return targets


def analyze(files, workers, label):
    print(f"== hashing {len(files)} {label} files ...", file=sys.stderr)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(hash_file, [p for _, p in files]))

    tot = sum(r[0] for r in results)
    zer = sum(r[1] for r in results)
    nz = tot - zer
    fleet = Counter()
    for r in results:
        fleet.update(x for x in r[2] if x)
    uniq = len(fleet)

    lines = [f"\n## {label} artifacts ({len(files)} files)"]
    lines.append(f"- total chunks (4K): {tot:,} ({tot*CHUNK/2**30:.2f} GiB)")
    lines.append(f"- all-zero chunks:   {zer:,} ({zer/tot*100:.2f} %)")
    lines.append(f"- non-zero chunks:   {nz:,}")
    lines.append(f"- unique non-zero:   {uniq:,}")
    lines.append(f"- 4K dedup ratio:    {1 - uniq/nz if nz else 0:.2%}")
    lines.append(f"- deduplicated logical size: {uniq*CHUNK/2**30:.2f} GiB vs {tot*CHUNK/2**30:.2f} GiB "
                 f"({tot*CHUNK/max(1, uniq*CHUNK):.2f}x reduction)")
    if len(files) > 1:
        singles = [Counter(x for x in r[2] if x) for r in results]
        ref = singles[0]
        j = [len(ref & s) / len(ref | s) for s in singles[1:]]
        lines.append(f"- pairwise similarity vs {files[0][0]} (Jaccard): "
                     f"min {min(j):.2%} / avg {sum(j)/len(j):.2%} / max {max(j):.2%}")
    if label == "snapshot":
        sup = Counter()
        supz = 0
        for r in results:
            s, z = super_hashes(r[2])
            sup.update(s)
            supz += z
        tot_sup = tot // GROUP
        lines.append(f"- 64K super-chunks:  {len(sup):,} unique of {tot_sup - supz:,} non-zero "
                     f"(64K dedup {1 - len(sup)/(tot_sup - supz) if tot_sup > supz else 0:.2%})")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshots_dir")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 8)
    args = ap.parse_args()

    targets = collect(args.snapshots_dir)
    out = []
    for kind in ("snapshot", "overlay"):
        files = targets[kind]
        if files:
            out += analyze(files, args.workers, kind)

    # sparsity overview
    out.append("\n## disk allocation (apparent vs allocated)")
    for kind in ("snapshot", "overlay"):
        app = alc = 0
        for _, p in targets[kind]:
            st = os.stat(p)
            app += st.st_size
            alc += st.st_blocks * 512
        if app:
            out.append(f"- {kind}: apparent {app/2**30:.2f} GiB, allocated {alc/2**30:.2f} GiB "
                       f"({alc/app*100:.1f}%)")
    print("\n".join(out))


if __name__ == "__main__":
    main()
