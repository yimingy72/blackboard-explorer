"""Offline integrity checks for pinned third-party downloads."""

import hashlib
import io
import unittest
from unittest.mock import patch

from install_extra_tools import download


class DownloadTests(unittest.TestCase):
    def test_sha256_verified(self) -> None:
        payload = b"generic tool fixture"
        source = {
            "url": "https://invalid.example/tool",
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        with patch("install_extra_tools.urlopen", return_value=io.BytesIO(payload)):
            self.assertEqual(download(source), payload)

    def test_git_blob_verified(self) -> None:
        payload = b"word\n"
        source = {
            "url": "https://invalid.example/words",
            "git_blob_sha": hashlib.sha1(b"blob 5\0" + payload).hexdigest(),
        }
        with patch("install_extra_tools.urlopen", return_value=io.BytesIO(payload)):
            self.assertEqual(download(source), payload)

    def test_changed_download_rejected(self) -> None:
        with patch("install_extra_tools.urlopen", return_value=io.BytesIO(b"wrong")):
            with self.assertRaises(ValueError):
                download({"url": "https://invalid.example/tool", "sha256": "0" * 64})


if __name__ == "__main__":
    unittest.main()
