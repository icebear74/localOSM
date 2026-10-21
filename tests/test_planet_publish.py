"""Exercise the real publisher with tiny files and mocked external OSM tools."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "auto-update/build_karte.sh"
MOCK_TOOL = """#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if name == 'osmium':
    if args[0] == 'fileinfo':
        if os.environ.get('FAIL_FILEINFO'):
            sys.exit(8)
        print(json.dumps({'header': {'generator': 'test', 'osmosis_replication_sequence_number': '42'}}))
    else:
        if args[0] == 'merge' and os.environ.get('FAIL_MERGE'):
            sys.exit(7)
        output = args[args.index('-o') + 1]
        pathlib.Path(output).write_text('new PBF' if args[0] == 'merge' else '{}')
elif name == 'mapshaper':
    pathlib.Path(args[args.index('-o') + 1]).write_text('{"type":"Polygon","coordinates":[]}')
"""


class PlanetPublishTest(unittest.TestCase):
    def test_manual_and_cron_profiles_require_new_publish_protocol_image(self):
        for profile in ("k8s", "k8s/bigmemory"):
            manifest = (ROOT / profile / "planet-update-job.yaml").read_text()
            images = [line.strip().split("image:", 1)[1].strip()
                      for line in manifest.splitlines()
                      if "image:" in line and "planet-update" in line]
            self.assertEqual(len(images), 2)
            self.assertTrue(all(image.endswith(":pelias-source-v1") for image in images))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.conf = self.root / "conf"
        self.status = self.root / "status"
        self.bin = self.root / "bin"
        for directory in (self.work, self.conf, self.status, self.bin):
            directory.mkdir()
        for name in ("mein_europa.geojson", "japan.geojson", "privat.osm", "welt_filter.txt"):
            (self.conf / name).write_text("{}")
        (self.work / "planet-latest.osm.pbf").write_text("test planet")
        self.output = self.work / "europa.osm.pbf"
        self.output.write_text("old PBF")
        tool = self.bin / "mock"
        tool.write_text(MOCK_TOOL)
        tool.chmod(0o755)
        for name in ("osmium", "mapshaper", "pyosmium-up-to-date"):
            (self.bin / name).symlink_to(tool)
        self.env = dict(os.environ, WORK_DIR=str(self.work), CONF_DIR=str(self.conf),
                        STATUS_DIR=str(self.status), PATH=f"{self.bin}:{os.environ['PATH']}")

    def run_update(self, **env):
        return subprocess.run(["bash", str(SCRIPT)], env=dict(self.env, **env),
                              cwd=self.root, capture_output=True, text=True, timeout=20)

    def test_publish_metadata_and_trigger_are_same_input(self):
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.output.read_text(), "new PBF")
        metadata = json.loads(Path(str(self.output) + ".meta.json").read_text())
        self.assertEqual(metadata, json.loads((self.status / "pelias-input.json").read_text()))
        self.assertEqual(metadata["sha256"], hashlib.sha256(b"new PBF").hexdigest())
        self.assertEqual(metadata["source"], "/planet/europa.osm.pbf")
        self.assertEqual(metadata["size"], self.output.stat().st_size)
        self.assertEqual(metadata["mtime_ns"], self.output.stat().st_mtime_ns)
        self.assertEqual(metadata["header"]["generator"], "test")

    def test_failed_merge_preserves_active_source_and_no_trigger(self):
        result = self.run_update(FAIL_MERGE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(), "old PBF")
        self.assertFalse((self.status / "pelias-input.json").exists())
        self.assertFalse((self.work / "europa.next.osm.pbf").exists())

    def test_shared_reader_blocks_publish_without_copying_old_source(self):
        lock_path = self.work / ".pelias-source.lock"
        with lock_path.open("w+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            process = subprocess.Popen(["bash", str(SCRIPT)], env=self.env, cwd=self.root,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while not (self.work / "europa.next.osm.pbf").exists():
                    self.assertIsNone(process.poll())
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                self.assertEqual(self.output.read_text(), "old PBF")
                self.assertIsNone(process.poll())
                fcntl.flock(lock, fcntl.LOCK_UN)
                self.assertEqual(process.wait(timeout=15), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        self.assertEqual(self.output.read_text(), "new PBF")

    def test_invalid_header_does_not_publish_or_trigger(self):
        result = self.run_update(FAIL_FILEINFO="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(), "old PBF")
        self.assertFalse((self.status / "pelias-input.json").exists())
        self.assertFalse((self.work / "europa.next.osm.pbf").exists())

    def test_contending_writer_does_not_unlink_lock_or_remove_build_files(self):
        lock_path = self.work / ".planet-update.lock"
        pending = self.work / "europa.next.osm.pbf"
        pending.write_text("another writer")
        with lock_path.open("w+b") as lock:
            inode = lock_path.stat().st_ino
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertNotEqual(self.run_update().returncode, 0)
            self.assertEqual(lock_path.stat().st_ino, inode)
            self.assertEqual(pending.read_text(), "another writer")


if __name__ == "__main__":
    unittest.main()
