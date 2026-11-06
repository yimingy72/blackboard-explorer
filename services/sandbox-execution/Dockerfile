ARG TOOL_IMAGE=bbx-exec-env-kali@sha256:c79ded7b50e9a7ea2433e9740c48417ce08601948196ec497ad403c779da58ae
FROM opensandbox/execd@sha256:c75836c46ef4302afccec182d5d7339eb2f905f51e3d73b7aa17bb76c832526c AS official
FROM ${TOOL_IMAGE} AS launcher-build
USER root
COPY launcher/launcher.c launcher/assignment_scope.c launcher/assignment_scope.h /tmp/
RUN gcc -std=c11 -O2 -Wall -Wextra -Werror -static /tmp/launcher.c /tmp/assignment_scope.c -o /tmp/launcher \
    && ! readelf -l /tmp/launcher | grep -q INTERP

FROM ${TOOL_IMAGE}
USER root
COPY --from=official /execd /opt/bbx/control/execd
COPY --from=launcher-build /tmp/launcher /opt/bbx/control/launcher
COPY --chmod=0555 launcher/kubernetes-setup.sh /opt/bbx/control/kubernetes-setup.sh
COPY src/bbx_sandbox_execution /opt/bbx/control/python/bbx_sandbox_execution
RUN groupadd --gid 10000 bbx-solver \
    && useradd --uid 10000 --gid 10000 --no-create-home --home-dir /workspace --shell /bin/bash bbx-solver \
    && install -d -o root -g root -m 0700 /var/lib/bbx/control /var/lib/bbx/control/launches \
    && install -d -o 10000 -g 10000 -m 0755 /workspace /workspace/.tmp \
    && chmod 0755 /opt/bbx /opt/bbx/control /opt/bbx/control/execd /opt/bbx/control/launcher \
    && chmod -R a-w /opt/bbx/control
WORKDIR /opt/bbx/control
ENTRYPOINT ["/opt/bbx/control/launcher", "--execd"]
