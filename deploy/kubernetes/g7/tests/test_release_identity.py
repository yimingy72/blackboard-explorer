"""Check the official chart rollout trigger and reject an old image before probing Tasks."""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import probe


class ReleaseIdentity(unittest.IsolatedAsyncioTestCase):
    async def test_content_change_updates_official_pod_template_with_same_tag(self):
        with tempfile.TemporaryDirectory(dir=probe.STATE) as temporary:
            state = Path(temporary)
            with patch.object(probe, "STATE", state):
                identity = {"patch_sha256": "first", "files": {"client.py": "first"}}
                (state / "identity.json").write_text(json.dumps(identity))
                first = probe.server_runtime_values("127.0.0.1:41000")
                self.assertEqual(first, probe.server_runtime_values("127.0.0.1:41000"))
                identity["patch_sha256"] = "second"
                (state / "identity.json").write_text(json.dumps(identity))
                second = probe.server_runtime_values("127.0.0.1:41000")
                third = probe.server_runtime_values("127.0.0.1:42000")
                hashes = []
                for runtime in (first, second, third):
                    values = state / "runtime.json"
                    values.write_text(json.dumps(runtime))
                    rendered = subprocess.check_output(
                        [
                            str(probe.ROOT / ".data/bbx-archv2-g7/helm"),
                            "template",
                            "bbx-archv2-g7-server",
                            str(
                                probe.ROOT / ".data/bbx-archv2-g7/upstream/manifests/charts/server"
                            ),
                            "-n",
                            probe.SYSTEM,
                            "-f",
                            str(probe.HERE / "values/server.yaml"),
                            "-f",
                            str(values),
                            "--show-only",
                            "templates/deployment.yaml",
                        ],
                        text=True,
                    )
                    pod_metadata = rendered.split("  template:\n", 1)[1].split("    spec:\n", 1)[0]
                    annotation = re.search(
                        r'        bbx\.archv2/content-sha256: "?([a-f0-9]{64})"?',
                        pod_metadata,
                    )
                    self.assertIsNotNone(annotation)
                    assert annotation is not None
                    self.assertEqual(
                        annotation[1],
                        runtime["server"]["podAnnotations"]["bbx.archv2/content-sha256"],
                    )
                    self.assertIn("image: bbx-archv2-server-g7:bbx-g7-direct-1", rendered)
                    hashes.append(annotation[1])
                self.assertEqual(len(set(hashes)), 3)

    async def test_old_remote_identity_is_rejected_before_task_side_effects(self):
        with tempfile.TemporaryDirectory(dir=probe.STATE) as temporary:
            state = Path(temporary)
            expected = {"patch_sha256": "current", "files": {"client.py": "current"}}
            (state / "identity.json").write_text(json.dumps(expected))
            with (
                patch.object(probe, "STATE", state),
                patch.object(probe, "kubectl", return_value=json.dumps(expected)) as kubectl,
                patch.object(probe, "forward") as forward,
                patch.object(probe.Sandbox, "create", new_callable=AsyncMock) as create,
            ):
                self.assertEqual(probe.verified_patch_identity(), expected)
                for old in (
                    {"patch_sha256": "old", "files": expected["files"]},
                    {"patch_sha256": "current", "files": {"client.py": "old"}},
                ):
                    kubectl.return_value = json.dumps(old)
                    with self.assertRaisesRegex(RuntimeError, "identity differs"):
                        await probe.verify()
                forward.assert_not_called()
                create.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
