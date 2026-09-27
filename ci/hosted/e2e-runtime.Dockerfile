# Runtime-only acceptance environment. No product sources or language toolchains.
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates curl docker.io e2fsprogs erofs-utils file iproute2 iputils-ping jq \
    libaio1t64 libbz2-1.0 libgflags2.2 liblz4-1 libnuma1 libsnappy1v5 libssl3t64 \
    libstdc++6 liburing2 libzstd1 openssl procps psmisc python3 python3-pip redis-server socat strace \
    tar unzip util-linux xz-utils zstd \
    && rm -rf /var/lib/apt/lists/* \
    && ! command -v go && ! command -v cargo && ! command -v rustc
