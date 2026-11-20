ARG TOOLS_BASE=bbx-ctf-tools:20261120-r4
FROM ${TOOLS_BASE} AS dependencies
USER root
WORKDIR /opt/ctf-runner
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && npm cache clean --force

FROM ${TOOLS_BASE}
USER root
WORKDIR /opt/ctf-runner
COPY --from=dependencies /opt/ctf-runner/node_modules ./node_modules
COPY package.json package-lock.json ./
COPY src/ ./src/
COPY scripts/run-lock.py ./scripts/run-lock.py
RUN mkdir -p /workspace/ctf-run /.data && chown 10000:10000 /workspace/ctf-run /.data
ENV HOME=/workspace \
    NODE_ENV=production
WORKDIR /workspace/ctf-run
USER 10000:10000
ENTRYPOINT ["/usr/local/bin/node", "/opt/ctf-runner/src/main.mjs"]
CMD []
LABEL org.opencontainers.image.title="BBX standalone CTF runner" \
    org.opencontainers.image.description="Single-container CTF Lead, Worker sessions, blackboard and arena adapter"
