"""Export JSON Schema for every shared model and the receipt union."""

import json
from pathlib import Path

from pydantic import TypeAdapter

from . import (
    challenge_board,
    ctf,
    heartbeat,
    models,
    native_gateway,
    progress,
    runner_bootstrap,
    runner_control,
    sandbox_tools,
    traffic,
)


def export_schemas(output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, obj in {
        **vars(models),
        **vars(challenge_board),
        **vars(ctf),
        **vars(heartbeat),
        **vars(traffic),
        **vars(progress),
        **vars(runner_control),
        **vars(runner_bootstrap),
        **vars(native_gateway),
        **vars(sandbox_tools),
    }.items():
        if (
            isinstance(obj, type)
            and issubclass(obj, models.ContractModel)
            and obj is not models.ContractModel
        ):
            path = output_dir / f"{name}.json"
            path.write_text(
                json.dumps(obj.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            paths.append(path)
    receipt_path = output_dir / "Receipt.json"
    receipt_path.write_text(
        json.dumps(TypeAdapter(models.Receipt).json_schema(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    paths.append(receipt_path)
    return paths


if __name__ == "__main__":
    export_schemas(Path(__file__).resolve().parents[2] / "schemas")
