"""Connect a trusted, already-bound data-plane endpoint with official SDK adapters."""

from opensandbox import Sandbox
from opensandbox.adapters.factory import AdapterFactory
from opensandbox.config import ConnectionConfig
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
