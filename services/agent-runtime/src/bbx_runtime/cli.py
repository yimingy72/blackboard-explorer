"""Explicit commands for running and inspecting a single registered agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from uuid import UUID

from bbx_runtime.clients import BlackboardClient, object_store
from bbx_runtime.execenv import ExecEnvManager
from bbx_runtime.models import load_runtime_profile
from bbx_runtime.runner import AgentRunner
from bbx_runtime.settings import ControlSettings, Settings


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="bbx-runtime")
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run-agent", help="运行一个 Agent（调用真实模型）")
    run.add_argument("--task", type=UUID, required=True)
    run.add_argument("--type", choices=["explore", "derive", "close"], required=True)
    start = run.add_mutually_exclusive_group()
    start.add_argument("--intent")
    start.add_argument("--seed", action="store_true")
    run.add_argument("--mode", choices=["judge", "final"])
    conclude = commands.add_parser("conclude", help="请求 Agent 结束并交接")
    conclude.add_argument("--task", type=UUID, required=True)
    conclude.add_argument("--agent", required=True)
    state = commands.add_parser("state", help="读取任务当前黑板")
    state.add_argument("--task", type=UUID, required=True)
    return root


async def run_command(args: argparse.Namespace, settings: ControlSettings) -> int:
    task_id = str(args.task)
    async with BlackboardClient(
        settings.blackboard_url, settings.service_token.get_secret_value()
    ) as service:
        if args.command == "state":
            print(json.dumps(await service.state(task_id), ensure_ascii=False))
            return 0
        if args.command == "conclude":
            await service.conclude(task_id, args.agent, "manual")
            print("已请求结束，请等待交接回执。")
            return 0
        if args.type != "explore" and (args.seed or args.intent):
            raise ValueError("Only explore accepts seed or intent")
        if args.type != "close" and args.mode:
            raise ValueError("Only close accepts mode")
        if args.type == "explore" and not (args.seed or args.intent):
            raise ValueError("Explore requires seed or intent")
        if not isinstance(settings, Settings):
            raise ValueError("Running an agent requires full runtime settings")
        mode = (args.mode or "judge") if args.type == "close" else None
        state = await service.state(task_id)
        task = state["task"]
        if task["status"] in {"finished", "failed", "stopped"}:
            raise ValueError("Task has already ended")
        if args.seed and not state["board_empty"]:
            raise ValueError("A seed requires an empty board")
        if mode == "final" and task["status"] != "closing":
            raise ValueError("Final close requires a closing task")
        if task["status"] == "closing" and mode != "final":
            raise ValueError("Only a final close agent may start during closing")
        objects = object_store(settings)
        manager = ExecEnvManager(settings, objects=objects)
        try:
            handle = None
            if args.type == "explore":
                profile = load_runtime_profile(
                    await service.get_profile(task["agent_profile"], task["agent_profile_version"])
                )
                if task["status"] == "created":
                    await service.start_task(task_id)
                handle = await manager.provision(task_id, profile)
                if task["status"] in {"created", "provisioning"}:
                    await service.transition(task_id, "running")
            registered = await service.register_agent(
                task_id, args.type, is_seed=args.seed, close_mode=mode
            )
            agent_id = registered["agent_id"]
            print(json.dumps({"task_id": task_id, "agent_id": agent_id}), flush=True)
            try:
                if args.intent:
                    await service.claim_for(task_id, args.intent, agent_id)
                if handle is not None:
                    await manager.create_user(handle, agent_id)
            except Exception:
                await service.finish_agent(
                    task_id,
                    agent_id,
                    {"accepted": False, "reason": "Agent 启动准备失败"},
                    "runtime_error",
                )
                raise
            result = await AgentRunner(settings, service, objects, manager).run_agent(
                task_id,
                agent_id,
                args.type,
                args.intent,
                mode,
                agent_token=registered["token"],
                handle=handle,
            )
            print(json.dumps({"agent_id": agent_id, **result.__dict__}, ensure_ascii=False))
            return 1 if result.end_reason == "runtime_error" else 0
        finally:
            manager.docker.close()


def main() -> None:
    args = parser().parse_args()
    try:
        settings = Settings() if args.command == "run-agent" else ControlSettings()  # type: ignore[call-arg]
        code = asyncio.run(run_command(args, settings))
    except KeyboardInterrupt:
        code = 130
    except Exception as error:
        print(
            f"命令失败：{type(error).__name__}；请检查参数、服务和进程环境配置。", file=sys.stderr
        )
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
