ARG TOOL_IMAGE=bbx-exec-env-kali@sha256:c79ded7b50e9a7ea2433e9740c48417ce08601948196ec497ad403c779da58ae
FROM opensandbox/execd@sha256:c75836c46ef4302afccec182d5d7339eb2f905f51e3d73b7aa17bb76c832526c AS official
FROM ${TOOL_IMAGE} AS launcher-build
USER root
COPY launcher/launcher.c launcher/assignment_scope.c launcher/assignment_scope.h /tmp/
RUN gcc -std=c11 -O2 -Wall -Wextra -Werror -static /tmp/launcher.c /tmp/assignment_scope.c -o /tmp/launcher \
    && ! readelf -l /tmp/launcher | grep -q INTERP

FROM ${TOOL_IMAGE}
USER root
ARG ASSIGNMENT_NETWORK_V1=0
ARG TASK_VPN_V1=0
ARG TASK_NETWORK_CHOICE_V1=0
COPY --from=official /execd /opt/bbx/control/execd
COPY --from=launcher-build /tmp/launcher /opt/bbx/control/launcher
COPY --chmod=0555 launcher/kubernetes-setup.sh launcher/assignment-network.py launcher/materialize-inputs.py launcher/task-vpn.py /opt/bbx/control/
RUN case "$ASSIGNMENT_NETWORK_V1" in 0) ;; 1) install -m 0444 /dev/null /opt/bbx/control/assignment-network-v1 ;; *) exit 1 ;; esac
RUN case "$TASK_VPN_V1" in \
    0) ;; \
    1) curl --proto '=https' -fsS -o /tmp/bbx-wg.deb \
        'https://kali.download/kali/pool/main/w/wireguard/wireguard-tools_1.0.20260223-2_amd64.deb' \
       && echo '83cc339b28f266944b8d0df7a44312cedf5a872143a37d783f05b89d1a1aade0  /tmp/bbx-wg.deb' \
          | sha256sum -c - \
       && dpkg -i /tmp/bbx-wg.deb \
       && rm /tmp/bbx-wg.deb \
       && install -m 0444 /dev/null /opt/bbx/control/task-vpn-v1 ;; \
    *) exit 1 ;; \
    esac
RUN case "$TASK_NETWORK_CHOICE_V1" in \
    0) ;; \
    1) test "$TASK_VPN_V1" = 1 && test "$ASSIGNMENT_NETWORK_V1" = 1 \
       && install -m 0444 /dev/null /opt/bbx/control/task-network-choice-v1 ;; \
    *) exit 1 ;; \
    esac

COPY src/bbx_sandbox_execution /opt/bbx/control/python/bbx_sandbox_execution
RUN groupadd --gid 10000 bbx-solver \
    && useradd --uid 10000 --gid 10000 --no-create-home --home-dir /workspace --shell /bin/bash bbx-solver \
    && install -d -o root -g root -m 0700 /var/lib/bbx/control /var/lib/bbx/control/launches \
    && install -d -o 10000 -g 10000 -m 0755 /workspace /workspace/.tmp \
    && chmod 0755 /opt/bbx /opt/bbx/control /opt/bbx/control/execd /opt/bbx/control/launcher \
    && chmod -R a-w /opt/bbx/control
WORKDIR /opt/bbx/control
ENTRYPOINT ["/opt/bbx/control/launcher", "--execd"]
