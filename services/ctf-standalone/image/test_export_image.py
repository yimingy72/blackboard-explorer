"""Offline artifact checks; run with python3 -m unittest discover -s this_directory."""

import gzip
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from export_image import UPLOAD_LIMIT, check_size, export_image, verify_archive


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = Path(__file__).resolve().parents[3] / ".data/ctf-standalone-image-20261120/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "image.tar.gz"
        self.config = b'{"architecture":"amd64","os":"linux"}'
        self.digest = hashlib.sha256(self.config).hexdigest()
        self.image_id = f"sha256:{self.digest}"
        self.tag = "bbx-ctf-export-test:local"

    def archive(
        self, *, modern=False, tags=None, missing_layer=False, wrong_config=False, oci_index=False
    ) -> None:
        config_path = f"blobs/sha256/{self.digest}" if modern else f"{self.digest}.json"
        manifest = [
            {
                "Config": config_path,
                "RepoTags": [self.tag] if tags is None else tags,
                "Layers": ["layer.tar"],
            }
        ]
        entries = {
            "manifest.json": json.dumps(manifest).encode(),
            config_path: b"wrong" if wrong_config else self.config,
        }
        if not missing_layer:
            entries["layer.tar"] = b"synthetic layer"
        if oci_index:
            descriptor = {"config": {"digest": f"sha256:{self.digest}"}}
            data = json.dumps(descriptor).encode()
            manifest_digest = hashlib.sha256(data).hexdigest()
            entries[f"blobs/sha256/{manifest_digest}"] = data
            data = json.dumps({"manifests": [{"digest": f"sha256:{manifest_digest}"}]}).encode()
            index_digest = hashlib.sha256(data).hexdigest()
            entries[f"blobs/sha256/{index_digest}"] = data
            self.image_id = f"sha256:{index_digest}"
        with tarfile.open(self.path, "w:gz") as archive:
            for name, data in entries.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

    def test_legacy_and_modern_config_layout(self) -> None:
        for modern in (False, True):
            with self.subTest(modern=modern):
                self.archive(modern=modern)
                result = verify_archive(self.path, self.tag, self.image_id)
                self.assertEqual(result["archive_bytes"], self.path.stat().st_size)
                self.assertEqual(
                    result["sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest()
                )
                self.assertEqual(result["layers"], 1)

    def test_wrong_or_extra_tag_is_rejected(self) -> None:
        for tags in (["other:tag"], [self.tag, "other:tag"]):
            with self.subTest(tags=tags):
                self.archive(tags=tags)
                with self.assertRaisesRegex(ValueError, "exactly one tag"):
                    verify_archive(self.path, self.tag, self.image_id)

    def test_containerd_index_image_id(self) -> None:
        self.archive(modern=True, oci_index=True)
        result = verify_archive(self.path, self.tag, self.image_id)
        self.assertEqual(result["config_digest"], f"sha256:{self.digest}")

    def test_config_mismatch_and_missing_layer(self) -> None:
        for change in ({"wrong_config": True}, {"missing_layer": True}):
            with self.subTest(change=change):
                self.archive(**change)
                with self.assertRaises(ValueError):
                    verify_archive(self.path, self.tag, self.image_id)

    def test_corrupt_gzip_trailer_is_rejected(self) -> None:
        self.archive()
        data = bytearray(self.path.read_bytes())
        data[-8] ^= 0xFF
        self.path.write_bytes(data)
        with self.assertRaises(gzip.BadGzipFile):
            verify_archive(self.path, self.tag, self.image_id)

    def test_size_boundary_is_strict(self) -> None:
        check_size(UPLOAD_LIMIT - 1)
        for size in (0, UPLOAD_LIMIT, UPLOAD_LIMIT + 1):
            with self.subTest(size=size), self.assertRaises(ValueError):
                check_size(size)

    def test_existing_artifact_is_not_overwritten(self) -> None:
        self.path.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            export_image(self.tag, self.path)
        self.assertEqual(self.path.read_bytes(), b"keep")

    def test_receipt_publish_failure_rolls_back_only_own_archive(self) -> None:
        self.archive()
        tar_bytes = gzip.decompress(self.path.read_bytes())
        self.path.unlink()
        save = MagicMock()
        save.__enter__.return_value = save
        save.stdout = io.BytesIO(tar_bytes)
        save.wait.return_value = save.poll.return_value = 0
        receipt = self.path.with_name(self.path.name + ".json")
        original_link = os.link

        def competing_receipt(source, destination):
            if destination == receipt:
                receipt.write_text("another run", encoding="utf-8")
                raise FileExistsError("Receipt was published by another run")
            original_link(source, destination)

        with (
            patch(
                "export_image.subprocess.check_output",
                return_value=json.dumps(
                    {
                        "image_id": self.image_id,
                    }
                ).encode(),
            ),
            patch("export_image.subprocess.Popen", return_value=save),
            patch("export_image.os.link", side_effect=competing_receipt),
            self.assertRaises(FileExistsError),
        ):
            export_image(self.tag, self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(receipt.read_text(), "another run")
        self.assertEqual(list(self.path.parent.glob("*.partial")), [])


if __name__ == "__main__":
    unittest.main()
