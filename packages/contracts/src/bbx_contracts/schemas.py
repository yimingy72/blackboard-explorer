"""Export JSON Schema for every shared model and the receipt union."""

import json
from pathlib import Path

from pydantic import TypeAdapter

from . import models


def export_schemas(output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, obj in vars(models).items():
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
