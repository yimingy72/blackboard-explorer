from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bbx_runtime.sandbox_mcp import SandboxMCP


@pytest.mark.parametrize(
    ("agent_id", "parent", "directory"),
    [
        ("lead", None, "agent-1"),
        ("member-1", None, "member-1"),
        ("helper", "member-1", "member-1"),
    ],
)
async def test_assignment_prepares_the_promised_workspace_as_solver(agent_id, parent, directory):
    run = AsyncMock(return_value=SimpleNamespace(complete=True, exit_code=0, error=None))
    sandbox = SimpleNamespace(commands=SimpleNamespace(run=run))
    binding = SimpleNamespace(agent_id=agent_id, parent_agent_id=parent, challenge_id=None)
    await SandboxMCP.prepare_workspace(sandbox, binding)
    assert run.call_args.args[0] == ["/bin/mkdir", "-p", "/workspace/agents/" + directory]
    options = run.call_args.kwargs["opts"]
    assert options.uid == 10000 and options.gid == 10000
    assert options.working_directory == "/workspace"
    run.return_value.exit_code = 1
    with pytest.raises(ValueError, match="workspace creation failed"):
        await SandboxMCP.prepare_workspace(sandbox, binding)


async def test_peer_workspace_keeps_agent_cwd_and_shared_challenge_directories():
    run = AsyncMock(return_value=SimpleNamespace(complete=True, exit_code=0, error=None))
    sandbox = SimpleNamespace(commands=SimpleNamespace(run=run))
    binding = SimpleNamespace(agent_id="member-2", parent_agent_id=None, challenge_id="challenge")
    await SandboxMCP.prepare_workspace(sandbox, binding)
    assert run.call_args.args[0] == [
        "/bin/mkdir",
        "-p",
        "/workspace/agents/member-2",
        "/workspace/shared/challenges/challenge/inputs",
        "/workspace/shared/challenges/challenge/workers/member-2",
        "/workspace/shared/challenges/challenge/artifacts",
    ]
