#!/usr/bin/env python3
"""Describe only the kernel release inputs between the selected unit tags."""
import argparse
import importlib.util
from pathlib import Path
import re
import sys

import tag_sources

spec = importlib.util.spec_from_file_location(
    "vmlinux_release_selection", Path(__file__).with_name("preview-selection.py"))
assert spec is not None and spec.loader is not None
selection = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = selection
spec.loader.exec_module(selection)


def remote_tags(root, refs):
    rows = tag_sources.git(root, "ls-remote", "--refs", "origin", *refs).splitlines()
    result = {}
    for row in rows:
        sha, ref = row.split("\t")
        tag_sources.require(ref in refs and ref not in result
                            and re.fullmatch(r"[0-9a-f]{40}", sha), "invalid kernel tag identity")
        result[ref] = sha
    tag_sources.require(set(result) == set(refs), "kernel update tag is missing")
    return result


def fetch_history(root, previous, current, current_sha):
    """Fetch both named tags completely; never accept a truncated comparison."""
    for tag in (previous, current):
        tag_sources.validate_tag("vmlinux", tag)
    refs = ["refs/tags/" + tag for tag in (previous, current)]
    expected = remote_tags(root, refs)
    tag_sources.require(expected[refs[1]] == current_sha, "selected kernel tag moved")
    shallow = tag_sources.git(root, "rev-parse", "--is-shallow-repository")
    tag_sources.require(shallow in ("true", "false"), "cannot establish kernel history completeness")
    tag_sources.git(root, "fetch", "--quiet", "--no-tags",
                    *(["--unshallow"] if shallow == "true" else []),
                    "origin", *(ref + ":" + ref for ref in refs))
    tag_sources.require(tag_sources.git(root, "rev-parse", "--is-shallow-repository") == "false",
                        "kernel update history is incomplete")
    for ref in refs:
        tag_sources.require(tag_sources.git(root, "cat-file", "-t", ref) == "commit"
                            and tag_sources.git(root, "rev-parse", ref) == expected[ref],
                            "kernel update tag identity differs")
    tag_sources.require(remote_tags(root, refs) == expected, "kernel update tags moved during fetch")


def render_updates(root, previous, current):
    for tag in (previous, current):
        tag_sources.validate_tag("vmlinux", tag)
    tag_sources.require(tag_sources.git(root, "rev-parse", "--is-shallow-repository") == "false",
                        "kernel update history is incomplete")
    lines = [f"Selected version: `{previous}` → `{current}`.", ""]
    ahead = int(tag_sources.git(root, "rev-list", "--count", f"{previous}..{current}"))
    behind = int(tag_sources.git(root, "rev-list", "--count", f"{current}..{previous}"))
    if behind and not ahead:
        lines += ["The selected version is behind the previous selection; this is a version rollback, not a forward update.", ""]
    elif ahead and behind:
        lines += ["The selected version has diverged from the previous selection.", ""]
    lines += ["Kernel release inputs: `native-deps/deps/vmlinux/*.config` and `native-deps/deps/linux-patches/`.", ""]
    if not selection.vmlinux_inputs_changed(root, previous, current):
        lines.append("No changes to kernel release inputs between the selected versions.")
    else:
        history = tag_sources.git(root, "log", "--full-history", "--format=%H%x00%s",
                                  f"{previous}..{current}", "--", *selection.VMLINUX_RELEASE_INPUTS)
        if history:
            for row in history.splitlines():
                sha, subject = row.split("\0", 1)
                subject = subject.replace("`", "'")
                lines.append(f"- `{sha[:12]}` {subject} ([commit](https://github.com/kuasar-sandbox/guest-runtime/commit/{sha}))")
        else:
            lines.append("Kernel release inputs differ, but no commits affecting those inputs are unique to the selected version.")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("previous")
    parser.add_argument("current")
    args = parser.parse_args()
    record = tag_sources.inspect_unit(args.source, "vmlinux", args.current)
    fetch_history(args.source, args.previous, args.current, record["sha"])
    tag_sources.require(tag_sources.inspect_unit(args.source, "vmlinux", args.current) == record,
                        "selected kernel source changed while reading history")
    print(render_updates(args.source, args.previous, args.current), end="")


if __name__ == "__main__":
    main()
