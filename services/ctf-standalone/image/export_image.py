#!/usr/bin/env python3
"""Export one Docker tag as a verified contest upload, without storing a second tar."""

import argparse
import gzip
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

UPLOAD_LIMIT = 3_000_000_000
CHUNK_SIZE = 1024 * 1024


def check_size(size: int) -> None:
    if not 0 < size < UPLOAD_LIMIT:
        raise ValueError(f"Upload must be smaller than {UPLOAD_LIMIT} bytes; got {size}")


def verify_archive(path: Path, image: str, image_id: str) -> dict:
    """Check gzip integrity, single-tag metadata and config identity without extracting."""
    check_size(path.stat().st_size)
    files: set[str] = set()
    documents: dict[str, dict] = {}
    digests: dict[str, str] = {}
    manifest = None
    with gzip.open(path, "rb") as stream:
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                name = member.name.removeprefix("./")
                if name in files:
                    raise ValueError(f"Duplicate archive member: {name}")
                files.add(name)
                if not (name.endswith(".json") or name.startswith("blobs/sha256/")):
                    continue
                if member.size > CHUNK_SIZE:
                    continue
                content = archive.extractfile(member)
                if content is None:
                    raise ValueError(f"Unreadable image metadata: {name}")
                data = content.read()
                if name == "manifest.json":
                    manifest = json.loads(data)
                else:
                    try:
                        document = json.loads(data)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(document, dict):
                        digest = "sha256:" + hashlib.sha256(data).hexdigest()
                        documents[digest] = document
                        digests[name] = digest
        # Tar readers stop at the end markers; consume the gzip trailer too.
        while stream.read(CHUNK_SIZE):
            pass
    if not isinstance(manifest, list) or len(manifest) != 1:
        raise ValueError("Expected exactly one image in manifest.json")
    entry = manifest[0]
    if not isinstance(entry, dict) or entry.get("RepoTags") != [image]:
        raise ValueError(f"Expected exactly one tag: {image}")
    # Containerd's image ID may identify an OCI index, rather than the config.
    reachable = {image_id}
    pending = [image_id]
    while pending:
        document = documents.get(pending.pop(), {})
        descriptors = document.get("manifests", []) + [document.get("config", {})]
        for descriptor in descriptors:
            digest = descriptor.get("digest")
            if isinstance(digest, str) and digest not in reachable:
                reachable.add(digest)
                pending.append(digest)
    config_path = entry.get("Config")
    config_digest = digests.get(config_path) if isinstance(config_path, str) else None
    if config_digest not in reachable:
        raise ValueError("Saved image config does not match the inspected image ID")
    layers = entry.get("Layers")
    if not isinstance(layers, list) or not layers or any(layer not in files for layer in layers):
        raise ValueError("Image layers are missing from the archive")
    with path.open("rb") as source:
        sha256 = hashlib.file_digest(source, "sha256").hexdigest()
    return {
        "archive_bytes": path.stat().st_size,
        "sha256": sha256,
        "config_digest": config_digest,
        "layers": len(layers),
    }


def export_image(image: str, output: Path) -> dict:
    if image.startswith("-") or "@" in image or ":" not in image.rsplit("/", 1)[-1]:
        raise ValueError("Pass one explicit repository:tag, not an image ID or repository")
    if not (output.name.endswith(".tar.gz") or output.name.endswith(".tgz")):
        raise ValueError("Output filename must end in .tar.gz or .tgz")
    receipt = output.with_name(output.name + ".json")
    if output.exists() or receipt.exists():
        raise FileExistsError("Output or receipt already exists; choose a new filename")
    template = (
        '{"image_id":{{json .Id}},"inspect_size_bytes":{{json .Size}},'
        '"os":{{json .Os}},"architecture":{{json .Architecture}}}'
    )
    metadata = json.loads(
        subprocess.check_output(["docker", "image", "inspect", "--format", template, image])
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    tar_bytes = 0
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".partial") as temporary:
        with subprocess.Popen(["docker", "image", "save", image], stdout=subprocess.PIPE) as save:
            try:
                assert save.stdout is not None
                with gzip.GzipFile(
                    filename="", mode="wb", fileobj=temporary, compresslevel=9, mtime=0
                ) as compressed:
                    while chunk := save.stdout.read(CHUNK_SIZE):
                        tar_bytes += len(chunk)
                        compressed.write(chunk)
                        if temporary.tell() >= UPLOAD_LIMIT:
                            raise ValueError("Compressed upload exceeds the 3 GB limit")
                if save.wait() != 0:
                    raise RuntimeError("docker image save failed; no upload was published")
            finally:
                if save.poll() is None:
                    save.terminate()
                if save.stdout is not None:
                    save.stdout.close()
        temporary.flush()
        os.fsync(temporary.fileno())
        result = {
            "image": image,
            **metadata,
            "docker_save_tar_bytes": tar_bytes,
            **verify_archive(Path(temporary.name), image, metadata["image_id"]),
            "upload_limit_bytes": UPLOAD_LIMIT,
            "archive": str(output.resolve()),
            "docker_load_verified": False,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, suffix=".partial"
        ) as receipt_file:
            json.dump(result, receipt_file, indent=2)
            receipt_file.write("\n")
            receipt_file.flush()
            os.fsync(receipt_file.fileno())
            # Linking publishes without overwriting artifacts from another run.
            os.link(temporary.name, output)
            try:
                os.link(receipt_file.name, receipt)
            except BaseException:
                if output.exists() and os.path.samefile(temporary.name, output):
                    output.unlink()
                raise
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="One explicit repository:tag")
    parser.add_argument("output", type=Path, help="Destination .tar.gz or .tgz")
    arguments = parser.parse_args()
    try:
        result = export_image(arguments.image, arguments.output)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, tarfile.TarError) as exc:
        parser.exit(1, f"Export failed: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
