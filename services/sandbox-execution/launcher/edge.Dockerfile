# Both base arguments are supplied as the trusted immutable images by the release builder.
ARG TASK_VPN_IMAGE
ARG LEGACY_GATEWAY_IMAGE
FROM ${LEGACY_GATEWAY_IMAGE} AS redsocks
FROM ${TASK_VPN_IMAGE}
COPY --from=redsocks /usr/sbin/redsocks /usr/sbin/redsocks
COPY --from=redsocks /lib/x86_64-linux-gnu/libevent_core-2.1.so.7* /usr/lib/x86_64-linux-gnu/
COPY --chmod=0555 task-vpn.py /opt/bbx/control/task-vpn.py
RUN /usr/sbin/redsocks -h >/dev/null 2>&1
