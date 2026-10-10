[English](download.md) | [简体中文](download_zh.md)

# Acquire a matching aggregate release

This is the acquisition reference for [Quick Start](quickstart.md). Run on the
native Linux **Docker host**, in one Bash shell, with Python 3.9+, curl, GNU tar,
sha256sum and Docker access. No GitHub login, `gh`, source checkout or compiler
is needed. Workbench supplies the product user-space libraries and Python 3.12;
do not install those libraries or the Demo SDK on the host for this path.

Resolve Stable once, or explicitly set `RELEASE_VERSION` to a published
`release-vX.Y.Z[-preview.YYYYMMDD[.N]]`. Every selected asset comes from that
aggregate. In new releases, each owner's products, tests, helpers and guides
come from its selected owner tag; kernel guides use the independent vmlinux tag.
Historical releases retain their actual recorded origins, which may differ,
and must be used with their own guides.
Never mix individual component Latest releases or retrieve replacement scripts
from `main`. An old Stable can lack this workflow: select a suitable published
version explicitly, or use its historical native guide. This procedure never
silently switches Stable to Preview. The source selection manifest is not a
latest-published channel.

The workbench-first path requires both `workbench-v1` delivery and the bundled
`test/demo/prepared.py` adapter. Older workbench releases retain their own
instructions. New source documentation does not retroactively add files to them.
`DOWNLOAD_WORKBENCH=0` is only for the explicit native alternative using the
selected version's own guide; do not continue with the workbench steps below.

## 1. Select and download native inputs

The default downloads native products, the platform materials, matching
workbench archive and checksums, while validating the complete declared asset
set. It keeps GitHub metadata, selected URLs, digests and sizes under
`DOWNLOAD_DIR`. Use new download/install directories for a new attempt.

