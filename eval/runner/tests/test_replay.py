"""Replay selection and isolation tests never call a model."""

import hashlib
import io
import json
import tarfile

import pytest
import zstandard

from eval.runner.replay import execute, replay, validate_manifest


def archive(path, members, *, compress=False, gzip=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz" if gzip else "w") as tar:
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


@pytest.mark.parametrize(
    "change",
    [
        {"service_port": True},
        {"service_port": 1023},
        {"service_port": 65536},
        {"service_port": "8021"},
        {"seed": "true"},
        {"files": ["agents/agent-1/repro.py"]},
        {"files": ["shared/mini-shop/shop/app.py"]},
        {"files": ["agents/agent-1/common.py"] * 2},
        {"files": [f"agents/agent-1/file-{index}.py" for index in range(17)]},
        {"seed_tokens": {"alice": "a" * 32}},
        {"service_port": 8021, "seed": False, "seed_tokens": {"alice": "a" * 32}},
        {"service_port": 8021, "seed_tokens": {"admin": "a" * 32}},
        {"service_port": 8021, "seed_tokens": {"alice": "not-32-hex"}},
        {"service_port": 8021, "seed_tokens": {"alice": "a" * 32, "bob": "a" * 32}},
    ],
)
def test_rejects_invalid_service_and_attachment_settings(change):
    item = {
        "id": "one",
        "path": "agents/agent-1/repro.py",
        "expected_exit_code": 0,
        "expected_output": "observed",
        **change,
    }
    with pytest.raises(ValueError):
        validate_manifest({"scripts": [item]})


def test_accepts_seedless_service_and_bounded_attachments():
    item = {
        "id": "one",
        "path": "agents/agent-1/repro.py",
        "service_port": 8021,
        "seed": False,
        "files": ["agents/agent-1/repro/common.py"],
        "expected_exit_code": 0,
        "expected_output": "observed",
    }
    assert validate_manifest({"scripts": [item]}) == [item]


def test_attachment_byte_limits_apply_before_container_creation(tmp_path):
    with pytest.raises(ValueError, match="size limit"):
        execute(
            "print('ok')",
            tmp_path / "unused.tar.gz",
            "example",
            10,
            files={"agents/oversized.txt": "x" * (200 * 1024 + 1)},
        )
    with pytest.raises(ValueError, match="size limit"):
        execute(
            "print('ok')",
            tmp_path / "unused.tar.gz",
            "example",
            10,
            files={f"agents/file-{index}.txt": "x" * (200 * 1024) for index in range(11)},
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
        "eval.runner.replay.execute", lambda *args, **kwargs: {"exit_code": 0, "output": "observed"}
    )
    result = await replay(tmp_path, target, "example", 10)
    assert result["reproducibility"] == 0.5
    assert [item["status"] for item in result["scripts"]] == ["passed", "error"]
    assert result["scripts"][0]["service_port"] is None
    assert result["scripts"][0]["seed"] is None
    assert result["scripts"][0]["script_sha256"] == hashlib.sha256(b"print('observed')").hexdigest()
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


@pytest.mark.integration
async def test_fresh_offline_service_replay_with_original_paths_twice(tmp_path):
    token = "a" * 32
    common_path = "agents/agent-1/repro/common.py"
    script_path = "agents/agent-1/repro.py"
    script = f"""
import httpx, sys
from pathlib import Path
sys.path.insert(0, '/workspace/agents/agent-1/repro')
from common import MARKER
assert Path(__file__).as_posix() == '/workspace/{script_path}'
output = Path(__file__).with_name('repro_out.txt')
assert not output.exists()
response = httpx.get('http://127.0.0.1:8127/me', headers={{'Authorization': 'Bearer {token}'}})
assert response.status_code == 200, response.text
assert response.json()['name'] == 'alice'
output.write_text(MARKER)
print(MARKER)
"""
    archive(
        tmp_path / "workspace.tar.zst",
        {
            script_path: script,
            common_path: "MARKER = 'observed fresh local service'\n",
        },
        compress=True,
    )
    target = tmp_path / "target.tar.gz"
    archive(
        target,
        {
            "shop/__init__.py": "",
            "shop/app.py": """
import os, sqlite3
from fastapi import FastAPI, Header, HTTPException
app = FastAPI()
@app.get('/products')
def products():
    return [{'name': 'Notebook'}]
@app.get('/me')
def me(authorization: str = Header(default='')):
    with sqlite3.connect(os.environ['SHOP_DB']) as connection:
        row = connection.execute('SELECT name FROM users WHERE token=?',
                                 (authorization.removeprefix('Bearer '),)).fetchone()
    if row is None:
        raise HTTPException(401, 'invalid token')
    return {'name': row[0]}
""",
            "scripts/seed.py": """
import os, secrets, sqlite3
with sqlite3.connect(os.environ['SHOP_DB']) as connection:
    connection.execute('CREATE TABLE users (name TEXT UNIQUE, token TEXT UNIQUE)')
    for name in ('alice', 'bob'):
        connection.execute('INSERT INTO users(name, token) VALUES(?, ?)',
                           (name, secrets.token_hex(16)))
""",
        },
        gzip=True,
    )
    (tmp_path / "replay.json").write_text(
        json.dumps(
            {
                "scripts": [
                    {
                        "id": "local-service",
                        "path": script_path,
                        "files": [common_path],
                        "service_port": 8127,
                        "seed": True,
                        "seed_tokens": {"alice": token, "bob": "b" * 32},
                        "expected_exit_code": 0,
                        "expected_output": "observed fresh local service",
                    }
                ]
            }
        )
    )
    for _ in range(2):
        document = await replay(tmp_path, target, "bbx-eval-env:latest", 60)
        result = document["scripts"][0]
        assert result["status"] == "passed", result
        assert result["service_port"] == 8127
        assert result["seed"] is True
        assert result["path"] == script_path
        assert result["script_sha256"] == hashlib.sha256(script.encode()).hexdigest()
        assert (
            result["attachment_sha256"][common_path]
            == hashlib.sha256(b"MARKER = 'observed fresh local service'\n").hexdigest()
        )
        assert result["seed_token_sha256"]["alice"] == hashlib.sha256(token.encode()).hexdigest()
        assert token not in json.dumps(document)
