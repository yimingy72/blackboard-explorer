"""Export and verify the committed HTTP contract without starting services."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import SecretStr

from bbx_blackboard.api import create_app
from bbx_blackboard.settings import Settings


def schema_text() -> str:
    settings = Settings.model_construct(
        postgres_password=SecretStr("placeholder"),
        minio_root_password=SecretStr("placeholder"),
        service_token=SecretStr("placeholder"),
        agent_token_secret=SecretStr("placeholder"),
        admin_users=SecretStr("placeholder"),
    )
    schema = create_app(settings).openapi()
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).resolve().parents[2] / "openapi.json"
    )
    args = parser.parse_args()
    generated = schema_text()
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != generated:
            parser.error(f"OpenAPI snapshot differs: {args.output}")
    else:
        args.output.write_text(generated, encoding="utf-8")


if __name__ == "__main__":
    main()
