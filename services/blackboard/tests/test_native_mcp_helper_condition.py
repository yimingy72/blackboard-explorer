from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from bbx_blackboard.native_mcp import business_mcp_route
from bbx_blackboard.native_mcp_routes import NativeMCPRoutes
from fastapi import HTTPException


def test_helper_route_is_not_ready_without_its_live_business_target_condition():
    route = business_mcp_route(
        uuid4(),
        "https://board.invalid",
        "https://gateway.invalid",
        "/test/jwks.json",
        "/test/token",
        sandbox={"host_url": "http://sandbox.invalid:8766", "inbound_key": "fixture"},
        helper_foreground=True,
    )
    route_name = route["name"]
    route_key = f"gateway/business-mcp/default/{route_name}"
    backend_name = route_key + "/backend0"
    configured = route["backends"][0]["mcp"]["targets"]
    targets, hosts = [], []
    for target in configured:
        url = urlsplit(target["mcp"]["host"])
        name = target["name"]
        targets.append(
            {
                "name": name,
                **({"condition": target["condition"]} if "condition" in target else {}),
                "mcp": {"path": url.path, "backend": {"backend": "/" + name}},
            }
        )
        hosts.append(
            {
                "backend": {
                    "host": {
                        "name": name,
                        "target": f"{url.hostname}:{url.port or 443}",
                    }
                }
            }
        )
    assert targets[0]["condition"] == 'jwt.role in ["lead", "teammate"]'
    dump = {
        "binds": [
            {
                "listeners": {
                    "gateway/business-mcp": {
                        "routes": {
                            route_key: {
                                "key": route_key,
                                "name": route_name,
                                "matches": route["matches"],
                                "backends": [{"weight": 1, "backend": "/" + backend_name}],
                            }
                        }
                    }
                }
            }
        ],
        "backends": [
            *hosts,
            {
                "backend": {
                    "mcp": {
                        "name": backend_name,
                        "target": {"prefixMode": "always", "targets": targets},
                    }
                }
            },
        ],
    }
    record, owned = {"resource_id": route_name}, {route_name: route}
    assert NativeMCPRoutes._active(dump, record, owned) is True
    del targets[0]["condition"]
    with pytest.raises(HTTPException, match="mcp_active_route_conflict"):
        NativeMCPRoutes._active(dump, record, owned)
