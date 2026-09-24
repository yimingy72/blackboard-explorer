"""Run the distributed shop with a fresh seeded SQLite database."""

import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

ARCHIVE = Path(__file__).resolve().parents[3] / "targets" / "dist" / "mini-shop.tar.gz"


@contextmanager
def shop():
    with tempfile.TemporaryDirectory(prefix="bbx-eval-verify-") as directory:
        root = Path(directory)
        with tarfile.open(ARCHIVE) as archive:
            archive.extractall(root, filter="data")
        env = os.environ.copy()
        env["SHOP_DB"] = str(root / "shop.sqlite3")
        env["PYTHONPATH"] = str(root)
        seeded = subprocess.run(
            [sys.executable, "scripts/seed.py"], cwd=root, env=env,
            text=True, capture_output=True, check=True,
        )
        credentials = {}
        for line in seeded.stdout.splitlines():
            if line.startswith("{"):
                item = __import__("ast").literal_eval(line)
                if "token" in item:
                    credentials[item["name"]] = item["token"]
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "shop.app:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False) as client:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError(process.stderr.read().decode())
                    try:
                        if client.get("/products").status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.05)
                else:
                    raise RuntimeError("shop did not start")
                yield client, credentials
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def auth(credentials, name):
    return {"Authorization": f"Bearer {credentials[name]}"}


def post(client, credentials, name, path, body):
    return client.post(path, headers=auth(credentials, name), json=body)


def balance(client, credentials, name):
    return client.get("/me", headers=auth(credentials, name)).json()["balance"]
