FROM opensandbox/server@sha256:25bebdcf1cf7724eb923faecbdbbf82d4038c6ab35c736151574726d14006f30
COPY server/opensandbox_server/config.py /app/opensandbox_server/config.py
COPY server/opensandbox_server/main.py /app/opensandbox_server/main.py
COPY server/opensandbox_server/services/k8s/client.py /app/opensandbox_server/services/k8s/client.py
COPY server/opensandbox_server/tenants/http_provider.py /app/opensandbox_server/tenants/http_provider.py
COPY identity.json /etc/bbx-g7-patch.json
LABEL org.opencontainers.image.version="1.2.0-bbx.g7-direct.1"
