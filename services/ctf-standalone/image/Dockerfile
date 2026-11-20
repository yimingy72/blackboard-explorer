ARG TOOLS_BASE=bbx-peer-sandbox:20261120
FROM node:22.21.1-bookworm-slim@sha256:25b3eb23a00590b7499f2a2ce939322727fcce1b15fdd69754fcd09536a3ae2c AS node
FROM ${TOOLS_BASE} AS tools

USER root
WORKDIR /tmp
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        nmap sqlmap dirsearch python3-scapy python3-numpy python3-httpx python3-bs4 \
        ffmpeg sox multimon-ng zbar-tools qrencode \
        xxd cmake pkg-config nasm \
        poppler-utils qpdf python3-pip \
    && apt-get clean

ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    DEBUGINFOD_URLS=""

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -sfn ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sfn ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY slim_root.py /tmp/ctf-slim-root.py
RUN mkdir -p /opt/ctf-tools \
    && python3 /tmp/ctf-slim-root.py \
    && rm /tmp/ctf-slim-root.py \
    && mkdir -p /workspace /opt/ctf-tools /etc/gdb/gdbinit.d \
    && (getent passwd 10000 || useradd --uid 10000 --home-dir /workspace --shell /bin/bash ctf) \
    && chown 10000:10000 /workspace \
    && printf 'set debuginfod enabled off\n' > /etc/gdb/gdbinit.d/ctf-offline.gdb

# Keep the large cleaned root stable; extra tools and verification are small layers.
FROM scratch
COPY --from=tools / /
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/workspace \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    DEBUGINFOD_URLS=""
WORKDIR /tmp
USER root
RUN rm -f /usr/local/bin/sage
COPY --chmod=0755 sage sage-python /usr/local/bin/
COPY sources.json /opt/ctf-tools/sources.json
COPY install_extra_tools.py /tmp/ctf-install-extras.py
RUN python3 /tmp/ctf-install-extras.py && rm /tmp/ctf-install-extras.py

COPY tool_manifest.py /opt/ctf-tools/tool_manifest.py
COPY smoke_tools.py /opt/ctf-tools/smoke_tools.py
RUN HOME=/root python3 /opt/ctf-tools/tool_manifest.py /opt/ctf-tools/manifest.json \
    && rm -rf /workspace/.sage /root/.sage
USER 10000:10000
WORKDIR /workspace
RUN --network=none python3 /opt/ctf-tools/smoke_tools.py > /workspace/tool-smoke.jsonl \
    && rm -rf /workspace/.sage \
    || (cat /workspace/tool-smoke.jsonl; exit 1)
USER root
RUN HOME=/root python3 /opt/ctf-tools/tool_manifest.py /opt/ctf-tools/manifest.json \
        --smoke /workspace/tool-smoke.jsonl \
    && rm /workspace/tool-smoke.jsonl \
    && rm -rf /root/.sage /workspace/.sage

WORKDIR /workspace
USER 10000:10000
ENTRYPOINT []
CMD ["/bin/bash"]
LABEL org.opencontainers.image.title="BBX standalone CTF tools base" \
    org.opencontainers.image.description="CTF tools only; the competition Agent harness is not included"
