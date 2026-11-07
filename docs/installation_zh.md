[English](installation.md) | [简体中文](installation_zh.md)

# 安装与版本选择

目标是得到可校验、版本匹配的本地安装目录。安装后按[快速开始](quickstart_zh.md)首次运行，或按[部署](deployment_zh.md)装配生产节点。

在原生 Linux 主机的同一个 Bash 会话中运行以下命令，需要 Python 3.9+、curl、
GNU tar 和 sha256sum。导入 Workbench 时才需要 Docker；仅下载并解压原生产品不依赖它。
此安装任务不需要 GitHub 登录、源码检出或编译器。运行产品还有额外前提，
以所选版本的部署指南为准。

将 `RELEASE_VERSION` 设为已发布的聚合 tag，或留空以一次性解析 Stable。该聚合 tag 下的 manifest 决定全部六个发布单元和精确包名；runtime 与 vmlinux 使用独立 tag。不要混用组件 Latest 或从 main 替换文件。旧版本应遵循其包内指南。

## 选版并下载本机架构的输入

先检查 Release 的完整资产集合，再只下载本机架构及共享 platform 包。仅在采用该版本指南中的原生方案时设置 `DOWNLOAD_WORKBENCH=0`。下载固定 GitHub 资产 ID，并校验 GitHub 原生大小和 SHA-256 digest；摘要缺失或校验失败立即停止。历史正文机器块和 SHA256SUMS 均被忽略。

`files.tsv` 仅在本地保存已下载文件的名称、大小和哈希，供离线复核；它不是发布资产或测试记录。每次使用新的下载和安装目录。

内联 Python 校验使用 `-I` 隔离模式，防止继承的 `PYTHONOPTIMIZE` 关闭断言校验。

