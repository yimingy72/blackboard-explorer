"""Every event kind the projector accepts must be expressible by the API contract.

Regression guard: ``ctf.coordination.reminded`` was written by the CTF layer and
accepted by ``apply_ctf_view``, but missing from ``CtfEvent.type``. Reading such a
task's event log back (``GET /api/tasks/{id}/events``) then failed response
validation and returned 500 forever.

The check parses the projector source instead of calling it, so it stays valid
without a database and covers branches that only run for specific kinds.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

from bbx_blackboard.store import repository
from bbx_contracts.ctf import CtfEvent
from bbx_contracts.models import EventType


def _projected_kinds() -> set[str]:
    """String literals compared against ``kind`` in the projector module."""
    source = Path(repository.__file__).read_text(encoding="utf-8")
    kinds: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        left, op, right = node.left, node.ops[0], node.comparators[0]
        if not (isinstance(left, ast.Name) and left.id == "kind"):
            continue
        if isinstance(op, ast.Eq) and isinstance(right, ast.Constant):
            if isinstance(right.value, str):
                kinds.add(right.value)
        elif isinstance(op, ast.In) and isinstance(right, (ast.Set, ast.Tuple, ast.List)):
            kinds.update(
                element.value
                for element in right.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
    return kinds


def _serializable_kinds() -> set[str]:
    """Kinds the board response models can render as Event or CtfEvent."""
    ctf = get_args(CtfEvent.model_fields["type"].annotation)
    return {member.value for member in EventType} | set(ctf)


def test_projected_event_kinds_are_serializable() -> None:
    projected = _projected_kinds()
    assert projected, "no event kinds parsed from the projector; the guard itself is broken"
    missing = sorted(projected - _serializable_kinds())
    assert not missing, (
        "these event kinds are accepted by the projector (and therefore can be stored in "
        "the event log) but cannot be rendered by Event/CtfEvent, which makes "
        "GET /api/tasks/{id}/events fail validation with HTTP 500: "
        f"{missing}"
    )
