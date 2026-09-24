from pathlib import Path

from bbx_blackboard.openapi import schema_text


def test_openapi_snapshot() -> None:
    path = Path(__file__).resolve().parents[1] / "openapi.json"
    assert schema_text() == path.read_text(encoding="utf-8")
