"""Install the original approved attachment tree without a privileged workspace writer."""

import contextlib
import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from uuid import UUID

CONTROL = "/var/lib/bbx/control/inputs"
HELPER = "/opt/bbx/control/materialize-inputs.py"
WORKSPACE = "/workspace"
UID = GID = 10000
FILE_LIMIT, TOTAL_LIMIT, MAX_FILES = 16 * 1024**2, 24 * 1024**2, 20
WORKSPACE_MARGIN = 16 * 1024**2
MANIFEST_LIMIT = 32 * 1024
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
SOURCE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
HEX = re.compile(r"[0-9a-f]{64}\Z")


class IntegrityMismatch(ValueError):
    pass


class WorkspaceConflict(ValueError):
    pass


def require(check):
    if not check:
        raise ValueError("Original attachment control evidence is invalid")


def uuid(value):
    return isinstance(value, str) and str(UUID(value)) == value


def directory(name, *, parent=None, owner=0):
    fd = os.open(name, DIRECTORY, dir_fd=parent)
    info = os.fstat(fd)
    if info.st_uid != owner or info.st_mode & 0o022:
        os.close(fd)
        raise WorkspaceConflict("Directory ownership does not match the original workspace")
    return fd


def create_dir(name, parent, mode):
    os.mkdir(name, mode, dir_fd=parent)
    os.fsync(parent)


def private_file(parent, name, limit):
    fd = os.open(name, SOURCE, dir_fd=parent)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_nlink != 1
        or info.st_mode & 0o077
        or info.st_size > limit
    ):
        os.close(fd)
        raise IntegrityMismatch("Original staged attachment is invalid")
    return fd


def read(fd, limit):
    with os.fdopen(os.dup(fd), "rb") as file:
        data = file.read(limit + 1)
    if len(data) > limit:
        raise IntegrityMismatch("Original attachment control payload exceeded its size limit")
    os.lseek(fd, 0, os.SEEK_SET)
    return data


