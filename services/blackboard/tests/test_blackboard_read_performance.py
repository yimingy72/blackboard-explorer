"""Offline equivalence and query projections for ordinary blackboard reads."""

import random
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from bbx_blackboard.conversations import Conversations
from bbx_blackboard.domain.rules import dispute_fields
from sqlalchemy.ext.asyncio import AsyncEngine


def original_fields(facts):
    result = {}
    disputers = {fid: [] for fid in facts}
    for fid, fact in facts.items():
        for target in fact["disputes"]:
            if target in disputers:
                disputers[target].append(fid)
    depths = {}
    for fid, fact in facts.items():
        depths[fid] = 1 + max((depths[x] for x in fact["disputes"]), default=-1)
    for fid in reversed(list(facts)):
        fact = facts[fid]
        result[fid] = {
            "status": "disputed"
            if any(result[x]["status"] == "proposed" for x in disputers[fid])
            else "proposed",
            "depth": depths[fid],
            "relied_by": sum(
                x["author"] != fact["author"] and fid in x["derived_from"] for x in facts.values()
            ),
        }
    return result


def test_linear_reference_counts_keep_duplicate_and_author_semantics():
    facts = {
        "F1": {"author": "alice", "derived_from": [], "disputes": []},
        "F2": {"author": "alice", "derived_from": ["F1"], "disputes": []},
        "F3": {"author": "bob", "derived_from": ["F1", "F1"], "disputes": ["F1"]},
        "F4": {"author": "carol", "derived_from": ["F1", "F3"], "disputes": ["F3"]},
    }
    fields = dispute_fields(facts)
    assert fields == original_fields(facts)
    assert fields["F1"] == {"status": "proposed", "depth": 0, "relied_by": 2}
    assert fields["F3"] == {"status": "disputed", "depth": 1, "relied_by": 1}


@pytest.mark.parametrize("seed", range(30))
def test_reference_counts_and_dispute_graph_match_original_for_random_histories(seed):
    rng = random.Random(seed)
    facts: dict[str, Any] = {}
    for index in range(64):
        previous = list(facts)
        references = rng.choices(previous, k=rng.randrange(8)) if previous else []
        disputes = rng.sample(previous, min(len(previous), rng.randrange(4)))
        facts[f"F{index}"] = {
            "author": f"agent-{rng.randrange(4)}",
            "derived_from": references,
            "disputes": disputes,
        }
    assert dispute_fields(facts) == original_fields(facts)


@pytest.mark.parametrize("count", [500, 1000, 2000])
def test_reference_count_visits_each_dependent_once(count):
    class Facts(dict):
        visits = 0

        def values(self):
            self.visits += len(self)
            return super().values()

    facts = Facts(
        {
            f"F{i}": {
                "author": f"agent-{i % 2}",
                "derived_from": [f"F{i - 1}", f"F{i - 1}"] if i else [],
                "disputes": [],
            }
            for i in range(count)
        }
    )
    result = dispute_fields(facts)
    assert facts.visits == count
    assert result[f"F{count - 2}"]["relied_by"] == 1


@pytest.mark.parametrize("origin", [None, "native", "legacy", ""])
@pytest.mark.parametrize("status", ["running", "concluding", "finished"])
async def test_message_list_reads_only_session_presence_and_origin(origin, status):
    statements = []

    async def execute(statement):
        statements.append(statement)
        if statement.get_final_froms()[0].name == "agent_messages":
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: []))
        assert list(statement.selected_columns.keys()) == ["origin"]
        row = {"origin": origin} if origin is not None else None
        return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: row))

    class Engine:
        @asynccontextmanager
        async def connect(self):
            yield SimpleNamespace(execute=execute)

    service = Conversations(cast(AsyncEngine, Engine()))
    service._task_agent = AsyncMock(return_value=({}, {"status": status}))
    service._session = AsyncMock(side_effect=AssertionError("Full Session must not be loaded"))
    result = await service.list_messages(uuid4(), "agent-1", status="queued")
    assert result == {
        "messages": [],
        "session_available": origin is not None,
        "session_origin": origin,
        "mode": "active" if status in {"running", "concluding"} else "review",
    }
    assert len(statements) == 2
    assert "agent_messages.status" in str(statements[0])
