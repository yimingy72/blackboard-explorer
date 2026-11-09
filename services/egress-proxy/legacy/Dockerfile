FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852

ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://archive.ubuntu.com/ubuntu/|$APT_MIRROR/|g" \
            /etc/apt/sources.list.d/ubuntu.sources; \
    fi \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       iproute2 iptables python3 openvpn=2.6.19-0ubuntu0.24.04.5 \
       wireguard-tools=1.0.20210914-1ubuntu4 redsocks=0.5-2build4 \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=0444 gateway.py /opt/bbx/gateway.py
RUN chmod 0755 /opt/bbx
ENTRYPOINT ["/usr/bin/python3", "-I", "-S", "/opt/bbx/gateway.py"]