```bash
set -euo pipefail
RELEASE_VERSION="${RELEASE_VERSION:-}"
ARCH="$(uname -m)"
case "$ARCH" in x86_64|aarch64) ;; *) echo "unsupported native architecture: $ARCH" >&2; exit 1 ;; esac
DOWNLOAD_WORKBENCH="${DOWNLOAD_WORKBENCH:-1}"
export ARCH DOWNLOAD_WORKBENCH
ASSETS_TSV="$(mktemp)"
trap 'rm -f -- "$ASSETS_TSV"' EXIT
RELEASE_VERSION="$(python3 -I - "$RELEASE_VERSION" "$ASSETS_TSV" <<'PY'
import base64, json, os, re, sys
from urllib.request import Request, urlopen
requested, output = sys.argv[1:]
api = 'https://api.github.com/repos/kuasar-sandbox/kuasar-sandbox'
version_re = r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-preview\.[0-9]{8}(?:\.[1-9][0-9]*)?)?'
assert not requested or re.fullmatch('release-v' + version_re, requested), 'invalid aggregate version'
def read(path):
    request = Request(api + path, headers={'Accept': 'application/vnd.github+json',
                                          'X-GitHub-Api-Version': '2022-11-28'})
    with urlopen(request, timeout=60) as response:
        return json.load(response)
release = read('/releases/tags/' + requested if requested else '/releases/latest')
tag = release['tag_name']
assert re.fullmatch('release-v' + version_re, tag) and (not requested or tag == requested)
assert release['draft'] is False and release['prerelease'] == ('-preview.' in tag)
# The tag fixes the manifest. Never reinterpret an old version using main/latest.
commit = read('/commits/refs/tags/' + tag)['sha']
path = 'releases/' + ('daily-preview.yaml' if '-preview.' in tag else 'release.yaml')
response = read('/contents/' + path + '?ref=' + tag)
assert response['type'] == 'file' and response['encoding'] == 'base64'
text = base64.b64decode(response['content']).decode('utf-8')
assert read('/commits/refs/tags/' + tag)['sha'] == commit, 'tag moved during manifest download'
fields, units, section = {}, {}, ''
for raw in text.splitlines():
    line = raw.split('#', 1)[0].rstrip()
    if not line.strip():
        continue
    if not raw[:1].isspace():
        key, separator, value = line.partition(':')
        assert separator, 'invalid manifest'
        section = key
        if key in ('version', 'preview_version', 'delivery'):
            assert key not in fields, 'duplicate manifest field'
            fields[key] = value.strip()
    elif section == 'components':
        match = re.fullmatch(r'  ([a-z]+): +([^ ]+)', line)
        assert match, 'invalid component selection'
        unit, version = match.groups()
        assert unit not in units, 'duplicate selected component'
        units[unit] = version
assert set(units) == {'accelerator', 'connector', 'sandboxer', 'orchestrator', 'runtime', 'vmlinux'}
assert fields['version'] + ('-' + fields['preview_version'] if '-preview.' in tag else '') == tag
for unit, version in units.items():
    prefix = unit + '-v' if unit in ('runtime', 'vmlinux') else 'v'
    assert re.fullmatch(prefix + version_re, version), ('invalid component tag', unit)
    assert '-preview.' in tag or '-preview.' not in version, ('Stable requires stable component', unit)
contract = fields.get('delivery', 'historical')
assert contract in ('historical', 'workbench-v1')
assert 'delivery' not in fields or contract == 'workbench-v1', 'unsupported explicit delivery contract'
arch = os.environ['ARCH']
workbench = os.environ['DOWNLOAD_WORKBENCH']
assert workbench in ('0', '1')
assert workbench == '0' or contract == 'workbench-v1', 'selected version has no Workbench; use its native guide with DOWNLOAD_WORKBENCH=0'
def names(architecture):
    result = set()
    for unit, version in units.items():
        if unit == 'runtime':
            result.add(f'sandbox-runtime-{architecture}-{version.removeprefix("runtime-")}.tar.gz')
        elif unit == 'vmlinux':
            result.add(f'vmlinux-{architecture}-{version.removeprefix("vmlinux-")}.tar.gz')
        else:
            result.add(f'{unit}-{version}-linux-{architecture}.tar.gz')
    return result
assets = [a for a in release['assets'] if a['name'] != 'SHA256SUMS']
actual = {a['name'] for a in assets}
assert len(actual) == len(assets), 'duplicate asset name'
platform_name = f'platform-{tag}.tar.gz'
expected = {platform_name} | names('x86_64')
if contract == 'workbench-v1' or actual & names('aarch64'):
    expected |= names('aarch64')
if contract == 'workbench-v1':
    expected |= {f'workbench-{a}-{tag.removeprefix("release-")}.tar.gz' for a in ('x86_64', 'aarch64')}
assert actual == expected, 'missing or unexpected aggregate asset'
selected = {platform_name} | names(arch)
if workbench == '1':
    selected.add(f'workbench-{arch}-{tag.removeprefix("release-")}.tar.gz')
assert selected <= actual, 'selected native architecture is unavailable'
with open(output, 'w', encoding='utf-8') as stream:
    for asset in sorted(assets, key=lambda a: a['name']):
        name, size, digest, aid = (asset.get(k) for k in ('name', 'size', 'digest', 'id'))
        assert re.fullmatch(r'[A-Za-z0-9._-]+', name)
        assert asset['state'] == 'uploaded' and type(aid) is int and aid > 0
        assert type(size) is int and size > 0
        assert re.fullmatch(r'sha256:[0-9a-f]{64}', digest or ''), ('missing asset digest', name)
        if name in selected:
            stream.write(f'{name}\t{size}\t{digest}\t{api}/releases/assets/{aid}\n')
print(tag)
PY
)"
DOWNLOAD_DIR="$PWD/kuasar-download-$RELEASE_VERSION"
INSTALL_DIR="$PWD/kuasar-$RELEASE_VERSION"
test ! -e "$DOWNLOAD_DIR"
test ! -e "$INSTALL_DIR"
mkdir -m 0755 "$DOWNLOAD_DIR"
while IFS=$'\t' read -r name size digest url; do
    curl --fail --silent --show-error --location --retry 4 \
        -H 'Accept: application/octet-stream' -H 'X-GitHub-Api-Version: 2022-11-28' \
        --output "$DOWNLOAD_DIR/$name.part" "$url"
    test "$(stat -c %s "$DOWNLOAD_DIR/$name.part")" = "$size"
    test "sha256:$(sha256sum "$DOWNLOAD_DIR/$name.part" | awk '{print $1}')" = "$digest"
    mv "$DOWNLOAD_DIR/$name.part" "$DOWNLOAD_DIR/$name"
done <"$ASSETS_TSV"
cut -f1-3 "$ASSETS_TSV" >"$DOWNLOAD_DIR/files.tsv"
rm -f -- "$ASSETS_TSV"
trap - EXIT
printf 'Selected aggregate Release: %s\n' "$RELEASE_VERSION"
```

## 复核所选字节并解压产品与材料

复核每个已下载文件，再检查归档类型、路径、权限及跨包路径冲突，全部通过后才解压。未选择架构的文件无需下载。Workbench 单独导入。

