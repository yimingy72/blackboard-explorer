"""Remove platform payloads and non-runtime data before flattening the tools image."""

import grp
import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    removed = []
    subprocess.run(["apt-mark", "manual", "python3-numpy", "libgmp-dev", "libssl-dev"], check=True)

    def orphaned_packages() -> set[str]:
        plan = subprocess.check_output(["apt-get", "-s", "autoremove", "--purge"], text=True)
        return {line.split()[1] for line in plan.splitlines() if line.startswith("Purg ")}

    old_orphans = orphaned_packages()
    replaced_packages = ["nodejs", "npm", "python3-opencv"]
    omitted_packages = ["qemu-user", "gdb-multiarch", "apktool", "dex2jar", "android-framework-res"]
    package_status = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${Package}\t${db:Status-Status}\n"], text=True
    )
    installed = {
        row.split("\t")[0] for row in package_status.splitlines() if row.endswith("\tinstalled")
    }
    selected = [name for name in replaced_packages + omitted_packages if name in installed]
    if selected:
        subprocess.run(["apt-get", "purge", "-y", *selected], check=True)
    new_orphans = sorted(orphaned_packages() - old_orphans)
    if new_orphans:
        subprocess.run(["apt-get", "purge", "-y", *new_orphans], check=True)
    # Preserve mathematical data, Python tests used as runtime modules, and tool help.
    paths = [
        "/opt/bbx",
        "/opt/envd",
        "/opt/traffic",
        "/opt/opensandbox",
        "/etc/bbx",
        "/var/log/bbx",
        "/etc/sudoers.d/bbx-agents",
        "/root/.cache",
        "/var/cache/apt",
        "/var/lib/apt/lists",
        "/opt/sage/etc/conda/test-files",
        "/opt/sage/share/doc",
        "/opt/sage/share/giac/doc",
        "/opt/sage/share/giac/examples",
        "/opt/sage/share/threejs/examples",
        "/opt/sage/lib/jvm/lib/src.zip",
        "/opt/sage/lib/jvm",
        "/opt/sage/bin/pandoc",
        "/opt/sage/share/jmol/jsmol",
        "/opt/sage/share/jupyter",
        "/opt/sage/lib/python3.12/site-packages/notebook/static",
        "/opt/sage/lib/python3.12/site-packages/jupyterlab/static",
        "/opt/sage/x86_64-conda-linux-gnu/sysroot/lib64/locale/locale-archive",
        "/opt/sage/x86_64-conda-linux-gnu/sysroot/lib64/locale/locale-archive.real",
        "/usr/lib/jvm/java-25-openjdk-amd64/jmods",
        "/usr/share/locale",
        "/usr/share/ghidra/Extensions/Ghidra",
        "/usr/share/ghidra/Ghidra/Features/BSim/support/postgresql-15.18.tar.gz",
        "/usr/share/ghidra/Ghidra/Debug/Debugger-agent-dbgeng/pypkg/dist",
    ]
    for value in paths:
        path = Path(value)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
            removed.append(value)
        elif path.exists() or path.is_symlink():
            path.unlink()
            removed.append(value)
    Path("/opt/sage/lib/jvm").symlink_to(
        "/usr/lib/jvm/java-25-openjdk-amd64", target_is_directory=True
    )
    for path in Path("/usr/share/ghidra").rglob("*-src.zip"):
        removed.append(str(path))
        path.unlink()
    for directory in (Path("/opt/sage/share/gap"), Path("/opt/sage/share/maxima")):
        for path in directory.rglob("*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and {"doc", "docs", "html"}.intersection(path.relative_to(directory).parts)
                and path.suffix.lower() in {".pdf", ".html", ".png", ".css", ".js", ".dvi", ".ps"}
            ):
                removed.append(str(path))
                path.unlink()

    # Keep package copyright/license files even when dropping bundled manuals.
    for path in Path("/usr/share/doc").glob("*/*"):
        if path.name.startswith(("copyright", "LICENSE", "COPYING")):
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    stripped = []
    seen = set()
    for directory in ("/opt/sage", "/usr/local/lib", "/usr/lib/x86_64-linux-gnu"):
        for current, _, names in os.walk(directory):
            for name in names:
                path = Path(current) / name
                if path.is_symlink():
                    continue
                stat = path.stat()
                inode = (stat.st_dev, stat.st_ino)
                if stat.st_size < 1024 * 1024 or inode in seen:
                    continue
                seen.add(inode)
                with path.open("rb") as stream:
                    magic = stream.read(8)
                if not (magic.startswith(b"\x7fELF") or magic == b"!<arch>\n"):
                    continue
                sections = subprocess.run(
                    ["readelf", "--wide", "--section-headers", str(path)],
                    capture_output=True,
                    check=False,
                )
                if b".debug_" not in sections.stdout and b".zdebug_" not in sections.stdout:
                    continue
                result = subprocess.run(
                    ["strip", "--strip-debug", str(path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode:
                    raise RuntimeError(f"Could not strip {path}: {result.stderr}")
                after = path.stat().st_size
                if after < stat.st_size:
                    stripped.append({"path": str(path), "before": stat.st_size, "after": after})
    report = {
        "removed": removed,
        "replaced_packages": replaced_packages,
        "omitted_packages": omitted_packages,
        "removed_orphan_packages": new_orphans,
        "aliases": {"/opt/sage/lib/jvm": "/usr/lib/jvm/java-25-openjdk-amd64"},
        "stripped": stripped,
        "stripped_bytes": sum(item["before"] - item["after"] for item in stripped),
    }
    Path("/opt/ctf-tools/slimming.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"stripped_bytes": report["stripped_bytes"], "files": len(stripped)}))

    # Replace the old platform identity/config while retaining container-local sudo.
    user = pwd.getpwuid(10000).pw_name
    group = grp.getgrgid(10000).gr_name
    if user != "ctf":
        subprocess.run(["usermod", "-l", "ctf", user], check=True)
    if group != "ctf":
        subprocess.run(["groupmod", "-n", "ctf", group], check=True)
    sudoers = Path("/etc/sudoers.d/ctf-tools")
    sudoers.write_text("ctf ALL=(ALL:ALL) NOPASSWD: ALL\nDefaults !use_pty\n")
    sudoers.chmod(0o440)
    subprocess.run(["visudo", "-cf", str(sudoers)], check=True)
    # File caps that exceed the container bounding set otherwise prevent exec entirely.
    nmap = "/usr/lib/nmap/nmap"
    if "security.capability" in os.listxattr(nmap):
        subprocess.run(["setcap", "-r", nmap], check=True)


if __name__ == "__main__":
    main()
