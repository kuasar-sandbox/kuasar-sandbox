# Ubuntu's multi-architecture index is pinned; every installed package version
# and both official toolchain archive identities are retained in the image.
FROM ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3 AS toolchain
ARG DEBIAN_FRONTEND=noninteractive
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod 0755 /usr/sbin/policy-rc.d \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       acl apparmor autoconf automake bc binutils bison build-essential ca-certificates \
       clang cmake containerd cpio curl dbus diffutils docker.io e2fsprogs erofs-utils \
       file findutils flex gawk git gperf gzip iproute2 iptables iputils-ping jq kmod \
       libaio-dev libbpf-dev libbz2-dev libcap-dev libclang-dev libelf-dev libfuse3-dev \
       libgcrypt20-dev libgflags-dev libgpg-error-dev liblz4-dev libncurses-dev \
       libnuma-dev libseccomp-dev libsnappy-dev libssl-dev libtool libtool-bin \
       liburing-dev libzstd-dev lld llvm lz4 make netcat-openbsd nftables ninja-build \
       openssl patch pkg-config procps psmisc python3 python3-pip python3-venv \
       python3-yaml redis-server rsync shellcheck skopeo socat sqlite3 strace \
       systemd systemd-sysv tar tcpdump time udev unzip util-linux uuid-dev \
       xz-utils zip zlib1g-dev zstd \
    && mkdir -p /usr/share/workbench \
    && dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > /usr/share/workbench/packages.tsv \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*
COPY toolchains.json install-toolchains.py /usr/share/workbench/
RUN python3 -B /usr/share/workbench/install-toolchains.py
ENV PATH=/usr/local/go/bin:/usr/local/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

FROM toolchain AS workbench
ARG AGGREGATE_VERSION
ARG SOURCE_REVISION
LABEL org.opencontainers.image.title="Kuasar Sandbox workbench" \
      org.opencontainers.image.source="https://github.com/kuasar-sandbox/kuasar-sandbox" \
      org.opencontainers.image.version=$AGGREGATE_VERSION \
      org.opencontainers.image.revision=$SOURCE_REVISION \
      org.opencontainers.image.licenses="Apache-2.0"
COPY daemon.json /etc/docker/daemon.json
COPY containerd.toml /etc/containerd/config.toml
COPY system-entrypoint.sh /usr/local/sbin/workbench-system
COPY check-system.py /usr/local/lib/workbench/check-system.py
COPY LICENSE /usr/share/workbench/LICENSE
COPY deps/ /opt/workbench/deps/
RUN chmod 0755 /usr/local/sbin/workbench-system \
    && mkdir -p /etc/systemd/system/docker.service.d /etc/systemd/system/containerd.service.d \
    && printf '[Service]\nLimitNOFILE=1048576\n' > /etc/systemd/system/docker.service.d/limits.conf \
    && cp /etc/systemd/system/docker.service.d/limits.conf /etc/systemd/system/containerd.service.d/limits.conf \
    && systemctl enable containerd.service docker.service \
    && systemctl mask getty.target console-getty.service systemd-logind.service redis-server.service \
    && rm -f /etc/machine-id /var/lib/dbus/machine-id \
    && touch /etc/machine-id \
    && ln -s /etc/machine-id /var/lib/dbus/machine-id \
    && mkdir -p /inputs/release /work /output /build /var/log/journal \
    && if [ -d /root/.ssh ]; then rmdir /root/.ssh; fi \
    && test ! -e /var/lib/docker/containers \
    && test ! -e /root/.docker/config.json \
    && test ! -e /root/.ssh
ENV container=docker E2E_DEPS_DIR=/opt/workbench/deps E2E_OFFLINE=1
STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/usr/local/sbin/workbench-system"]
CMD ["system"]
