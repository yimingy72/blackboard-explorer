FROM bbx-archv2-sandbox-seam:20261106
USER root
COPY tests/poison.c /tmp/bbx-test-poison.c
RUN chmod u+w /opt/bbx/control \
    && gcc -shared -fPIC -Wall -Wextra -Werror /tmp/bbx-test-poison.c -o /opt/bbx/control/poison.so \
    && chmod a-w /opt/bbx/control /opt/bbx/control/poison.so
COPY tests/container_setup.py tests/raw_probe.py tests/poison.sh /opt/bbx/test/
ENTRYPOINT ["/usr/bin/python3", "-I", "/opt/bbx/test/container_setup.py"]
