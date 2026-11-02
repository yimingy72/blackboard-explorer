FROM node:22-bookworm-slim AS pi
WORKDIR /opt/bbx-pi
COPY services/agent-runtime/pi/package.json services/agent-runtime/pi/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund
COPY services/agent-runtime/pi/worker.mjs ./

FROM python:3.12-slim

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
