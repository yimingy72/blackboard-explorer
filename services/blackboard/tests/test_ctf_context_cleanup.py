"""Operational errors must not export database parameters or credentials."""

from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bbx_blackboard import ctf_context_cleanup
from sqlalchemy.exc import DBAPIError


async def test_cleanup_cli_masks_database_error_and_disposes_engine(monkeypatch, capsys):
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(
        ctf_context_cleanup, "Settings", lambda: SimpleNamespace(database_url="synthetic-url")
    )
    monkeypatch.setattr(ctf_context_cleanup, "create_async_engine", lambda *a, **k: engine)
    monkeypatch.setattr(
        ctf_context_cleanup,
        "cleanup",
        AsyncMock(
            side_effect=DBAPIError(
                "synthetic query", {"private": "synthetic-private-parameter"}, RuntimeError("fail")
            )
        ),
    )
    assert await ctf_context_cleanup.run(Namespace(apply=True, task_id=[])) == 1
    result = capsys.readouterr()
    assert "DBAPIError" in result.err
    assert "synthetic-private-parameter" not in result.err + result.out
    engine.dispose.assert_awaited_once()
