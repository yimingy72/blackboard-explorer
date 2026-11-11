"""Run the common controller or inspect a registered Blackboard task."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from uuid import UUID

from bbx_runtime.clients import BlackboardClient
from bbx_runtime.settings import ControlSettings


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="bbx-runtime")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", aliases=["serve-runner-control"], help="运行共同任务控制服务")
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
        else:
            await service.conclude(task_id, args.agent, "manual")
            print("已请求结束，请等待交接回执。")
    return 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    args = parser().parse_args()
    try:
        if args.command in {"serve", "serve-runner-control"}:
            from bbx_runtime.native_runner import serve_native
            from bbx_runtime.settings import NativeRunnerSettings

            asyncio.run(serve_native(NativeRunnerSettings()))  # type: ignore[call-arg]
            code = 0
        else:
            code = asyncio.run(run_command(args, ControlSettings()))  # type: ignore[call-arg]
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
