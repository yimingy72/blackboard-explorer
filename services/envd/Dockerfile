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
    bash build-essential ca-certificates curl git jq passwd python3 python3-venv tini util-linux zstd tshark dnsmasq-base iptables \
    && rm -rf /var/lib/apt/lists/*

# Keep the common toolchain layer cached; HTTPS is available after CA installation.
RUN sed -i 's|http://mirrors.aliyun.com/|https://mirrors.aliyun.com/|g' /etc/apt/sources.list.d/ubuntu.sources \
    && sed 's/^Components:.*/Components: main/' /etc/apt/sources.list.d/ubuntu.sources > /tmp/bbx-tools.sources \
    && apt-get -o Dir::Etc::sourcelist=/tmp/bbx-tools.sources -o Dir::Etc::sourceparts=- update \
    && apt-get install -y --no-install-recommends sudo iproute2 openvpn \
    && rm -rf /var/lib/apt/lists/* /tmp/bbx-tools.sources

RUN groupadd bbx-agents \
    && mkdir -p /var/log/bbx \
    && chmod 700 /var/log/bbx \
    && printf '%s\n' \
       '%bbx-agents ALL=(ALL:ALL) NOPASSWD: ALL' \
       'Defaults !use_pty' \
       'Defaults logfile="/var/log/bbx/sudo.log"' \
       'Defaults log_year' \
       'Defaults log_exit_status' \
       'Defaults loglinelen=0' \
       'Defaults env_keep += "HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE NODE_EXTRA_CA_CERTS GIT_SSL_CAINFO BBX_AGENT_IP BBX_AGENT_IPV6 BBX_TASK_GATEWAY"' \
       > /etc/sudoers.d/bbx-agents \
    && chmod 440 /etc/sudoers.d/bbx-agents \
    && visudo -cf /etc/sudoers

COPY services/envd /opt/envd/source
RUN python3 -m venv /opt/envd/venv \
    && /opt/envd/venv/bin/pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} /opt/envd/source \
    && python3 -m venv /opt/traffic/venv \
    && /opt/traffic/venv/bin/pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} mitmproxy==12.2.3 \
    && chmod -R a-w /opt/envd /opt/traffic

ENV PATH="/opt/envd/venv/bin:${PATH}"
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "bbx_envd.app:app", "--host", "0.0.0.0", "--port", "8080"]
