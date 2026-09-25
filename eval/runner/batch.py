"""Run both evaluation profiles on an isolated disposable deployment."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path

import httpx
from bbx_runtime.e2e import ComposeCheckpoint, redact

from .interfaces import changed_interfaces
from .run import run_batch

ROOT = Path(__file__).resolve().parents[2]


async def run(out: Path, n: int, timeout: float | None) -> None:
    out.mkdir(parents=True, exist_ok=False)
    target = ROOT / "eval/targets/dist/mini-shop.tar.gz"
    shutil.copy2(target, out / "target.tar.gz")
    inventory = {"expected_interfaces": changed_interfaces(target)}
    (out / "interfaces.json").write_text(json.dumps(inventory, indent=2))
    checkpoint = ComposeCheckpoint(ROOT, dict(os.environ))
    try:
        await checkpoint.docker("up", "-d")
        address = (await checkpoint.docker("port", "blackboard", "8000")).strip().splitlines()[-1]
        base = f"http://{address}"
        async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
            async with asyncio.timeout(120):
                while True:
                    try:
                        response = await http.get(
                            f"{base}/api/tasks",
                            headers={
                                "Authorization": f"Bearer {checkpoint.environment['SERVICE_TOKEN']}"
                            },
                        )
                        if response.is_success:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(1)
        (out / "batch.json").write_text(
            json.dumps(
                {
                    "n_per_profile": n,
                    "task": "mini-shop-review",
                    "profiles": ["default", "single"],
                    "target_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "project": checkpoint.project,
                },
                indent=2,
            )
        )
        print(f"隔离评估：{base}，每组 {n} 次；结果：{out}", flush=True)
        failed = False
        for profile in ("default", "single"):
            results = await run_batch(
                task="mini-shop-review",
                profile=profile,
                n=n,
                out=out / profile,
                base_url=base,
                token=checkpoint.environment["SERVICE_TOKEN"],
                exec_image="bbx-eval-env:latest",
                timeout=timeout,
            )
            failed |= any(row["outcome"] != "success" for row in results)
            for metadata in (out / profile).rglob("run.json"):
                shutil.copy2(out / "interfaces.json", metadata.parent / "interfaces.json")
        if failed:
            raise RuntimeError(
                "One or more evaluation runs did not complete successfully; see run.json"
            )
    finally:
        try:
            (out / "compose.log").write_text(
                await checkpoint.docker("logs", "--no-color", "--tail", "200")
            )
        finally:
            try:
                await checkpoint.docker("stop", "agent-runtime")
            finally:
                try:
                    for metadata in out.rglob("run.json"):
                        task_id = json.loads(metadata.read_text()).get("task_id")
                        if task_id:
                            checkpoint.task_id = task_id
                            await asyncio.to_thread(checkpoint._remove_task_containers)
                finally:
                    await checkpoint.docker("down", "-v", "--remove-orphans")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="多 Agent/单 Agent 各 n 次隔离评估，会产生模型费用"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--n", type=int, default=5)
    parser.add_argument("--timeout", type=float)
    args = parser.parse_args()
    if args.n < 1 or (args.timeout is not None and args.timeout <= 0):
        parser.error("n and timeout must be positive")
    try:
        asyncio.run(run(args.out, args.n, args.timeout))
    except Exception as error:
        print(redact(f"评估失败：{error}", dict(os.environ)))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
