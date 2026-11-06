"""Connect a trusted, already-bound data-plane endpoint with official SDK adapters."""

import json
import re
from datetime import timedelta

from opensandbox import Sandbox
from opensandbox.adapters.factory import AdapterFactory
from opensandbox.config import ConnectionConfig
from opensandbox.models.execd import ExecutionHandlers, ExecutionInit, RunCommandOpts
from opensandbox.models.sandboxes import SandboxEndpoint
from opensandbox.transport import RetryPolicy

ACCESS_HEADER = "X-EXECD-ACCESS-TOKEN"


def connect_bound_execd(sandbox_id: str, endpoint: str, token: str) -> Sandbox:
    if not token.strip():
        raise ValueError("An empty execd access token must never enable compatibility access")
    config = ConnectionConfig(
        disable_metrics=True, retry_policy=RetryPolicy(max_retries=0, wake_retry_enabled=False)
    )
    factory = AdapterFactory(config)
    target = SandboxEndpoint(endpoint=endpoint, headers={ACCESS_HEADER: token})
    return Sandbox(
        sandbox_id=sandbox_id,
        sandbox_service=factory.create_sandbox_service(),
        filesystem_service=factory.create_filesystem_service(target),
        command_service=factory.create_command_service(target),
        health_service=factory.create_health_service(target),
        metrics_service=factory.create_metrics_service(target),
        egress_service=factory.create_network_policy_service(sandbox_id),
        connection_config=config,
    )


async def read_execution_context(sandbox: Sandbox) -> dict[str, str]:
    """Read once during trusted environment registration; callers must freeze the result."""
    observed_id = None

    async def on_init(event: ExecutionInit) -> None:
        nonlocal observed_id
        observed_id = event.id

    result = await sandbox.commands.run(
        ["/opt/bbx/control/launcher", "--assignment-context"],
        opts=RunCommandOpts(working_directory="/opt/bbx/control", timeout=timedelta(seconds=10)),
        handlers=ExecutionHandlers(on_init=on_init),
    )
    if (
        not observed_id
        or result.id != observed_id
        or result.complete is None
        or result.exit_code != 0
        or result.error is not None
    ):
        raise RuntimeError("Trusted execution context terminal evidence is missing")
    context = json.loads("".join(item.text for item in result.logs.stdout))
    for name in ("pod_uid", "environment_revision", "execution_context_id"):
        value = context.get(name)
        expression = r"[0-9a-f]{64}" if name == "execution_context_id" else r"[A-Za-z0-9_-]{1,80}"
        if not isinstance(value, str) or re.fullmatch(expression, value) is None:
            raise ValueError("Invalid trusted execution context")
    return {
        name: context[name] for name in ("pod_uid", "environment_revision", "execution_context_id")
    }
