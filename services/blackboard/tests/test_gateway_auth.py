"""Direct MCP and model scopes reject the retired audience and cross-target tokens."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from bbx_blackboard.gateway_auth import (
    issue_gateway_credentials,
    require_gateway_identity,
    scoped_key,
)
from fastapi import HTTPException
from starlette.requests import Request
from test_ctf_api import settings


def test_runner_capabilities_keep_distinct_target_and_role():
    config = settings()
    tid = uuid4()
    owner = {
        "hard_deadline": datetime.now(UTC) + timedelta(minutes=10),
        "task_id": tid,
        "runner_id": "runner-test",
        "pod_uid": "pod-test",
        "epoch": 1,
        "token_version": "version-test",
    }
    issued = issue_gateway_credentials(
        config, owner, "binding-test", "teammate", targets={"business", "sandbox"}
    )
    app = SimpleNamespace(state=SimpleNamespace(settings=config))

    def verify(token, audience="business-mcp"):
        request = Request({"type": "http", "app": app, "headers": []})
        return require_gateway_identity(request, tid, audience, authorization="Bearer " + token)

    assert verify(issued["business"]["token"]).role == "teammate"
    assert verify(issued["sandbox"]["token"], "sandbox-mcp").role == "teammate"
    assert verify(issued["model"]["token"], "model").role is None
    claims = jwt.decode(issued["business"]["token"], options={"verify_signature": False})
    for changed in ({"iss": "foreign"}, {"aud": "sandbox-mcp"}, {"role": "admin"}, {"aud": "mcp"}):
        token = jwt.encode(
            {**claims, **changed}, scoped_key(config, "business-mcp"), algorithm="HS256"
        )
        with pytest.raises(HTTPException) as denied:
            verify(token)
        assert denied.value.status_code == 401
    with pytest.raises(HTTPException):
        issue_gateway_credentials(config, owner, "binding-test", "helper", targets={"business"})
