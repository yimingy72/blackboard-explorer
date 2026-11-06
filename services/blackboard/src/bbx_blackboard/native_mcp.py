"""One Task route on the shared native Virtual MCP gateway."""

import json
from uuid import UUID

from bbx_contracts.challenge_board import business_manifest


def business_mcp_route(task_id, backend_origin, gateway_origin, jwks_file, backend_token_file):
    tid = str(UUID(str(task_id)))
    path = f"/api/tasks/{tid}/mcp"
    return {
        "name": "business-" + tid,
        "gateways": ["business-mcp"],
        "matches": [{"path": {"exact": path}}],
        "policies": {
            "authorization": {
                "rules": [{"require": f'jwt.tid == "{tid}" && jwt.role in ["lead", "teammate"]'}]
            },
            "mcpAuthentication": {
                "issuer": "bbx-runner-gateway-v1",
                "audiences": ["mcp"],
                "jwks": {"file": jwks_file},
                "resourceMetadata": {"resource": gateway_origin.rstrip("/") + path},
            },
            "mcpAuthorization": {
                "rules": [
                    f'jwt.tid == "{tid}" && mcp.tool.target == "business" && jwt.role == '
                    + json.dumps(role)
                    + " && mcp.tool.name in "
                    + json.dumps(names)
                    for role, names in business_manifest()["roles"].items()
                ]
            },
        },
        "backends": [
            {
                "mcp": {
                    "prefixMode": "always",
                    "targets": [
                        {
                            "name": "business",
                            "mcp": {
                                "host": backend_origin.rstrip("/")
                                + f"/api/tasks/{tid}/business-mcp/"
                            },
                            "policies": {
                                "backendAuth": {
                                    "passthrough": {},
                                    "credentials": [
                                        {
                                            "location": {
                                                "header": {"name": "x-bbx-gateway-backend"}
                                            },
                                            "key": {"file": backend_token_file},
                                        }
                                    ],
                                }
                            },
                        }
                    ],
                }
            }
        ],
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Render static routes for the shared native MCP gateway"
    )
    parser.add_argument("--task-id", action="append", required=True)
    parser.add_argument("--backend-origin", required=True)
    parser.add_argument("--gateway-origin", required=True)
    parser.add_argument("--jwks-file", required=True)
    parser.add_argument("--backend-token-file", required=True)
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "gateways": {"business-mcp": {"port": args.port}},
                "routes": [
                    business_mcp_route(
                        tid,
                        args.backend_origin,
                        args.gateway_origin,
                        args.jwks_file,
                        args.backend_token_file,
                    )
                    for tid in dict.fromkeys(args.task_id)
                ],
            },
            indent=2,
        )
    )