def new_file(parent, name, data):
    fd = os.open(name, NEW_FILE, 0o600, dir_fd=parent)
    with os.fdopen(fd, "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.fsync(parent)


def manifest(fd, digest):
    data = read(fd, MANIFEST_LIMIT)
    if hashlib.sha256(data).hexdigest() != digest:
        raise IntegrityMismatch("Original attachment manifest digest changed")
    value = json.loads(data)
    require(set(value) == {"scope", "files"})
    scope, files = value["scope"], value["files"]
    require(
        set(scope)
        == {
            "task_id",
            "operation_id",
            "resource_generation",
            "workspace_pvc_uid",
            "sandbox_id",
            "sandbox_pod_uid",
            "sandbox_container_id",
            "execution_context_id",
        }
    )
    require(
        all(
            uuid(scope[key])
            for key in (
                "task_id",
                "operation_id",
                "workspace_pvc_uid",
                "sandbox_id",
                "sandbox_pod_uid",
            )
        )
    )
    require(type(scope["resource_generation"]) is int and scope["resource_generation"] >= 1)
    require(
        isinstance(scope["sandbox_container_id"], str)
        and re.fullmatch(r"containerd://[0-9a-f]{64}", scope["sandbox_container_id"])
    )
    require(HEX.fullmatch(scope["execution_context_id"]))
    require(isinstance(files, list) and 1 <= len(files) <= MAX_FILES)
    ids, total = set(), 0
    for item in files:
        require(set(item) == {"id", "filename", "path", "size", "sha256"})
        fid, filename, size = item["id"], item["filename"], item["size"]
        require(uuid(fid) and fid not in ids)
        ids.add(fid)
        require(
            isinstance(filename, str)
            and filename not in {"", ".", ".."}
            and "/" not in filename
            and "\\" not in filename
            and len(filename.encode("utf-8")) <= 255
            and all(ord(c) >= 32 and not 127 <= ord(c) <= 159 for c in filename)
        )
        require(item["path"] == f"/workspace/shared/inputs/{fid}/{filename}")
        require(type(size) is int and 0 <= size <= FILE_LIMIT and HEX.fullmatch(item["sha256"]))
        total += size
    require(total <= TOTAL_LIMIT)
    return scope, files


def move_noreplace(parent, name, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent, os.fsencode(name), parent, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def copy(source, parent, item):
    fd = os.open(item["filename"], NEW_FILE, 0o600, dir_fd=parent)
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(fd, "wb") as target:
        while chunk := os.read(source, 64 * 1024):
            size += len(chunk)
            if size > item["size"]:
                raise IntegrityMismatch("Original staged attachment length changed")
            digest.update(chunk)
            target.write(chunk)
        if size != item["size"] or digest.hexdigest() != item["sha256"]:
            raise IntegrityMismatch("Original staged attachment digest changed")
        target.flush()
        os.fsync(target.fileno())


def low_install(digest, manifest_fd, sources):
    require(os.getuid() == os.geteuid() == UID and os.getgid() == os.getegid() == GID)
    require(not os.getgroups())
    _, files = manifest(manifest_fd, digest)
    require(len(files) == len(sources))
    published = False
    try:
        with contextlib.ExitStack() as stack:
            workspace = directory(WORKSPACE, owner=UID)
            stack.callback(os.close, workspace)
            try:
                create_dir("shared", workspace, 0o755)
            except FileExistsError:
                pass
            shared = directory("shared", parent=workspace, owner=UID)
            stack.callback(os.close, shared)
            if os.fstat(workspace).st_dev != os.fstat(shared).st_dev:
                raise WorkspaceConflict("Shared path is not on the original workspace")
            capacity = os.fstatvfs(shared)
            if capacity.f_bavail * capacity.f_frsize < (
                sum(item["size"] for item in files) + WORKSPACE_MARGIN
            ):
                return "space_unavailable"
            try:
                os.stat("inputs", dir_fd=shared, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise WorkspaceConflict("Original attachment destination already exists")
            staging = ".bbx-inputs-" + digest
            create_dir(staging, shared, 0o700)
            pending = directory(staging, parent=shared, owner=UID)
            stack.callback(os.close, pending)
            for source, item in zip(sources, files, strict=True):
                create_dir(item["id"], pending, 0o700)
                subdir = directory(item["id"], parent=pending, owner=UID)
                try:
                    copy(source, subdir, item)
                    os.fsync(subdir)
                finally:
                    os.close(subdir)
            os.fsync(pending)
            move_noreplace(shared, staging, "inputs")
            published = True
            os.fsync(shared)
        return "materialized"
    except IntegrityMismatch:
        return "integrity_mismatch"
    except WorkspaceConflict:
        return "workspace_conflict"
    except OSError as error:
        if error.errno in {errno.ENOSPC, errno.EDQUOT}:
            return "unconfirmed" if published else "space_unavailable"
        if not published and error.errno in {errno.EEXIST, errno.ELOOP, errno.ENOTDIR}:
            return "workspace_conflict"
        raise


def original_context(scope):
    result = subprocess.run(
        ["/opt/bbx/control/launcher", "--assignment-context"],
        env={},
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=5,
    )
    data = json.loads(result.stdout)
    require(
        data["pod_uid"] == scope["sandbox_pod_uid"]
        and data["execution_context_id"] == scope["execution_context_id"]
    )


def install(parent, digest):
    with contextlib.ExitStack() as stack:
        manifest_fd = private_file(parent, "manifest.json", MANIFEST_LIMIT)
        stack.callback(os.close, manifest_fd)
        scope, files = manifest(manifest_fd, digest)
        original_context(scope)
        new_file(parent, "install.claim", b"claimed\n")
        try:
            sources = []
            for item in files:
                source = private_file(parent, item["id"] + ".part", item["size"])
                stack.callback(os.close, source)
                if os.fstat(source).st_size != item["size"]:
                    raise IntegrityMismatch("Original staged attachment length changed")
                sources.append(source)
            fds = (manifest_fd, *sources)
            completed = subprocess.run(
                [
                    "/usr/bin/setpriv",
                    "--reuid=10000",
                    "--regid=10000",
                    "--clear-groups",
                    "--bounding-set=-all",
                    "--inh-caps=-all",
                    "--ambient-caps=-all",
                    "--no-new-privs",
                    "/usr/bin/python3",
                    "-I",
                    "-S",
                    HELPER,
                    "write",
                    digest,
                    *map(str, fds),
                ],
                cwd="/opt/bbx/control",
                env={},
                pass_fds=fds,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=90,
            )
            result = completed.stdout.decode().strip()
            require(
                result
                in {
                    "materialized",
                    "integrity_mismatch",
                    "workspace_conflict",
                    "space_unavailable",
                    "unconfirmed",
                }
            )
        except IntegrityMismatch:
            result = "integrity_mismatch"
        receipt = {"scope": scope | {"manifest_sha256": digest}, "result": result}
        new_file(parent, "receipt.pending", json.dumps(receipt, separators=(",", ":")).encode())
        move_noreplace(parent, "receipt.pending", "receipt.json")
        os.fsync(parent)
        for item in files:
            with contextlib.suppress(OSError):
                os.unlink(item["id"] + ".part", dir_fd=parent)
        return receipt


def root(mode, digest):
    require(os.getuid() == os.geteuid() == 0)
    if mode == "prepare":
        Path(CONTROL).mkdir(mode=0o700, exist_ok=True)
    try:
        top = directory(CONTROL)
    except FileNotFoundError:
        require(mode == "status")
        return {"result": "unconfirmed"}
    with contextlib.ExitStack() as stack:
        stack.callback(os.close, top)
        if mode == "prepare":
            create_dir(digest, top, 0o700)
            return {"result": "prepared"}
        try:
            parent = directory(digest, parent=top)
        except FileNotFoundError:
            require(mode == "status")
            return {"result": "unconfirmed"}
        stack.callback(os.close, parent)
        if mode == "install":
            return install(parent, digest)
        require(mode == "status")
        try:
            fd = private_file(parent, "receipt.json", MANIFEST_LIMIT)
        except FileNotFoundError:
            return {"result": "unconfirmed"}
        with os.fdopen(fd, "rb") as stored:
            receipt = json.loads(stored.read(MANIFEST_LIMIT + 1))
        os.fsync(parent)
        return receipt


if __name__ == "__main__":
    os.umask(0o077)
    method, digest = sys.argv[1:3]
    require(HEX.fullmatch(digest))
    if method == "write":
        manifest_fd, *sources = map(int, sys.argv[3:])
        print(low_install(digest, manifest_fd, sources))
    else:
        require(len(sys.argv) == 3)
        print(json.dumps(root(method, digest), separators=(",", ":")))
