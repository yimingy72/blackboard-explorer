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
COPY worker-tools.txt /opt/ctf-tools/worker-tools.md
RUN python3 - <<'PY'
import hashlib
import json
import re
import shutil
from pathlib import Path

manifest_path = Path("/opt/ctf-tools/manifest.json")
summary_path = Path("/opt/ctf-tools/worker-tools.md")
manifest = json.loads(manifest_path.read_text())
summary = summary_path.read_text()
manifest["purpose"] = "single-container-ctf-runner"
manifest["special_paths"].update({
    "ctf_runner_entrypoint": "/opt/ctf-runner/src/main.mjs",
    "worker_tools_summary": "/opt/ctf-tools/worker-tools.md",
})
manifest["capabilities"]["competition_harness"] = {
    "status": "available",
    "scope": "Single-container Lead/Worker runner, board, Pi sessions, local tools, and competition API adapter",
}
manifest["usage_notes"] = [
    note for note in manifest.get("usage_notes", [])
    if note != "No competition Agent harness or challenge answers are included."
] + [
    "The standalone image includes the Lead/Worker runner, blackboard, Pi sessions, local tools, and competition API adapter.",
    "No challenge answers are included.",
]
commands = set(manifest["commands"])
commands.update(
    name
    for name in re.findall(
        r"(?<![A-Za-z0-9+_.-])([A-Za-z0-9][A-Za-z0-9+_.-]*)(?![A-Za-z0-9+_.-])",
        summary,
    )
    if shutil.which(name)
)
required = {
    "file", "strings", "readelf", "objdump", "ldd", "strace", "ltrace", "rg",
    "tar", "unzip", "zip", "base64", "diff", "patch", "timeout",
}
missing = sorted(name for name in required if not shutil.which(name))
if missing:
    raise SystemExit(f"Required runtime tools are missing: {', '.join(missing)}")
required_paths = {
    "/usr/bin/python3",
    "/usr/local/bin/node",
    "/usr/local/bin/sage",
    "/usr/local/bin/sage-python",
    "/opt/sage/bin/python",
    "/usr/share/ghidra/support/analyzeHeadless",
    "/usr/share/john/password.lst",
    "/opt/ctf-runner/src/main.mjs",
    "/opt/ctf-tools/worker-tools.md",
}
missing_paths = sorted(path for path in required_paths if not Path(path).exists())
if missing_paths:
    raise SystemExit(f"Documented runtime paths are missing: {', '.join(missing_paths)}")
manifest["commands"] = {name: shutil.which(name) for name in sorted(commands | required)}
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
manifest_path.with_suffix(".sha256").write_text(
    f"{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}  {manifest_path.name}\n"
)
PY
RUN mkdir -p /workspace/ctf-run /.data && chown 10000:10000 /workspace/ctf-run /.data
ENV HOME=/workspace \
    NODE_ENV=production
WORKDIR /workspace/ctf-run
USER 10000:10000
ENTRYPOINT ["/usr/local/bin/node", "/opt/ctf-runner/src/main.mjs"]
CMD []
LABEL org.opencontainers.image.title="BBX standalone CTF runner" \
    org.opencontainers.image.description="Single-container CTF Lead, Worker sessions, blackboard and arena adapter"
