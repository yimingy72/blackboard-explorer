ARG NODE_IMAGE=node:22-bookworm-slim
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${NODE_IMAGE} AS pi
WORKDIR /opt/bbx-pi
COPY services/agent-runtime/pi/package.json services/agent-runtime/pi/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund
COPY services/agent-runtime/pi/worker.mjs services/agent-runtime/pi/trusted-model.mjs ./

FROM ${PYTHON_IMAGE} AS runtime-base

ARG PIP_INDEX_URL=
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} uv

WORKDIR /app
COPY . .
COPY --from=pi /usr/local/bin/node /usr/local/bin/node
COPY --from=pi /opt/bbx-pi /app/services/agent-runtime/pi
ENV UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:${PATH}"
RUN --mount=type=cache,id=bbx-uv,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --package bbx-runtime ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"}

CMD ["bbx-runtime", "serve"]

FROM runtime-base AS kubernetes
ADD --chmod=0555 --checksum=sha256:cfda68cba5848bc3b6c6135ae2f20ba2c78de20059f68789c090166d6abc3e2c https://dl.k8s.io/release/v1.34.0/bin/linux/amd64/kubectl /usr/local/bin/kubectl
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
RUN kubectl version --client

FROM runtime-base AS runtime
