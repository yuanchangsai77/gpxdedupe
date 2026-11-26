import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from route_semantics import package_delivery


class PackageTests(unittest.TestCase):
    def test_manifest_and_reproducibility(self):
        with tempfile.TemporaryDirectory() as directory:
            a = package_delivery.build(Path(directory) / "one.zip")
            b = package_delivery.build(Path(directory) / "two.zip")
            self.assertEqual(a["sha256"], b["sha256"])
            with zipfile.ZipFile(a["output"]) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(
                    set(archive.namelist()), set(manifest["files"]) | {"manifest.json"}
                )
                for name, entry in manifest["files"].items():
                    self.assertEqual(
                        hashlib.sha256(archive.read(name)).hexdigest(), entry["sha256"]
                    )
                self.assertFalse(
                    any(
                        name.endswith((".sqlite3", ".jar", ".osm", ".pyc", ".so"))
                        for name in archive.namelist()
                    )
                )
                self.assertIn("route_semantics/方案介绍.md", archive.namelist())
                self.assertIn("route_semantics/DELIVERY.md", archive.namelist())
                self.assertTrue(
                    all(
                        json.loads(line)["synthetic"]
                        for line in archive.read(
                            "route_semantics/examples/synthetic.ndjson"
                        ).splitlines()
                    )
                )

    def test_invalid_output_is_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "source.py"
            with self.assertRaises(ValueError):
                package_delivery.build(output)
            self.assertFalse(output.exists())

    def test_outside_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "package"
            module = root / "route_semantics"
            module.mkdir(parents=True)
            (module / "package_delivery.py").write_text("# placeholder")
            outside = Path(directory) / "outside.py"
            outside.write_text("private")
            (module / "module.py").symlink_to(outside)
            with (
                patch.object(
                    package_delivery, "__file__", str(module / "package_delivery.py")
                ),
                patch.object(package_delivery, "MODULE_FILES", ["module.py"]),
                self.assertRaises(ValueError),
            ):
                package_delivery.build(Path(directory) / "result.zip")
