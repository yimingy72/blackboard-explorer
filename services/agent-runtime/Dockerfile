FROM python:3.12-slim

ARG PIP_INDEX_URL=
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} uv

WORKDIR /app
COPY . .
ENV UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:${PATH}"
RUN --mount=type=cache,id=bbx-uv,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --package bbx-runtime ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"}

CMD ["bbx-runtime", "serve"]
