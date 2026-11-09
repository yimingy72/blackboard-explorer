"""Bind one fixed native UI build to its original standalone browser routes."""

import argparse
import hashlib
import json
import re
from pathlib import Path

UPSTREAM_UI_COMMIT = "26f5aebe99a36a037fe54f8080a608aca5547e15"
ROUTE = re.compile(r"const (\w+) = createRoute\(\{(.*?)\n\}\);", re.S)
PARENT = re.compile(r"getParentRoute: \(\) => (\w+),")
PATH = re.compile(r"^\s*path: '([/A-Za-z0-9-]+)',", re.M)
ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*")
LOCAL_LINK = re.compile(r'(?:src|href)="(/ui/[^"]+)"')


def routes(main: str) -> list[str]:
    prefixes = {"rootRoute": ""}
    paths = {"/ui"}
    for name, block in ROUTE.findall(main):
        parent = PARENT.search(block)
        if parent is None or parent[1] not in prefixes:
            raise ValueError("Unexpected pinned native UI route parent")
        prefix = prefixes[parent[1]]
        route = PATH.search(block)
        if route is None:
            if name != "appRoute":
                raise ValueError("Unexpected pinned native UI route without a path")
            prefixes[name] = prefix
            continue
        prefix = prefix + route[1].rstrip("/")
        prefixes[name] = prefix
        paths.add("/ui" + prefix)
    if (
        not {"/ui/login", "/ui/llm/models", "/ui/settings/ui", "/ui/settings/observability"}
        <= paths
    ):
        raise ValueError("Incomplete pinned native UI route list")
    return sorted(paths)


def manifest(dist: Path, main: str) -> dict:
    files = {}
    for path in sorted(dist.rglob("*")):
        if path.is_symlink():
            raise ValueError("A native UI build cannot contain symlinks")
        if not path.is_file():
            continue
        name = path.relative_to(dist).as_posix()
        if not ASSET.fullmatch(name):
            raise ValueError("Unexpected native UI build path")
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    index = (dist / "index.html").read_text()
    links = LOCAL_LINK.findall(index)
    if not links or not all(link.removeprefix("/ui/") in files for link in links):
        raise ValueError("Native UI index does not reference its own complete build")
    return {"upstream_commit": UPSTREAM_UI_COMMIT, "spa_paths": routes(main), "files": files}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", type=Path)
    parser.add_argument("pinned_main_tsx", type=Path)
    args = parser.parse_args()
    print(json.dumps(manifest(args.dist, args.pinned_main_tsx.read_text()), indent=2))
