ARG SANDBOX_BASE
FROM ${SANDBOX_BASE} AS launcher-build
USER root
COPY launcher/launcher.c launcher/assignment_scope.c launcher/assignment_scope.h /tmp/
RUN gcc -std=c11 -O2 -Wall -Wextra -Werror -static /tmp/launcher.c /tmp/assignment_scope.c -o /tmp/launcher

FROM ${SANDBOX_BASE}
USER root
COPY --from=launcher-build /tmp/launcher /opt/bbx/control/launcher
COPY --chmod=0555 launcher/kubernetes-setup.sh /opt/bbx/control/kubernetes-setup.sh
COPY src/bbx_sandbox_execution /opt/bbx/control/python/bbx_sandbox_execution
RUN rm -f /opt/bbx/control/assignment-network-v1 /opt/bbx/control/task-vpn-v1 \
        /opt/bbx/control/task-network-choice-v1 /opt/bbx/control/assignment-network.py \
        /opt/bbx/control/task-vpn.py \
    && install -m 0444 /dev/null /opt/bbx/control/ctf-direct-v1
