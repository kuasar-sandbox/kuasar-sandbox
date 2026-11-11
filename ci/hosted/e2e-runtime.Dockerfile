# Runtime-only acceptance environment. No product sources or language toolchains.
FROM ubuntu:24.04
# Use a signed Ubuntu mirror only during this build when the primary repository is unavailable.
RUN set -e; \
    if ! apt-get --error-on=any update; then \
        sed -E '/^URIs:/s#https?://(archive|security|ports)\.ubuntu\.com/#http://mirrors.mit.edu/#g' \
            /etc/apt/sources.list.d/ubuntu.sources > /tmp/build-ubuntu.sources; \
        set -- -o Dir::Etc::sourcelist=/tmp/build-ubuntu.sources -o Dir::Etc::sourceparts=-; \
        apt-get "$@" --error-on=any update; \
    fi; \
    DEBIAN_FRONTEND=noninteractive apt-get "$@" install -y --no-install-recommends \
    ca-certificates curl docker.io e2fsprogs erofs-utils file iproute2 iputils-ping jq \
    libaio1t64 libbz2-1.0 libgflags2.2 liblz4-1 libnuma1 libsnappy1v5 libssl3t64 \
    libstdc++6 liburing2 libzstd1 openssl procps psmisc python3 python3-pip redis-server socat strace \
    tar unzip util-linux xz-utils zstd \
    && rm -rf /var/lib/apt/lists/* /tmp/build-ubuntu.sources \
    && for tool in go cargo rustc cc gcc g++ clang clang++; do \
        if command -v "$tool" >/dev/null 2>&1; then echo "unexpected compiler: $tool" >&2; exit 1; fi; \
    done