```bash
set -euo pipefail
RELEASE_VERSION="${RELEASE_VERSION:-}"
ARCH="$(uname -m)"
case "$ARCH" in x86_64|aarch64) ;; *) echo "unsupported native architecture: $ARCH" >&2; exit 1 ;; esac
DOWNLOAD_WORKBENCH="${DOWNLOAD_WORKBENCH:-1}"
export ARCH DOWNLOAD_WORKBENCH
RELEASE_METADATA="$(mktemp)"
ASSETS_TSV="$(mktemp)"
SELECTION_MANIFEST="$(mktemp)"
trap 'rm -f -- "$RELEASE_METADATA" "$ASSETS_TSV" "$SELECTION_MANIFEST"' EXIT

if [ -n "$RELEASE_VERSION" ]; then
    [[ "$RELEASE_VERSION" =~ ^release-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-preview\.[0-9]{8}(\.[1-9][0-9]*)?)?$ ]]
    RELEASE_API="https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox/releases/tags/$RELEASE_VERSION"
else
    RELEASE_API="https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox/releases/latest"
fi
curl --fail --silent --show-error --location --retry 4 \
    -H 'Accept: application/vnd.github+json' \
    -H 'X-GitHub-Api-Version: 2022-11-28' \
    "$RELEASE_API" >"$RELEASE_METADATA"

# Validate automatic source provenance; retrieve released files only by tag.
SOURCE_SHA="$(python3 -c 'import json,re,sys; value=json.load(open(sys.argv[1]))["target_commitish"]; assert re.fullmatch(r"[0-9a-f]{40}",value); print(value)' "$RELEASE_METADATA")"
RELEASE_TAG="$(python3 -c '
import json, re, sys
tag = json.load(open(sys.argv[1], encoding="utf-8"))["tag_name"]
assert re.fullmatch(r"release-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-preview\.[0-9]{8}(?:\.[1-9][0-9]*)?)?", tag), tag
assert not sys.argv[2] or sys.argv[2] == tag, (sys.argv[2], tag)
print(tag)
' "$RELEASE_METADATA" "$RELEASE_VERSION")"
case "$RELEASE_TAG" in
    *-preview.*) MANIFEST_PATH="releases/daily-preview.yaml" ;;
    *) MANIFEST_PATH="releases/release.yaml" ;;
esac
curl --fail --silent --show-error --location --retry 4 \
    "https://raw.githubusercontent.com/kuasar-sandbox/kuasar-sandbox/$RELEASE_TAG/$MANIFEST_PATH" >"$SELECTION_MANIFEST"

RELEASE_VERSION="$(python3 - "$RELEASE_METADATA" "$RELEASE_VERSION" "$ASSETS_TSV" "$SELECTION_MANIFEST" <<'PY'
import json, os, platform, re, sys
metadata, requested, output, manifest = sys.argv[1:]
release = json.load(open(metadata, encoding="utf-8"))
tag = release.get("tag_name", "")
tag_re = re.compile(r"^release-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-preview\.[0-9]{8}(?:\.[1-9][0-9]*)?)?$")
assert tag_re.fullmatch(tag), tag
assert not release.get("draft")
assert bool(re.search(r"-preview\.[0-9]{8}(?:\.[1-9][0-9]*)?$", tag)) == bool(release.get("prerelease"))
assert not requested or requested == tag, (requested, tag)
arch = os.environ.get("ARCH", platform.machine())
assert arch in ("x86_64", "aarch64"), arch
other_arch = "aarch64" if arch == "x86_64" else "x86_64"
patterns = {
    "platform": re.compile(rf"^platform-{re.escape(tag)}\.tar\.gz$"),
    "accelerator": re.compile(rf"^accelerator-v[^/]+-linux-{arch}\.tar\.gz$"),
    "connector": re.compile(rf"^connector-v[^/]+-linux-{arch}\.tar\.gz$"),
    "orchestrator": re.compile(rf"^orchestrator-v[^/]+-linux-{arch}\.tar\.gz$"),
    "sandboxer": re.compile(rf"^sandboxer-v[^/]+-linux-{arch}\.tar\.gz$"),
    "runtime": re.compile(rf"^sandbox-runtime-{arch}-v[^/]+\.tar\.gz$"),
    "vmlinux": re.compile(rf"^vmlinux-{arch}-v[^/]+\.tar\.gz$"),
}
assets = release.get("assets", [])
names = [a.get("name", "") for a in assets]
assert len(names) == len(set(names))
assert names.count("SHA256SUMS") == 1
bindings = re.findall(r"<!-- kuasar-integration-validation (.*?) -->", release.get("body", ""), re.S)
assert len(bindings) <= 1
binding = json.loads(bindings[0]) if bindings else {}
# The exact source declares compatibility, never missing image assets/notes.
fields = {}
for line in open(manifest, encoding="utf-8"):
    if line[:1].isspace() or not line.strip() or line.startswith("#"):
        continue
    key, separator, value = line.partition(":")
    assert separator
    if key in ("version", "preview_version", "delivery"):
        assert key not in fields
        fields[key] = value.strip()
selected = fields["version"]
if "-preview." in tag:
    selected += "-" + fields["preview_version"]
assert selected == tag
contract = fields.get("delivery", "historical")
assert "delivery" not in fields or contract == "workbench-v1"
if contract != "workbench-v1" and os.environ.get("DOWNLOAD_WORKBENCH", "0") == "1":
    raise SystemExit("selected Release has no workbench-v1 delivery; explicitly select a suitable published version, or set DOWNLOAD_WORKBENCH=0 and follow the selected historical native guide")
if contract == "workbench-v1":
    assert binding.get("delivery") == contract
roles = {"SHA256SUMS": "checksum"}
for label, pattern in patterns.items():
    matches = [name for name in names if pattern.fullmatch(name)]
    assert len(matches) == 1, (label, matches)
    roles[matches[0]] = "platform" if label == "platform" else "product"
other_patterns = [re.compile(pattern.pattern.replace(arch, other_arch))
                for label, pattern in patterns.items() if label != "platform"]
other = [name for name in names if any(pattern.fullmatch(name) for pattern in other_patterns)]
if contract == "workbench-v1" or other:
    for pattern in other_patterns:
        assert sum(bool(pattern.fullmatch(name)) for name in names) == 1
expected = set(roles) | set(other)
if contract == "workbench-v1":
    expected |= {f"workbench-{arch}-{tag.removeprefix('release-')}.tar.gz" for arch in ("x86_64", "aarch64")}
    if os.environ.get("DOWNLOAD_WORKBENCH", "0") == "1":
        roles[f"workbench-{arch}-{tag.removeprefix('release-')}.tar.gz"] = "workbench"
assert set(names) == expected, (sorted(names), sorted(expected))
assert os.environ.get("DOWNLOAD_WORKBENCH", "0") in ("0", "1")
prefix = f"https://github.com/kuasar-sandbox/kuasar-sandbox/releases/download/{tag}/"
with open(output, "w", encoding="utf-8") as stream:
    for asset in sorted(assets, key=lambda item: item["name"]):
        name, url, digest, size = (asset.get(k) for k in ("name", "browser_download_url", "digest", "size"))
        assert re.fullmatch(r"[A-Za-z0-9._-]+", name), name
        assert url == prefix + name, url
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest or ""), (name, digest)
        assert isinstance(size, int) and size > 0, (name, size)
        if name in roles:
            stream.write(f"{name}\t{url}\t{digest}\t{size}\t{roles[name]}\n")
print(tag)
PY
)"

DOWNLOAD_DIR="$PWD/kuasar-download-$RELEASE_VERSION"
INSTALL_DIR="$PWD/kuasar-$RELEASE_VERSION"
test ! -e "$DOWNLOAD_DIR"
test ! -e "$INSTALL_DIR"
mkdir -m 0755 "$DOWNLOAD_DIR"
install -m 0644 "$RELEASE_METADATA" "$DOWNLOAD_DIR/release.json"
install -m 0644 "$ASSETS_TSV" "$DOWNLOAD_DIR/assets.tsv"

while IFS=$'\t' read -r name url digest size role; do
    curl --fail --silent --show-error --location --retry 4 \
        --output "$DOWNLOAD_DIR/$name.part" "$url"
    test "$(stat -c %s "$DOWNLOAD_DIR/$name.part")" = "$size"
    test "sha256:$(sha256sum "$DOWNLOAD_DIR/$name.part" | awk '{print $1}')" = "$digest"
    mv "$DOWNLOAD_DIR/$name.part" "$DOWNLOAD_DIR/$name"
done <"$DOWNLOAD_DIR/assets.tsv"
rm -f -- "$RELEASE_METADATA" "$ASSETS_TSV" "$SELECTION_MANIFEST"
trap - EXIT
printf 'Pinned aggregate Release: %s\n' "$RELEASE_VERSION"
```

