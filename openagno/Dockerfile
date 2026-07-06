# ============================================================
# HiClaw Agno Worker — standalone conversational agent runtime
# ============================================================
# No Matrix / MinIO / Nacos SDK. AgentSpec is injected by
# hiclaw-controller via ConfigMap mount at /etc/hiclaw/agentspec.
#
# Build args:
#   HIGRESS_REGISTRY - base image registry
#   PIP_INDEX_URL    - PyPI mirror
#   AGNO_VERSION     - Agno package version (default: 2.7.0a4)
# ============================================================

ARG HIGRESS_REGISTRY=higress-registry.cn-hangzhou.cr.aliyuncs.com

FROM ${HIGRESS_REGISTRY}/higress/python:3.11-slim

ARG APT_MIRROR=
ARG PIP_INDEX_URL=https://pypi.org/simple/
ARG AGNO_VERSION=2.7.0a4

RUN if [ -n "${APT_MIRROR}" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    fi && \
    apt-get update && apt-get install -y --no-install-recommends \
        libjemalloc2 \
        curl \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN JEMALLOC=$(find /usr/lib -name 'libjemalloc.so.2' -print -quit) \
    && echo "LD_PRELOAD=${JEMALLOC}" >> /etc/environment \
    && echo "${JEMALLOC}" > /etc/ld.so.preload

COPY pyproject.toml README.md /tmp/agno-worker/
RUN mkdir -p /tmp/agno-worker/src/agno_worker \
    && echo '__version__ = "0.0.0"' > /tmp/agno-worker/src/agno_worker/__init__.py

RUN python -m venv /opt/venv/agno \
    && /opt/venv/agno/bin/pip install --upgrade pip setuptools wheel \
        --index-url "${PIP_INDEX_URL}" \
        --trusted-host "$(echo ${PIP_INDEX_URL} | sed 's|https\?://||;s|/.*||')" \
    && /opt/venv/agno/bin/pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        --trusted-host "$(echo ${PIP_INDEX_URL} | sed 's|https\?://||;s|/.*||')" \
        "agno==${AGNO_VERSION}" \
        "psycopg[binary]>=3.1" \
        "uvicorn>=0.27" \
        "fastapi>=0.109" \
        "pyyaml>=6.0" \
        "typer>=0.9" \
        "rich>=13.0" \
    && /opt/venv/agno/bin/pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        --trusted-host "$(echo ${PIP_INDEX_URL} | sed 's|https\?://||;s|/.*||')" \
        /tmp/agno-worker/

COPY src/ /tmp/agno-worker/src/
RUN SITE=/opt/venv/agno/lib/python3.11/site-packages \
    && cp -a /tmp/agno-worker/src/agno_worker/* "$SITE/agno_worker/" \
    && rm -rf /tmp/agno-worker/

RUN ln -sf /opt/venv/agno/bin/agno-worker /usr/local/bin/agno-worker

COPY --chmod=755 scripts/agno-worker-entrypoint.sh /opt/hiclaw/scripts/agno-worker-entrypoint.sh

RUN mkdir -p /etc/hiclaw/agentspec /opt/agno-worker

WORKDIR /opt/agno-worker

ENV AGNO_AGENTSPEC_DIR=/etc/hiclaw/agentspec \
    AGNO_DB_URL=postgresql+psycopg://root:vector_store@localhost:5432/postgres \
    AGNO_CONTROL_PORT=8090

ENTRYPOINT ["/opt/hiclaw/scripts/agno-worker-entrypoint.sh"]
