# ============================================================
# HiClaw sensitive-content — 敏感内容规则管理服务
# ============================================================
# python-slim 多阶段构建：build 阶段装依赖进 venv，运行阶段仅
# 携带 venv + migrations，镜像内不含构建工具链。
#
# Build args:
#   HIGRESS_REGISTRY - 基础镜像 registry
#   PIP_INDEX_URL    - PyPI 镜像源
#   APT_MIRROR       - APT 镜像源（可选）
# ============================================================

ARG HIGRESS_REGISTRY=higress-registry.cn-hangzhou.cr.aliyuncs.com

# ---------- 构建阶段 ----------
FROM ${HIGRESS_REGISTRY}/higress/python:3.11-slim AS build

ARG PIP_INDEX_URL=https://pypi.org/simple/

COPY pyproject.toml README.md /tmp/sensitive-content/
COPY src/ /tmp/sensitive-content/src/

RUN python -m venv /opt/venv/sensitive-content \
    && /opt/venv/sensitive-content/bin/pip install --upgrade pip setuptools wheel \
        --index-url "${PIP_INDEX_URL}" \
        --trusted-host "$(echo ${PIP_INDEX_URL} | sed 's|https\?://||;s|/.*||')" \
    && /opt/venv/sensitive-content/bin/pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        --trusted-host "$(echo ${PIP_INDEX_URL} | sed 's|https\?://||;s|/.*||')" \
        /tmp/sensitive-content/

# ---------- 运行阶段 ----------
FROM ${HIGRESS_REGISTRY}/higress/python:3.11-slim

ARG APT_MIRROR=

RUN if [ -n "${APT_MIRROR}" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    fi && \
    apt-get update && apt-get install -y --no-install-recommends \
        curl \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv/sensitive-content /opt/venv/sensitive-content
COPY migrations/ /opt/sensitive-content/migrations/

RUN ln -sf /opt/venv/sensitive-content/bin/sensitive-content /usr/local/bin/sensitive-content

WORKDIR /opt/sensitive-content

ENV SENSITIVE_CONTENT_MIGRATIONS_DIR=/opt/sensitive-content/migrations \
    SENSITIVE_CONTENT_PORT=8091

EXPOSE 8091

ENTRYPOINT ["sensitive-content"]
CMD ["serve"]