```bash
set -euo pipefail
python3 -I - "$DOWNLOAD_DIR" <<'PY'
import hashlib, pathlib, re, sys, tarfile
root = pathlib.Path(sys.argv[1])
rows = [line.split('\t') for line in (root / 'files.tsv').read_text().splitlines()]
assert rows and all(len(row) == 3 for row in rows), 'invalid local file list'
assert len(rows) == len({row[0] for row in rows}), 'duplicate local filename'
archives = []
for name, size, digest in rows:
    assert re.fullmatch(r'[A-Za-z0-9._-]+\.tar\.gz', name), 'unsafe filename'
    assert size.isdecimal() and int(size) > 0 and re.fullmatch(r'sha256:[0-9a-f]{64}', digest)
    asset = root / name
    assert asset.is_file() and not asset.is_symlink(), ('missing selected asset', name)
    assert asset.stat().st_size == int(size), ('selected asset size', name)
    hasher = hashlib.sha256()
    with asset.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(chunk)
    assert 'sha256:' + hasher.hexdigest() == digest, ('selected asset digest', name)
    if not name.startswith('workbench-'):
        archives.append(name)
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
while IFS=$'\t' read -r name size digest; do
    case "$name" in workbench-*) continue ;; esac
    tar --extract --gzip --no-same-owner --no-same-permissions \
        --file "$DOWNLOAD_DIR/$name" --directory "$STAGE_DIR"
done <"$DOWNLOAD_DIR/files.tsv"
test -f "$STAGE_DIR/test/demo/demo_common.sh"
test -f "$STAGE_DIR/test/demo/requirements.txt"
test -d "$STAGE_DIR/bin" && test -d "$STAGE_DIR/test"
if [ "$DOWNLOAD_WORKBENCH" = 1 ]; then
    test -f "$STAGE_DIR/workbench/workbench"
    test -d "$STAGE_DIR/guide"
    test -f "$STAGE_DIR/test/demo/prepared.py" || {
        echo 'selected Release predates the prepared Demo guide; use its bundled instructions' >&2
        exit 1
    }
fi
mv "$STAGE_DIR" "$INSTALL_DIR"
STAGE_DIR=""
trap - EXIT
```

## 单独导入 Workbench

默认 `DOWNLOAD_WORKBENCH=1` 时，导入已下载归档，检查本机架构与版本，
再保留本地 image ID 用于启动。`DOWNLOAD_WORKBENCH=0` 时，下方命令只设置
`RELEASE` 并将 `IMAGE` 留空，不调用 Docker，也不寻找未下载的归档。

```bash
RELEASE="$INSTALL_DIR"
IMAGE=""
if [ "$DOWNLOAD_WORKBENCH" = 1 ]; then
    IMAGE_TAG="ghcr.io/kuasar-sandbox/workbench:${RELEASE_VERSION#release-}"
    docker load --input "$DOWNLOAD_DIR/workbench-$ARCH-${RELEASE_VERSION#release-}.tar.gz"
    case "$ARCH" in x86_64) DOCKER_ARCH=amd64 ;; aarch64) DOCKER_ARCH=arm64 ;; esac
    test "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE_TAG")" = "linux/$DOCKER_ARCH"
    test "$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$IMAGE_TAG")" = "$RELEASE_VERSION"
    IMAGE="$(docker image inspect --format '{{.Id}}' "$IMAGE_TAG")"
    printf 'Local Workbench image: %s\n' "$IMAGE"
fi
printf 'Release directory: %s\n' "$RELEASE"
```

根据实际下载的输入选择下一步：

- **Workbench：** 在当前会话保留 `RELEASE`、`IMAGE` 和 `ARCH`，继续
  [快速开始](quickstart_zh.md#启动私有系统环境)。启动器来自
  `$RELEASE/workbench/workbench`。复用已验证安装时，使用其发布目录和本地 image ID。
- **仅原生产品：** 使用 `RELEASE` 和 `ARCH`，按该版本包内的部署或原生指南继续。
  此时没有可启动的 Workbench 镜像，不要执行 Workbench 快速开始命令。

缺文件、架构不符、摘要、权限或路径检查失败时停止，不要从其他版本获取替代文件。

## 安装结果

保留所选聚合 Tag、`files.tsv` 和原始下载包。`bin/` 保存产品工具，`guide/README.md` 是项目入口，`guide/index.md` 是完整文档目录，组件指南在 `guide/<component>/`。测试与演示材料在 `test/`，Workbench 在独立镜像中。历史版本的目录以其自带指南为准。
