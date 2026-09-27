"""Own the runtime lock, supervision loop, and graceful process shutdown."""

import asyncio
import signal

from bbx_runtime.chatworker import ChatWorker
from bbx_runtime.clients import BlackboardClient, object_store
from bbx_runtime.execenv import ExecEnvManager
from bbx_runtime.runner import AgentRunner
from bbx_runtime.scheduler.lock import RuntimeLock
from bbx_runtime.scheduler.supervisor import TaskSupervisor
from bbx_runtime.settings import SchedulerSettings


async def serve(settings: SchedulerSettings) -> None:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    manager = None
    try:
        async with (
            RuntimeLock(settings) as instance,
            BlackboardClient(
                settings.blackboard_url, settings.service_token.get_secret_value()
            ) as service,
        ):
            objects = object_store(settings)
            manager = ExecEnvManager(settings, objects=objects)
            runner = AgentRunner(settings, service, objects, manager)
            supervisor = TaskSupervisor(settings, service, manager, runner)
            await service.recover_conversations()
            chat = ChatWorker(settings, service)
            work = asyncio.create_task(supervisor.run(), name="task-supervision")
            chat_work = asyncio.create_task(chat.run(), name="review-conversations")
            shutdown = asyncio.create_task(stopped.wait())
            lost = asyncio.create_task(instance.lost.wait())
            try:
                done, _ = await asyncio.wait(
                    [work, chat_work, shutdown, lost], return_when=asyncio.FIRST_COMPLETED
                )
                if lost in done:
                    raise RuntimeError("Runtime database lock connection was lost")
                if work in done:
                    await work
                if chat_work in done:
                    await chat_work
            finally:
                await chat.stop()
                chat_work.cancel()
                await asyncio.gather(chat_work, return_exceptions=True)
                await supervisor.stop()
                await work
                shutdown.cancel()
                lost.cancel()
                await asyncio.gather(shutdown, lost, return_exceptions=True)
    finally:
        if manager is not None:
            manager.docker.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
