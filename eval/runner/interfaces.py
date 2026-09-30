"""Read changed HTTP route functions from the packaged target's Git history."""

import ast
import subprocess
import tarfile
import tempfile
from pathlib import Path

METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def routes(source: str) -> dict[str, tuple[str, str]]:
    tree = ast.parse(source)
    prefixes = {"app": ""}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if not isinstance(node.value.func, ast.Name) or node.value.func.id != "APIRouter":
            continue
        prefix = next((item.value for item in node.value.keywords if item.arg == "prefix"), None)
        value = ast.literal_eval(prefix) if prefix is not None else ""
        for target in node.targets:
            if isinstance(target, ast.Name):
                prefixes[target.id] = value
    result = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            method = decorator.func.attr
            owner = decorator.func.value
            if method not in METHODS or not isinstance(owner, ast.Name) or not decorator.args:
                continue
            if owner.id not in prefixes:
                raise ValueError(f"Unknown router prefix for {owner.id}")
            path = ast.literal_eval(decorator.args[0])
            route = f"{method.upper()} {prefixes[owner.id]}{path}"
            result[route] = (node.name, ast.dump(node, include_attributes=False))
    return result


def changed_interfaces(archive: Path) -> list[str]:
    if archive.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("Evaluation target archive exceeds 128 MiB")
    with tempfile.TemporaryDirectory(prefix="bbx-eval-interfaces-") as directory:
        with tarfile.open(archive) as bundle:
            size = 0
            for count, member in enumerate(bundle, 1):
                size += member.size
                if count > 50_000 or member.size < 0 or size > 512 * 1024 * 1024:
                    raise ValueError("Evaluation target exceeds its extraction limits")
                bundle.extract(member, directory, filter="data")

        def git(*args: str) -> str:
            return subprocess.check_output(
                ["git", "--no-pager", "-c", "core.fsmonitor=false", *args],
                cwd=directory,
                text=True,
                stderr=subprocess.PIPE,
                timeout=30,
            )

        changed = git(
            "diff", "--no-ext-diff", "--name-only", "main..feature/coupon-refund", "--", "shop/"
        )
        result = set()
        for name in changed.splitlines():
            if not name.endswith(".py"):
                continue
            current = Path(directory, name)
            if not current.is_file():
                continue
            after = routes(current.read_text())
            try:
                before = routes(git("show", f"main:{name}"))
            except subprocess.CalledProcessError:
                before = {}
            result.update(route for route, body in after.items() if before.get(route) != body)
        return sorted(result)
