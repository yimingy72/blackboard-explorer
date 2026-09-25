"""Run a fixed-profile evaluation batch against an existing blackboard."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml
from bbx_contracts.profile import load_profile
from bbx_runtime.clients import BlackboardClient

ROOT = Path(__file__).resolve().parents[2]
TERMINAL = {"finished", "failed", "stopped"}
Sleep = Callable[[float], Awaitable[None]]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _error(exc: BaseException, token: str) -> str:
    message = str(exc)
    secrets = {token}
    secrets.update(
        value
        for name, value in os.environ.items()
        if any(part in name.upper() for part in ("TOKEN", "PASSWORD", "SECRET", "API_KEY"))
    )
    for secret in sorted(secrets - {""}, key=len, reverse=True):
        message = message.replace(secret, "[REDACTED]")
    return f"{type(exc).__name__}: {message}"


async def _download(
    http: httpx.AsyncClient,
    base_url: str,
    token: str,
    path: str,
    destination: Path,
    *,
    params: dict[str, str] | None = None,
) -> None:
    try:
        async with http.stream(
            "GET",
            f"{base_url}/api{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=120,
        ) as response:
            response.raise_for_status()
            with destination.open("xb") as output:
                async for chunk in response.aiter_bytes():
                    output.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


async def _stop_owned(board: BlackboardClient, task_id: str) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(board.stop_task(task_id)), timeout=15)
    except (Exception, asyncio.CancelledError):
        pass


async def _settle(
    board: BlackboardClient,
    task_id: str,
    timeout: float,
    poll_interval: float,
    sleep: Sleep,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            task = (await board.state(task_id))["task"]
        except Exception:
            return
        if task["status"] in TERMINAL and task.get("workspace_uri"):
            return
        await sleep(min(poll_interval, max(0, deadline - time.monotonic())))


def _evidence_uris(state: dict[str, Any], events: list[dict[str, Any]]) -> list[str]:
    uris: set[str] = set()
    for fact in state.get("facts", {}).values():
        for evidence in fact.get("evidence", []):
            if isinstance(evidence, dict) and isinstance(evidence.get("uri"), str):
                uris.add(evidence["uri"])
    for event in events:
        if event.get("type") == "tool_call.recorded":
            uri = event.get("payload", {}).get("result_uri")
            if isinstance(uri, str):
                uris.add(uri)
    return sorted(uris)


async def _export(
    board: BlackboardClient,
    http: httpx.AsyncClient,
    base_url: str,
    token: str,
    task_id: str,
    directory: Path,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    try:
        state = await board.state(task_id)
    except Exception as exc:
        state = {}
        errors.append(f"state: {_error(exc, token)}")
    try:
        events = await board.events(task_id)
    except Exception as exc:
        events = []
        errors.append(f"events: {_error(exc, token)}")
    _write_json(directory / "state.json", state)
    _write_json(directory / "events.json", events)

    task = state.get("task", {})
    (directory / "report.md").write_text("", encoding="utf-8")
    if task.get("report_uri"):
        try:
            await _download(
                http,
                base_url,
                token,
                f"/tasks/{task_id}/report",
                directory / "report.download",
            )
            (directory / "report.download").replace(directory / "report.md")
        except Exception as exc:
            errors.append(f"report: {_error(exc, token)}")
    if task.get("workspace_uri"):
        try:
            await _download(
                http,
                base_url,
                token,
                f"/tasks/{task_id}/workspace",
                directory / "workspace.tar.zst",
            )
        except Exception as exc:
            errors.append(f"workspace: {_error(exc, token)}")

    evidence_dir = directory / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    mapping: dict[str, str] = {}
    for uri in _evidence_uris(state, events):
        filename = hashlib.sha256(uri.encode()).hexdigest() + ".bin"
        relative = f"evidence/{filename}"
        try:
            await _download(
                http,
                base_url,
                token,
                "/evidence",
                directory / relative,
                params={"uri": uri},
            )
            mapping[uri] = relative
        except Exception as exc:
            errors.append(f"evidence {uri}: {_error(exc, token)}")
    _write_json(evidence_dir / "index.json", mapping)
    return state, errors


async def _run_one(
    board: BlackboardClient,
    http: httpx.AsyncClient,
    base_url: str,
    token: str,
    request: dict[str, Any],
    profile: dict[str, Any],
    profile_name: str,
    version: int,
    directory: Path,
    timeout: float,
    settle_timeout: float,
    poll_interval: float,
    sleep: Sleep,
) -> dict[str, Any]:
    run_started: float | None = None
    elapsed_seconds: float | None = None
    task_id: str | None = None
    outcome = "error"
    problem: str | None = None
    _write_json(directory / "state.json", {})
    _write_json(directory / "events.json", [])
    (directory / "report.md").write_text("", encoding="utf-8")
    evidence_dir = directory / "evidence"
    evidence_dir.mkdir()
    _write_json(evidence_dir / "index.json", {})
    try:
        created = await board.create_task(request)
        task_id = str(created["id"])
        _write_json(
            directory / "run.json",
            {
                "task_id": task_id,
                "profile_name": profile_name,
                "profile_version": version,
                "profile": profile,
                "budget": request["budget"],
                "status": "created",
                "outcome": "running",
                "elapsed_seconds": 0,
                "export_seconds": 0,
            },
        )
        run_started = time.monotonic()
        await board.start_task(task_id)
        deadline = time.monotonic() + timeout
        while True:
            state = await board.state(task_id)
            task = state["task"]
            status = task["status"]
            if status in TERMINAL and elapsed_seconds is None:
                elapsed_seconds = time.monotonic() - run_started
            if status in TERMINAL and (status != "finished" or task.get("workspace_uri")):
                outcome = "success" if status == "finished" else status
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Task did not finish before the evaluation deadline")
            await sleep(min(poll_interval, remaining))
    except asyncio.CancelledError:
        if task_id is not None:
            await _stop_owned(board, task_id)
        _write_json(
            directory / "run.json",
            {
                "task_id": task_id,
                "profile_name": profile_name,
                "profile_version": version,
                "profile": profile,
                "budget": request["budget"],
                "status": "interrupted",
                "outcome": "interrupted",
                "elapsed_seconds": elapsed_seconds
                if elapsed_seconds is not None
                else (time.monotonic() - run_started if run_started is not None else 0),
                "export_seconds": 0,
            },
        )
        raise
    except TimeoutError as exc:
        if elapsed_seconds is None:
            elapsed_seconds = time.monotonic() - run_started if run_started is not None else 0
        outcome = "timeout"
        problem = _error(exc, token)
        if task_id is not None:
            await _stop_owned(board, task_id)
            await _settle(board, task_id, settle_timeout, poll_interval, sleep)
    except Exception as exc:
        if elapsed_seconds is None:
            elapsed_seconds = time.monotonic() - run_started if run_started is not None else 0
        problem = _error(exc, token)
        if task_id is not None:
            await _stop_owned(board, task_id)
            await _settle(board, task_id, settle_timeout, poll_interval, sleep)

    state: dict[str, Any] = {}
    export_errors: list[str] = []
    export_started = time.monotonic()
    if task_id is not None:
        state, export_errors = await _export(board, http, base_url, token, task_id, directory)
    export_seconds = time.monotonic() - export_started
    task = state.get("task", {})
    status = task.get("status", "not_created")
    if outcome == "success" and (
        export_errors or not task.get("report_uri") or not task.get("workspace_uri")
    ):
        outcome = "incomplete"
    result = {
        "task_id": task_id,
        "profile_name": profile_name,
        "profile_version": version,
        "profile": profile,
        "budget": request["budget"],
        "status": status,
        "outcome": outcome,
        "elapsed_seconds": elapsed_seconds if elapsed_seconds is not None else 0,
        "export_seconds": export_seconds,
        "error": problem,
        "export_errors": export_errors,
    }
    _write_json(directory / "run.json", result)
    return result


async def run_batch(
    *,
    task: str,
    profile: str,
    n: int,
    out: Path,
    base_url: str,
    token: str,
    exec_image: str | None = None,
    timeout: float | None = None,
    settle_timeout: float | None = None,
    poll_interval: float = 2,
    http_client: httpx.AsyncClient | None = None,
    sleep: Sleep = asyncio.sleep,
    root: Path = ROOT,
) -> list[dict[str, Any]]:
    if task != "mini-shop-review" or profile not in {"default", "single"}:
        raise ValueError("Unsupported task or profile")
    if (
        n < 1
        or not token
        or poll_interval <= 0
        or (timeout is not None and timeout <= 0)
        or (settle_timeout is not None and settle_timeout <= 0)
    ):
        raise ValueError("Invalid batch settings")
    if exec_image is not None and not exec_image.strip():
        raise ValueError("Execution image cannot be empty")
    spec = yaml.safe_load((root / "eval/tasks" / task / "task.yaml").read_text(encoding="utf-8"))
    fixed, _ = load_profile(root / "profiles" / profile)
    content = fixed.model_dump(mode="json")
    if exec_image is not None:
        content["exec_image"] = exec_image
    request = {**spec, "agent_profile": profile}
    request["budget"] = {**spec["budget"]}
    if profile == "single":
        request["budget"]["max_concurrent_agents"] = 1
    out.mkdir(parents=True, exist_ok=False)
    own_http = http_client is None
    http = http_client or httpx.AsyncClient(trust_env=False, timeout=30)
    base_url = base_url.rstrip("/")
    try:
        response = await http.post(
            f"{base_url}/api/profiles/{quote(profile, safe='')}/versions",
            json=content,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        published = response.json()
        version = int(published["version"])
        pinned = published["profile"]
        request["profile_version"] = version
        duration = timeout or (
            int(request["budget"]["max_minutes"]) * 60
            + int(pinned["params"].get("grace_timeout", 5)) * 60
            + 120
        )
        settle = settle_timeout or (int(pinned["params"].get("grace_timeout", 5)) * 60 + 120)
        board = BlackboardClient(base_url, token, http)
        results = []
        for number in range(1, n + 1):
            directory = out / f"run-{number:03d}"
            directory.mkdir()
            results.append(
                await _run_one(
                    board,
                    http,
                    base_url,
                    token,
                    request,
                    pinned,
                    profile,
                    version,
                    directory,
                    duration,
                    settle,
                    poll_interval,
                    sleep,
                )
            )
        return results
    finally:
        if own_http:
            await http.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a fixed-profile evaluation batch")
    parser.add_argument("--task", choices=["mini-shop-review"], required=True)
    parser.add_argument("--profile", choices=["default", "single"], required=True)
    parser.add_argument("--n", type=int, default=5)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--base-url", default=os.environ.get("BLACKBOARD_URL", "http://127.0.0.1:58000")
    )
    parser.add_argument("--exec-image")
    parser.add_argument("--timeout", type=float, help="Per-task wall-clock deadline in seconds")
    parser.add_argument("--settle-timeout", type=float, help="Wait after stop for finalization")
    args = parser.parse_args(argv)
    token = os.environ.get("SERVICE_TOKEN")
    if not token:
        parser.error("SERVICE_TOKEN is required")
    try:
        results = asyncio.run(
            run_batch(
                task=args.task,
                profile=args.profile,
                n=args.n,
                out=args.out,
                base_url=args.base_url,
                token=token,
                exec_image=args.exec_image,
                timeout=args.timeout,
                settle_timeout=args.settle_timeout,
            )
        )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(_error(exc, token))
        return 1
    for result in results:
        print(f"{result['task_id']}: {result['outcome']} ({result['elapsed_seconds']:.1f}s)")
    return 0 if all(result["outcome"] == "success" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
