FROM node:22.21.1-bookworm-slim@sha256:25b3eb23a00590b7499f2a2ce939322727fcce1b15fdd69754fcd09536a3ae2c
WORKDIR /opt/bbx-runner
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund
COPY src ./src
COPY protocol.schema.json ./
USER 1000:1000
ENV PI_OFFLINE=1
ENTRYPOINT ["node", "/opt/bbx-runner/src/bootstrap.mjs"]
