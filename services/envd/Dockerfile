FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
# Optional package mirrors; empty values keep the upstream sources.
ARG APT_MIRROR=
ARG PIP_INDEX_URL=
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://archive.ubuntu.com/ubuntu/|$APT_MIRROR/|g; s|http://security.ubuntu.com/ubuntu/|$APT_MIRROR/|g" \
            /etc/apt/sources.list.d/ubuntu.sources; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
    bash build-essential ca-certificates curl git jq passwd python3 python3-venv tini util-linux zstd \
    && rm -rf /var/lib/apt/lists/*

COPY services/envd /opt/envd/source
RUN python3 -m venv /opt/envd/venv \
    && /opt/envd/venv/bin/pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} /opt/envd/source \
    && chmod -R a-w /opt/envd

ENV PATH="/opt/envd/venv/bin:${PATH}"
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "bbx_envd.app:app", "--host", "0.0.0.0", "--port", "8080"]