## 2. Verify selected bytes and extract products/materials

All selected files must exist and match their recorded size and digest. Missing
unselected architecture files do not invalidate a native-only download. The
checksum list must still describe exactly the release's assets. Validate member
types, paths, permissions and cross-archive collisions before extracting into a
new staging directory. Standard relative root directories (`.` or `./`) are
accepted only as directories without set-ID bits; empty or absolute root names
are rejected. The workbench archive is deliberately not extracted here.

```bash
set -euo pipefail
(
cd "$DOWNLOAD_DIR"
sha256sum --quiet --check --ignore-missing SHA256SUMS
)

python3 - "$DOWNLOAD_DIR" <<'PY'
import hashlib, json, pathlib, re, sys, tarfile
root = pathlib.Path(sys.argv[1])
rows = [line.split("\t") for line in (root / "assets.tsv").read_text().splitlines()]
archives = [row[0] for row in rows if row[4] in ("platform", "product")]
checksum_re = re.compile(r"^([0-9a-f]{64}) [ *]([A-Za-z0-9._-]+)$")
checksums = {}
for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
    match = checksum_re.fullmatch(line)
    assert match, line
    digest, name = match.groups()
    assert name not in checksums
    checksums[name] = digest
metadata = json.loads((root / "release.json").read_text())
assert set(checksums) == {a["name"] for a in metadata["assets"] if a["name"] != "SHA256SUMS"}
for name, _, digest, size, role in rows:
    asset = root / name
    assert asset.is_file() and not asset.is_symlink(), ("missing selected asset", name)
    assert asset.stat().st_size == int(size), ("selected asset size", name)
    hasher = hashlib.sha256()
    with asset.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    assert "sha256:" + hasher.hexdigest() == digest, ("selected asset digest", name)
    if role != "checksum":
        assert "sha256:" + checksums[name] == digest
types = {}
for archive in archives:
    with tarfile.open(root / archive, "r:gz") as stream:
        for member in stream.getmembers():
            raw = member.name
            assert raw and not raw.startswith("/"), (archive, raw)
            while raw.startswith("./"):
                raw = raw[2:]
            raw = raw.rstrip("/")
            if raw in ("", "."):
                assert member.isdir(), (archive, member.name)
                assert not (member.mode & 0o6000), (archive, member.name, oct(member.mode))
                continue
            assert "\\" not in raw and not any(ord(c) < 32 for c in raw), (archive, raw)
            path = pathlib.PurePosixPath(raw)
            assert not path.is_absolute() and ".." not in path.parts and "." not in path.parts
            assert "/".join(path.parts) == raw, (archive, raw)
            kind = "dir" if member.isdir() else "file" if member.isfile() else "unsupported"
            assert kind != "unsupported", (archive, raw, member.type)
            assert not (member.mode & 0o6000), (archive, raw, oct(member.mode))
            assert kind == "dir" or not (member.mode & 0o002), (archive, raw, oct(member.mode))
            prior = types.get(raw)
            assert prior is None or prior == kind == "dir", (archive, raw, prior, kind)
            for parent in path.parents:
                if str(parent) != ".":
                    assert types.get(str(parent)) != "file", (archive, raw, str(parent))
            if kind == "file":
                assert not any(existing.startswith(raw + "/") for existing in types), (archive, raw)
            types[raw] = kind
PY

INSTALL_PARENT="$(dirname "$INSTALL_DIR")"
STAGE_DIR="$(mktemp -d "$INSTALL_PARENT/.kuasar-install.XXXXXX")"
cleanup_stage() {
    case "$STAGE_DIR" in "$INSTALL_PARENT"/.kuasar-install.*) rm -rf -- "$STAGE_DIR" ;; esac
}
trap cleanup_stage EXIT
while IFS=$'\t' read -r name url digest size role; do
    case "$role" in
        platform|product) tar --extract --gzip --no-same-owner --no-same-permissions \
            --file "$DOWNLOAD_DIR/$name" --directory "$STAGE_DIR" ;;
    esac
done <"$DOWNLOAD_DIR/assets.tsv"

test -f "$STAGE_DIR/test/demo/demo_common.sh"
test -f "$STAGE_DIR/test/demo/requirements.txt"
test -d "$STAGE_DIR/bin" && test -d "$STAGE_DIR/test"
if [ "$DOWNLOAD_WORKBENCH" = 1 ]; then
    test -f "$STAGE_DIR/workbench/workbench"
    test -d "$STAGE_DIR/guide"
    test -f "$STAGE_DIR/test/demo/prepared.py" || {
        echo 'selected Release predates the prepared Demo guide; use its bundled instructions or explicitly select a newer published version' >&2
        exit 1
    }
fi
mv "$STAGE_DIR" "$INSTALL_DIR"
STAGE_DIR=""
trap - EXIT
```

## 3. Import workbench separately

```bash
IMAGE="ghcr.io/kuasar-sandbox/workbench:${RELEASE_VERSION#release-}"
docker load --input "$DOWNLOAD_DIR/workbench-$ARCH-${RELEASE_VERSION#release-}.tar.gz"
test "$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$IMAGE")" = "$RELEASE_VERSION"
RELEASE="$INSTALL_DIR"
printf 'Release directory: %s\nWorkbench image: %s\n' "$RELEASE" "$IMAGE"
```

Keep `RELEASE`, `IMAGE` and `ARCH` in this shell and continue with
[Quick Start](quickstart.md#2-start-a-private-system-environment). The launcher is
`$RELEASE/workbench/workbench`; it came from the selected platform archive, not
from a checkout. The archive is a gzip-compressed Docker image archive, not a
root filesystem or another product tarball.

For an already verified installation, set those variables to its absolute
release directory, matching image tag or ID, and native architecture instead of
redownloading. Stop on checksum, naming, missing-input or historical-contract
errors; do not repair a selected release by fetching files from another version.
