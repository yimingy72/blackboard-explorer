FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
WORKDIR /opt/bbx-sandbox-host
COPY requirements.txt /tmp/bbx-sidecar-requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/bbx-sidecar-requirements.txt \
    && rm /tmp/bbx-sidecar-requirements.txt
COPY source/ ./
ENV PYTHONDONTWRITEBYTECODE=1
USER 1001:1001
EXPOSE 8766
ENTRYPOINT ["python", "-m", "bbx_runtime.sandbox_mcp"]
