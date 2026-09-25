"""Replay selection and isolation tests never call a model."""

import io
import json
import tarfile

import pytest
import zstandard

from eval.runner.replay import execute, replay, validate_manifest


def archive(path, members, *, compress=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        for name, content in members.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    data = output.getvalue()
    path.write_bytes(zstandard.ZstdCompressor().compress(data) if compress else data)


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "agents/../secret.py", "agents/x.sh", None, "shared//a.py"]
)
def test_rejects_non_normalized_script_paths(path):
    with pytest.raises(ValueError):
        validate_manifest(
            {
                "scripts": [
                    {"id": "one", "path": path, "expected_exit_code": 0, "expected_output": "ok"}
                ]
            }
        )


async def test_replay_reports_observed_result_and_preserves_missing_as_error(tmp_path, monkeypatch):
    scripts = [
        {
            "id": "one",
            "path": "agents/agent-1/repro.py",
            "expected_exit_code": 0,
            "expected_output": "observed",
        },
        {
            "id": "missing",
            "path": "agents/agent-1/absent.py",
            "expected_exit_code": 0,
            "expected_output": "observed",
        },
    ]
    (tmp_path / "replay.json").write_text(json.dumps({"scripts": scripts}))
    archive(
        tmp_path / "workspace.tar.zst", {scripts[0]["path"]: "print('observed')"}, compress=True
    )
    target = tmp_path / "target.tar.gz"
    archive(target, {"app.py": "print('target')"})
    monkeypatch.setattr(
        "eval.runner.replay.execute", lambda *args: {"exit_code": 0, "output": "observed"}
    )
    result = await replay(tmp_path, target, "example", 10)
    assert result["reproducibility"] == 0.5
    assert [item["status"] for item in result["scripts"]] == ["passed", "error"]
    assert (tmp_path / "replay-results.json").is_file()


@pytest.mark.integration
def test_fresh_offline_container_can_run_target_dependencies(tmp_path):
    target = tmp_path / "target.tar.gz"
    archive(target, {"shop/__init__.py": "", "shop/example.py": "VALUE = 42"})
    script = """
import os
import fastapi, pytest, httpx
from pathlib import Path
from shop.example import VALUE
assert not Path('/var/run/docker.sock').exists()
assert 'DEEPSEEK_API_KEY' not in os.environ
assert not Path('previous.txt').exists()
Path('previous.txt').write_text('one')
print('value', VALUE)
"""
    for _ in range(2):
        result = execute(script, target, "bbx-eval-env:latest", 30)
        assert result["exit_code"] == 0, result["output"]
        assert "value 42" in result["output"]
