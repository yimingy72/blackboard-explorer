FROM bbx-exec-env:latest
ARG PIP_INDEX_URL=
COPY eval/targets/mini-shop/src-main/requirements.txt /tmp/eval-requirements.txt
RUN /opt/envd/venv/bin/pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} \
    -r /tmp/eval-requirements.txt && chmod -R a-w /opt/envd
