# Local sandbox image. It is not published on Docker Hub.
# Base is pulled through a registry mirror, then tagged python:3.10-slim-bookworm.
ARG BASE_IMAGE=python:3.10-slim-bookworm
FROM ${BASE_IMAGE}

USER root
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i \
        -e 's|deb.debian.org|mirrors.aliyun.com|g' \
        -e 's|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i \
        -e 's|deb.debian.org|mirrors.aliyun.com|g' \
        -e 's|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      git ca-certificates procps coreutils util-linux passwd; \
    rm -rf /var/lib/apt/lists/*; \
    pip install --no-cache-dir \
      -i https://pypi.tuna.tsinghua.edu.cn/simple \
      numpy pandas requests scipy matplotlib pytest; \
    if ! id -u 1000 >/dev/null 2>&1; then \
      useradd --uid 1000 --create-home --shell /bin/sh sandbox; \
    fi

WORKDIR /workspace
