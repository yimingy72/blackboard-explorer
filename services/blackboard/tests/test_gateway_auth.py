"""Fixed MCP issuer and algorithm boundaries remain separate from model capabilities."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from bbx_blackboard.gateway_auth import (
    _key,
    issue_gateway_credentials,
    mcp_public_jwks,
    require_gateway_identity,
)
from fastapi import HTTPException
from mcp_gateway_support import configured_mcp_settings
from pydantic import SecretStr
from starlette.requests import Request
from test_ctf_api import settings


def test_mcp_rsa_scope_rejects_algorithm_issuer_audience_role_and_key_changes():
    config = configured_mcp_settings(settings())
    tid = uuid4()
    owner = {
        "hard_deadline": datetime.now(UTC) + timedelta(minutes=10),
        "task_id": tid,
        "runner_id": "runner-test",
        "pod_uid": "pod-test",
        "epoch": 1,
        "token_version": "version-test",
    }
    issued = issue_gateway_credentials(config, owner, "binding-test", "teammate")
    app = SimpleNamespace(state=SimpleNamespace(settings=config))

    def verify(token, audience="mcp"):
        request = Request({"type": "http", "app": app, "headers": []})
        return require_gateway_identity(request, tid, audience, authorization="Bearer " + token)

    assert verify(issued["mcp"]["token"]).role == "teammate"
    assert verify(issued["model"]["token"], "model").role is None
    public = mcp_public_jwks(config)["keys"][0]
    assert set(public) == {"kty", "key_ops", "n", "e", "kid", "alg", "use"}
    assert public["kty"] == "RSA" and public["alg"] == "RS256"
    claims = jwt.decode(issued["mcp"]["token"], options={"verify_signature": False})
    for changed, algorithm, kid in [
        ({}, "HS256", public["kid"]),
        ({"iss": "foreign-issuer"}, "RS256", public["kid"]),
        ({"aud": "model"}, "RS256", public["kid"]),
        ({"role": "admin"}, "RS256", public["kid"]),
        ({}, "RS256", "foreign-key"),
    ]:
        token = jwt.encode(
            {**claims, **changed},
            _key(config)
            if algorithm == "HS256"
            else config.runner_mcp_signing_key.get_secret_value(),
            algorithm=algorithm,
            headers={"kid": kid},
        )
        with pytest.raises(HTTPException) as denied:
            verify(token)
        assert denied.value.status_code == 401
    with pytest.raises(HTTPException) as unavailable:
        issue_gateway_credentials(
            config.model_copy(update={"runner_mcp_signing_key": SecretStr("")}),
            owner,
            "binding-test",
            "lead",
        )
    assert unavailable.value.status_code == 503
