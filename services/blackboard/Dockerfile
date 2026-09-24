FROM python:3.12-slim

ARG DEBIAN_MIRROR=
ARG PIP_INDEX_URL=
RUN if [ -n "$DEBIAN_MIRROR" ]; then \
        sed -i "s|http://deb.debian.org/debian-security|${DEBIAN_MIRROR}-security|g; s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} uv

WORKDIR /app
COPY . .
ENV UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:${PATH}"
RUN --mount=type=cache,id=bbx-uv,target=/root/.cache/uv,sharing=locked \
    uv sync --frozen --no-dev --package bbx-blackboard ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"}

EXPOSE 8000
CMD ["python", "-m", "bbx_blackboard.server"]
