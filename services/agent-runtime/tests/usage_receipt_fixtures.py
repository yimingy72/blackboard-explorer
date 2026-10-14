"""Synthetic acknowledgments for fake services that implement ordinary heartbeats."""

from bbx_contracts.heartbeat import heartbeat_digest


def heartbeat_ack(task_id, agent_id, body):
    if body.get("request_id") is None:
        return {}
    return {
        "request_id": str(body["request_id"]),
        "receipt_digest": heartbeat_digest(str(task_id), agent_id, body),
        "confirmed": True,
        "usage_known": body.get("usage_known", True),
    }
