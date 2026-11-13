#!/usr/bin/env bash
# e2e-requires: ["bpf-map","bpf-tc","cgroup-v2","docker","journald","kvm","netns","podman","quadlet","systemd","tun","userfaultfd"]
set -euo pipefail
umask 077
source "${E2E_LIB:?}/common.sh"
require_root
require_kvm
for tool in python3 docker dockerd nft systemctl ip mkfs.ext4; do require_command "$tool"; done
for binary in flatten-ctl sandbox-ctl e2b-key-ctl; do require_binary "$binary"; done
python3 -B "$E2E_LIB/platform/node_infra_quickstart.py"
python3 -B "$E2E_LIB/platform/node_infra_docker.py"
