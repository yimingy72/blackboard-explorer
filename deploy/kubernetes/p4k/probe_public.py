"""One public native task with a ceiling binary attachment and an empty file."""

import argparse
import asyncio
import hashlib
import json
import runpy
import shlex
from pathlib import Path

from common import ROOT, STATE, kubectl

previous = runpy.run_path(str(ROOT / "deploy/kubernetes/p4j/probe_public.py"))
PROFILE = "shared-inputs-v1"
FILE_LIMIT = 16 * 1024**2


class SharedInputsProbe(previous["PublicCTFProbe"]):
    prefix = "p4k"
    publication_id = "p4k-isolated-shared-inputs"

    def __init__(self, destination):
        super().__init__(destination)
        self.result_directory = "joint-inputs"
        self.publication_label = "Isolated Kali shared initial attachments fixture"
        self.files = []
        self.first_model = False

    def publication_options(self):
        return {"inputs_profile": PROFILE}

    async def group_inputs(self, http, origin, group_id, capability):
        assert capability["shared_inputs"] == {
            "profile": PROFILE,
            "max_file_bytes": FILE_LIMIT,
            "max_total_bytes": 24 * 1024**2,
            "max_files": 20,
        }
        endpoint = origin + f"/api/task-input-groups/{group_id}/files"
        payloads = [
            ("binary ceiling '测'.bin", bytes(range(256)) * (FILE_LIMIT // 256)),
            ("empty attachment.txt", b""),
        ]
        for name, data in payloads:
            response = await http.post(
                endpoint,
                params={"filename": name},
                headers={**self.user, "Content-Type": "application/octet-stream"},
                content=data,
                timeout=90,
            )
            assert response.status_code in {200, 201}, response.status_code
            item = response.json()
            assert item["filename"] == name and item["size"] == len(data)
            assert item["sha256"] == hashlib.sha256(data).hexdigest()
            assert item["path"] == f"/workspace/shared/inputs/{item['id']}/{name}"
            self.files.append({key: item[key] for key in ("id", "path", "size", "sha256")})
        ids = [item["id"] for item in self.files]
        return {"input_file_ids": ids, "public_input_ids": ids}

    async def public_create(self, app, *args):
        self.app = app
        self.task_id = await super().public_create(app, *args)
        return self.task_id

    async def tool(self, body, names):
        assert "exec_command" in names and "bash" not in names
        prior = previous["tool_results"](body["messages"])
        if "exec_command" in prior:
            return None
        assert not self.first_model
        self.first_model = True
        record = await self.app.state.native_runner_bootstrap.view(self.task_id)
        receipt = record["data"]["inputs_receipt"]
        scope = receipt["scope"]
        assert record["data"]["inputs_profile"] == PROFILE and receipt["result"] == "materialized"
        assert scope["workspace_pvc_uid"] == record["observation"]["workspace_pvc"]["uid"]
        assert scope["sandbox_pod_uid"] == record["observation"]["sandbox"]["pod_uid"]
        assert scope["sandbox_container_id"] == record["observation"]["sandbox"]["container_id"]
        (STATE / self.result_directory / "before-first-model.json").write_text(
            json.dumps(
                {
                    "task_id": str(self.task_id),
                    "before_first_model": True,
                    "workspace_pvc_uid": scope["workspace_pvc_uid"],
                    "sandbox_pod_uid": scope["sandbox_pod_uid"],
                    "manifest_sha256": scope["manifest_sha256"],
                },
                indent=2,
            )
            + "\n"
        )
        address = self.destination["ipv4"]
        port = previous["PORT"]
        code = f"""
from pathlib import Path
import hashlib,json,os,socket
for item in json.loads({json.dumps(self.files, ensure_ascii=False)!r}):
    p=Path(item["path"])
    st=p.stat()
    assert st.st_size == item["size"] and st.st_uid == st.st_gid == 10000
    assert st.st_mode & 0o777 == 0o600
    with p.open("rb") as source:
        assert hashlib.file_digest(source,"sha256").hexdigest() == item["sha256"]
with socket.create_connection(({address!r},{port}),2) as s:
    s.sendall(b'p4k'); assert s.makefile('rb').read(3)==b'p4k'
assert os.getuid() == 10000
print("p4k-initial-inputs-ok 16777216 0 10000")
"""
        return "exec_command", {
            "command": "python3 -c " + shlex.quote(code),
            "cwd": "/workspace",
            "timeout_seconds": 20,
        }

    async def verify_traffic(self, tid, app, engine, wait, evidence):
        from bbx_blackboard.store import schema as tables
        from sqlalchemy import select

        async def executed():
            async with engine.connect() as conn:
                rows = (
                    (
                        await conn.execute(
                            select(tables.sandbox_command_records).where(
                                tables.sandbox_command_records.c.task_id == tid
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            if not rows or rows[0]["state"] != "completed":
                return None
            assert len(rows) == 1 and rows[0]["exit_code"] == 0
            return rows[0]

        evidence["traffic_stage"] = "ready original work PVC and first Lead input checksum"
        row = await wait(executed, 660)
        assert self.first_model and row["role"] == "lead"
        assert row["data"]["stdout"].strip() == "p4k-initial-inputs-ok 16777216 0 10000"
        record = await app.state.native_runner_bootstrap.view(tid)
        original = record["observation"]["sandbox"]
        receipt = record["data"]["inputs_receipt"]
        before = json.loads((STATE / self.result_directory / "before-first-model.json").read_text())
        assert receipt["scope"]["manifest_sha256"] == before["manifest_sha256"]
        image = json.loads((STATE / "input-image/receipt.json").read_text())
        pod = json.loads(
            kubectl("-n", record["namespace"], "get", "pod", original["pod_name"], "-o", "json")
        )
        assert pod["metadata"]["uid"] == original["pod_uid"] == before["sandbox_pod_uid"]
        (status,) = [c for c in pod["status"]["containerStatuses"] if c["name"] == "sandbox"]
        assert status["containerID"] == original["container_id"]
        assert status["imageID"].rsplit("@", 1)[-1].rsplit("://", 1)[-1] in {
            image["reference"].rsplit("@", 1)[1],
            image["config_digest"],
        }
        evidence["attachment_proof"] = {
            "before_first_model": before,
            "checked_files": self.files,
            "original_command_id": row["id"],
            "sandbox_image_id": status["imageID"],
        }
        evidence["checks"].update(
            {
                "public_selected_shared_inputs": True,
                "original_workspace_confirmed_before_first_model": True,
                "lead_verified_exact_binary_and_empty_low_uid_files": True,
                "actual_pinned_shared_inputs_kali_image": True,
            }
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--stop-only", action="store_true")
    args = parser.parse_args()
    target = previous["shared"]["target"]
    destination = (
        json.loads((STATE / "traffic-target.json").read_text())
        if args.stop_only
        else target(False, "p4k")
    )
    harness = runpy.run_path(str(ROOT / "deploy/kubernetes/p3h/probe.py"))
    asyncio.run(
        harness["main"](
            args.candidate.resolve(),
            args.stop_only,
            args.stop_only,
            workers=SharedInputsProbe(destination),
        )
    )
