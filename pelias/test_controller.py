import argparse
import bz2
import contextlib
import copy
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import tarfile
import tempfile
import unittest
from unittest import mock

import controller
import smoke


CASES = [
    {"endpoint": endpoint, "params": {"text": "Berlin"}, "expect": (
        {"source": "openstreetmap", "layer": "address"} if endpoint == "search"
        else {"source": "openstreetmap"} if endpoint == "autocomplete" else {}
    )}
    for endpoint in sorted(smoke.ENDPOINTS)
]


class FakeES:
    def __init__(self):
        self.active = None
        self.calls = []
        self.count = 20
        self.osm_count = 20

    def target(self, _alias):
        return self.active

    def switch(self, alias, old, new):
        if self.active != old:
            raise RuntimeError("concurrent alias mutation")
        self.calls.append(("SWITCH", old, new))
        self.active = new

    def request(self, method, path, body=None, missing=None):
        self.calls.append((method, path, body))
        if "/_cluster/health/" in path:
            return {"status": "yellow", "timed_out": False}
        if path.endswith("/_count"):
            if body and body.get("query") == {"term": {"source": "openstreetmap"}}:
                return {"count": self.osm_count}
            return {"count": self.count}
        if method == "GET":
            return {}
        return {"acknowledged": True}


class FakeServing:
    label = "generation"

    def __init__(self, generation="none"):
        self.selected = {"app": "pelias", self.label: generation}
        self.calls = []

    def selector(self):
        return dict(self.selected)

    def prepare(self, identifier):
        self.calls.append(("prepare", identifier))
        return "pelias-gen-" + identifier

    def select(self, old, new):
        self.calls.append(("select", old, new))
        if self.selected not in (old, new):
            raise RuntimeError("selector changed")
        self.selected = dict(new)

    def remove(self, name, identifier):
        self.calls.append(("remove", name, identifier))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.policy = {
            "enabled": True, "auto_update": True, "auto_promote": False,
            "min_documents": 10, "min_osm_documents": 10, "retain_generations": 2, "smoke": CASES,
            "aux": {"version": "v1", "countries": ["DE"], "files": [{
                "path": "wof/a.db", "url": "https://example.invalid/a.db",
                "sha256": hashlib.sha256(b"aux").hexdigest(),
            }]},
        }
        self.native = {"esclient": {"hosts": ["http://localhost:9200"]}}
        controller.atomic_json(self.root / "policy.json", self.policy)
        controller.atomic_json(self.root / "config.json", self.native)
        (self.root / "source.osm.pbf").write_bytes(b"pbf")
        (self.root / "source.lock").touch()
        self.args = argparse.Namespace(
            data=str(self.root / "data"), status=str(self.root / "status.json"),
            policy=str(self.root / "policy.json"), config=str(self.root / "config.json"),
            source=str(self.root / "source.osm.pbf"),
            source_lock=str(self.root / "source.lock"), generation=None, manual=False,
            modules="/code",
            scratch=str(self.root / "scratch"), port=4000,
        )
        self.c = controller.Controller(self.args)
        self.c.es = FakeES()
        self.c.state = {
            "active": None, "last_good": None, "building": None, "generations": {},
        }
        self.c.fingerprint = mock.Mock(return_value={"sha256": "source", "size": 3})
        self.c.candidate_smoke = mock.Mock(return_value=[{"features": 1}])

    def record(self, identifier, phase="built"):
        root = self.c.generations / identifier
        root.mkdir(parents=True)
        (root / ".serving.lock").touch()
        config = root / "pelias.json"
        controller.atomic_json(config, {
            section: {"indexName": "pelias-" + identifier}
            for section in ("api", "schema")
        })
        record = {
            "id": identifier, "index": "pelias-" + identifier, "phase": phase,
            "created_at": identifier, "source": self.c.fingerprint(),
            "config_sha256": controller.digest(config),
            "aux_version": "v1",
        }
        self.c.state["generations"][identifier] = record
        return record

    def test_promote_switches_only_after_feature_validation(self):
        self.record("g1")
        self.c.activate("g1", source=self.c.fingerprint())
        self.assertEqual(self.c.state["active"], "g1")
        self.assertEqual(self.c.es.active, "pelias-g1")
        self.c.candidate_smoke.assert_called_once_with("g1")
        self.assertEqual(self.c.es.calls[-1], ("SWITCH", None, "pelias-g1"))

    def test_candidate_failure_does_not_change_serving_alias(self):
        self.record("g1", "active")
        self.record("g2")
        self.c.state["active"] = "g1"
        self.c.es.active = "pelias-g1"
        self.c.candidate_smoke.side_effect = RuntimeError("zero features")
        with self.assertRaisesRegex(RuntimeError, "zero features"):
            self.c.activate("g2")
        self.assertEqual(self.c.es.active, "pelias-g1")
        self.assertEqual(self.c.state["active"], "g1")

    def test_sigterm_during_validation_does_not_promote(self):
        self.record("g1")
        self.c.candidate_smoke.side_effect = controller.Cancelled("SIGTERM")
        with self.assertRaises(controller.Cancelled):
            self.c.activate("g1")
        self.assertIsNone(self.c.es.active)
        self.assertIsNone(self.c.state["active"])

    def test_rejects_wrong_source_and_modified_build_config(self):
        self.record("g1")
        with self.assertRaisesRegex(RuntimeError, "source fingerprint"):
            self.c.activate("g1", source={"sha256": "different"})
        (self.c.generations / "g1" / "pelias.json").write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "configuration differs"):
            self.c.activate("g1")
        self.assertFalse(self.c.es.calls)

    def test_minimum_count_and_alias_drift_fail_closed(self):
        self.record("g1")
        self.c.es.count = 0
        with self.assertRaisesRegex(RuntimeError, "minimum"):
            self.c.activate("g1")
        self.c.es.active = "unknown"
        with self.assertRaisesRegex(RuntimeError, "disagree"):
            self.c.activate("g1")
        self.c.candidate_smoke.assert_not_called()

    def test_wof_document_count_cannot_mask_an_empty_osm_import(self):
        self.record("g1")
        self.c.es.count = 100000
        self.c.es.osm_count = 0
        with self.assertRaisesRegex(RuntimeError, "0 OpenStreetMap documents"):
            self.c.activate("g1")
        self.assertEqual(self.c.generation("g1")["osm_documents"], 0)
        self.assertIsNone(self.c.state["active"])
        self.assertIsNone(self.c.es.active)
        self.c.candidate_smoke.assert_not_called()

    def test_rollback_keeps_previous_generation(self):
        self.record("g1", "retired")
        self.record("g2", "active")
        self.c.state.update(active="g2", last_good="g1")
        self.c.es.active = "pelias-g2"
        self.c.rollback()
        self.assertEqual(self.c.state["active"], "g1")
        self.assertEqual(self.c.state["last_good"], "g2")
        self.assertEqual(self.c.state["generations"]["g2"]["phase"], "retired")

    def test_cluster_activation_prepares_complete_stack_before_alias_and_selector_switch(self):
        self.record("g1")
        self.c.policy["serving"] = {"mode": "kubernetes"}
        self.c.serving = FakeServing()
        original = self.c.serving.select

        def select(old, new):
            self.assertEqual(self.c.es.active, "pelias-g1")
            self.assertIn("pending_activation", self.c.state)
            original(old, new)

        self.c.serving.select = select
        self.c.activate("g1")
        self.assertEqual(self.c.serving.selected["generation"], "g1")
        self.assertEqual(self.c.serving.calls[0], ("prepare", "g1"))
        self.assertEqual(self.c.generation("g1")["serving_deployment"], "pelias-gen-g1")
        self.assertNotIn("pending_activation", self.c.state)

    def test_service_switch_failure_preserves_journal_and_reconciles(self):
        self.record("g1", "active")
        self.record("g2")
        self.c.state["active"] = "g1"
        self.c.es.active = "pelias-g1"
        self.c.policy["serving"] = {"mode": "kubernetes"}
        serving = self.c.serving = FakeServing("g1")
        with mock.patch.object(serving, "select", side_effect=RuntimeError("API unavailable")):
            with self.assertRaisesRegex(RuntimeError, "API unavailable"):
                self.c.activate("g2")
        self.assertEqual(serving.selected["generation"], "g1")
        self.assertEqual(self.c.es.active, "pelias-g2")
        self.assertEqual(self.c.state["active"], "g1")
        self.assertEqual(self.c.state["pending_activation"]["new"], "g2")
        self.c.reconcile()
        self.assertEqual(serving.selected["generation"], "g2")
        self.assertEqual(self.c.state["active"], "g2")
        self.assertEqual(self.c.state["last_good"], "g1")

    def test_cluster_selector_drift_refuses_activation(self):
        self.record("g1", "active")
        self.record("g2")
        self.c.state["active"] = "g1"
        self.c.es.active = "pelias-g1"
        self.c.policy["serving"] = {"mode": "kubernetes"}
        self.c.serving = FakeServing("foreign")
        with self.assertRaisesRegex(RuntimeError, "Service selector"):
            self.c.activate("g2")
        self.assertEqual(self.c.es.active, "pelias-g1")

    def test_cleanup_requires_explicit_unprotected_target_and_retention(self):
        for identifier in ("g1", "g2", "g3", "g4"):
            self.record(identifier)
        self.c.state.update(active="g4", last_good="g3", building="g2")
        self.c.es.active = "pelias-g4"
        for identifier in (None, "g4", "g3", "g2"):
            self.args.generation = identifier
            with self.assertRaises((RuntimeError, ValueError)):
                self.c.cleanup()
        self.args.generation = "g1"
        self.c.cleanup()
        self.assertNotIn("g1", self.c.state["generations"])
        self.assertIn(("DELETE", "/pelias-g1", None), self.c.es.calls)
        self.assertFalse((self.c.generations / "g1").exists())

    def test_cleanup_retains_newest_successful_generations(self):
        self.record("g1")
        self.record("g2")
        self.args.generation = "g1"
        with self.assertRaisesRegex(RuntimeError, "retention"):
            self.c.cleanup()
        self.assertFalse(self.c.es.calls)

    def test_cleanup_protects_draining_serving_stack_even_when_state_has_advanced(self):
        for identifier in ("g1", "g2", "g3"):
            self.record(identifier)
        self.args.generation = "g1"
        with open(self.c.generations / "g1" / ".serving.lock", "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            with self.assertRaisesRegex(RuntimeError, "serving stack"):
                self.c.cleanup()
        self.assertIn("g1", self.c.state["generations"])
        self.assertFalse(self.c.es.calls)

    def test_interrupted_alias_commit_is_reconciled(self):
        self.record("g1", "active")
        self.record("g2")
        self.c.state.update(active="g1", pending_activation={"old": "g1", "new": "g2"})
        self.c.es.active = "pelias-g2"
        self.c.reconcile()
        self.assertEqual(self.c.state["active"], "g2")
        self.assertEqual(self.c.state["last_good"], "g1")
        self.assertNotIn("pending_activation", self.c.state)

    def test_interrupted_precommit_does_not_promote(self):
        self.record("g1", "building")
        self.c.state.update(building="g1", pending_activation={"old": None, "new": "g1"})
        self.c.reconcile()
        self.assertIsNone(self.c.state["active"])
        self.assertEqual(self.c.state["generations"]["g1"]["phase"], "failed")

    def test_disabled_build_does_not_fingerprint_or_download(self):
        self.c.policy["enabled"] = False
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            self.c.build()
        self.c.fingerprint.assert_not_called()

    def prepare_build(self):
        self.args.generation = "g1"
        aux = self.c.data / "aux" / "v1"
        (aux / "wof").mkdir(parents=True)
        (aux / "wof" / "a.db").write_bytes(b"aux")
        controller.atomic_json(aux / "manifest.json", self.policy["aux"])
        self.c.verified_aux = mock.Mock(return_value=aux)

        def config(root, index, _aux):
            path = root / "pelias.json"
            controller.atomic_json(path, {
                section: {"indexName": index} for section in ("api", "schema")
            })
            for relative in (
                "placeholder/store.sqlite3", "interpolation/address.db", "interpolation/street.db",
                "polylines/source.0sv",
            ):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"built")
            return path

        self.c.make_config = mock.Mock(side_effect=config)

    def test_build_holds_readonly_source_lock_and_freezes_complete_index(self):
        self.prepare_build()

        def pipeline(_root, _config):
            with open(self.args.source_lock, "rb") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        self.c.pipeline = mock.Mock(side_effect=pipeline)
        self.c.build()
        self.assertEqual(self.c.generation("g1")["phase"], "built")
        self.assertIsNone(self.c.state["building"])
        self.assertIsNone(self.c.state["active"])
        self.assertIn(("PUT", "/pelias-g1/_settings", {"index.blocks.write": True}), self.c.es.calls)
        with open(self.args.source_lock, "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_cancelled_import_records_failed_candidate_without_alias_change(self):
        self.prepare_build()
        self.c.pipeline = mock.Mock(side_effect=controller.Cancelled("SIGTERM"))
        with self.assertRaises(controller.Cancelled):
            self.c.build()
        persisted = controller.read_json(self.c.state_path)
        self.assertEqual(persisted["generations"]["g1"]["phase"], "failed")
        self.assertIn("SIGTERM", persisted["generations"]["g1"]["error"])
        self.assertIsNone(persisted["active"])
        self.assertIsNone(persisted["building"])
        self.assertIsNone(self.c.es.active)

    def test_import_source_change_rejects_generation(self):
        self.prepare_build()
        self.c.pipeline = mock.Mock()
        self.c.fingerprint.side_effect = [
            {"sha256": "before"}, {"sha256": "after"},
        ]
        with self.assertRaisesRegex(RuntimeError, "source changed"):
            self.c.build()
        self.assertEqual(self.c.generation("g1")["phase"], "failed")
        self.assertIsNone(self.c.es.active)

    def test_actual_source_digest_overrides_stale_publisher_metadata(self):
        path = Path(self.args.source)
        checksum = controller.digest(path)
        metadata = {
            "sha256": checksum, "size": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns, "published_at": "publication",
        }
        controller.atomic_json(str(path) + ".meta.json", metadata)
        self.c.processes.output = mock.Mock(return_value='{"header": {"generator": "osmium"}}')
        self.c.expected_source = checksum
        with self.c.source_locked():
            fingerprint = controller.Controller.fingerprint(self.c)
        self.assertEqual(fingerprint["sha256"], checksum)
        self.assertEqual(fingerprint["published_at"], "publication")
        self.assertEqual(fingerprint["inode"], path.stat().st_ino)
        self.c.expected_source = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "queued SHA256"):
            controller.Controller.fingerprint(self.c)
        self.c.expected_source = None
        metadata["sha256"] = "0" * 64
        controller.atomic_json(str(path) + ".meta.json", metadata)
        fingerprint = controller.Controller.fingerprint(self.c)
        self.assertEqual(fingerprint["sha256"], checksum)
        self.assertEqual(fingerprint["publisher_metadata"], "stale-or-unreadable")
        self.assertNotIn("published_at", fingerprint)

    def test_stale_queued_sha_fails_before_creating_build_record(self):
        self.prepare_build()
        self.c.expected_source = "0" * 64
        self.c.processes.output = mock.Mock(return_value='{"header": {}}')
        self.c.fingerprint = controller.Controller.fingerprint.__get__(self.c)
        self.c.pipeline = mock.Mock()
        controller.atomic_json(self.args.source + ".meta.json", {
            "sha256": "0" * 64, "size": 3,
            "mtime_ns": Path(self.args.source).stat().st_mtime_ns,
        })
        with self.assertRaisesRegex(RuntimeError, "queued SHA256"):
            self.c.build()
        self.c.pipeline.assert_not_called()
        self.assertNotIn("g1", self.c.state["generations"])
        self.assertIsNone(self.c.state["active"])
        self.assertFalse(self.c.es.calls)

    def test_manual_build_can_recover_source_rename_before_sidecar_publish(self):
        self.prepare_build()
        self.c.processes.output = mock.Mock(return_value='{"header": {}}')
        self.c.fingerprint = controller.Controller.fingerprint.__get__(self.c)
        self.c.pipeline = mock.Mock()
        controller.atomic_json(self.args.source + ".meta.json", {
            "sha256": "0" * 64, "size": 3,
            "mtime_ns": Path(self.args.source).stat().st_mtime_ns,
        })
        self.args.manual = True
        self.c.build()
        source = self.c.generation("g1")["source"]
        self.assertEqual(source["sha256"], controller.digest(self.args.source))
        self.assertEqual(source["publisher_metadata"], "stale-or-unreadable")
        self.assertEqual(self.c.generation("g1")["phase"], "built")

    def test_manual_candidate_rejects_wrong_expected_source_sha(self):
        self.record("g1")
        self.c.expected_source = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "expected source SHA256"):
            self.c.activate("g1")
        self.assertIsNone(self.c.es.active)

    def test_failed_auto_promotion_preserves_existing_active_index(self):
        self.prepare_build()
        self.record("g0", "active")
        self.c.state["active"] = "g0"
        self.c.es.active = "pelias-g0"
        self.c.policy["auto_promote"] = True
        self.c.pipeline = mock.Mock()
        self.c.candidate_smoke.side_effect = RuntimeError("failed reverse")
        with self.assertRaisesRegex(RuntimeError, "failed reverse"):
            self.c.build()
        self.assertEqual(self.c.es.active, "pelias-g0")
        self.assertEqual(self.c.state["active"], "g0")
        self.assertEqual(self.c.generation("g1")["phase"], "failed")

    def test_aux_paths_and_tls_are_fail_closed(self):
        item = self.c.policy["aux"]["files"][0]
        for path in ("../escape", "/absolute", ".", "manifest.json"):
            item["path"] = path
            with self.assertRaises(ValueError):
                self.c.aux_spec()
        item["path"] = "good.db"
        item["url"] = "http://insecure.invalid/file"
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            self.c.aux_spec()

    def test_aux_cache_is_verified_and_never_silently_refreshed(self):
        aux = self.c.data / "aux" / "v1"
        (aux / "wof").mkdir(parents=True)
        (aux / "wof" / "a.db").write_bytes(b"aux")
        controller.atomic_json(aux / "manifest.json", self.policy["aux"])
        with mock.patch("urllib.request.urlopen") as request:
            self.c.aux()
            request.assert_not_called()
        (aux / "wof" / "a.db").write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            self.c.aux()

    def test_aux_cache_rejects_unpinned_extra_files(self):
        self.prepare_build()
        aux = self.c.data / "aux" / "v1"
        self.c.verified_aux = controller.Controller.verified_aux.__get__(self.c)
        (aux / "untrusted.db").write_bytes(b"extra")
        with self.assertRaisesRegex(RuntimeError, "unpinned"):
            self.c.verified_aux()

    def test_explicit_aux_verifies_compressed_and_expanded_checksums(self):
        compressed = bz2.compress(b"aux")
        item = self.c.policy["aux"]["files"][0]
        item.update(
            compression="bz2", sha256=hashlib.sha256(compressed).hexdigest(),
            content_sha256=hashlib.sha256(b"aux").hexdigest(),
        )
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(compressed)
        with mock.patch("urllib.request.urlopen", return_value=response):
            self.c.aux()
        aux = self.c.verified_aux()
        self.assertEqual((aux / "wof" / "a.db").read_bytes(), b"aux")
        self.assertFalse(aux.with_name(aux.name + ".download").exists())
        self.assertFalse(aux.with_name(aux.name + ".next").exists())

    def test_bad_expanded_aux_checksum_is_never_published(self):
        compressed = bz2.compress(b"aux")
        item = self.c.policy["aux"]["files"][0]
        item.update(
            compression="bz2", sha256=hashlib.sha256(compressed).hexdigest(),
            content_sha256="0" * 64,
        )
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(compressed)
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "expanded checksum"):
                self.c.aux()
        aux = self.c.data / "aux" / "v1"
        self.assertFalse(aux.exists())
        self.assertFalse(aux.with_name(aux.name + ".download").exists())
        self.assertFalse(aux.with_name(aux.name + ".next").exists())

    def automatic_aux_fixture(self):
        self.c.policy["aux"]["files"] = []
        path = self.root / "official-inventory-fixture.db"
        with sqlite3.connect(path) as database:
            database.execute("CREATE TABLE geojson (id INTEGER, body TEXT)")
            database.execute("CREATE TABLE spr (id INTEGER)")
        content = path.read_bytes()
        entry = {
            "name": "whosonfirst-data-admin-de.db",
            "name_compressed": "whosonfirst-data-admin-de-latest.db.bz2",
            "last_modified": "2026-01-01T00:00:00Z",
        }
        return content, entry

    def downloaded(self, content):
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(content)
        return response

    def test_official_inventory_aux_resolves_countries_captures_hashes_and_never_refreshes(self):
        content, entry = self.automatic_aux_fixture()
        compressed = bz2.compress(content)
        inventory = json.dumps([
            dict(entry, name_compressed="whosonfirst-data-admin-de-latest.db.tar.bz2",
                 last_modified="2025-01-01T00:00:00Z"),
            entry,
            dict(entry, name="whosonfirst-data-admin-us.db",
                 name_compressed="whosonfirst-data-admin-us-latest.db.bz2"),
            dict(entry, name="whosonfirst-data-postalcode-de.db",
                 name_compressed="whosonfirst-data-postalcode-de-latest.db.bz2"),
        ]).encode()
        with mock.patch("urllib.request.urlopen", side_effect=[
            self.downloaded(inventory), self.downloaded(compressed),
        ]) as request:
            self.c.aux()
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            request.call_args_list[0].args[0],
            "https://data.geocode.earth/wof/dist/sqlite/inventory.json",
        )
        aux = self.c.verified_aux()
        manifest = controller.read_json(aux / "manifest.json")
        self.assertEqual(manifest["policy"]["files"], [])
        self.assertEqual(manifest["inventory"]["sha256"], hashlib.sha256(inventory).hexdigest())
        self.assertEqual(manifest["inventory"]["selected"], [entry])
        self.assertEqual(manifest["files"][0]["sha256"], hashlib.sha256(compressed).hexdigest())
        self.assertEqual(manifest["files"][0]["content_sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual((aux / manifest["files"][0]["path"]).read_bytes(), content)
        with mock.patch("urllib.request.urlopen") as request:
            self.c.aux()
            request.assert_not_called()
        self.c.native["imports"] = {"whosonfirst": {"importPostalcodes": True}}
        with self.assertRaisesRegex(RuntimeError, "selection changed"):
            self.c.verified_aux()

    def test_official_inventory_requires_every_requested_country(self):
        _content, entry = self.automatic_aux_fixture()
        self.c.policy["aux"]["countries"] = ["DE", "JP"]
        inventory = json.dumps([entry]).encode()
        with mock.patch("urllib.request.urlopen", return_value=self.downloaded(inventory)):
            with self.assertRaisesRegex(RuntimeError, "no admin data for JP"):
                self.c.aux()
        self.assertFalse((self.c.data / "aux" / "v1").exists())

    def test_official_inventory_tar_bz2_is_streamed_without_extracting_other_members(self):
        content, entry = self.automatic_aux_fixture()
        entry["name_compressed"] = "whosonfirst-data-admin-de-latest.db.tar.bz2"
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:bz2") as archive:
            member = tarfile.TarInfo("nested/" + entry["name"])
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
            unrelated = tarfile.TarInfo("../unselected")
            unrelated.size = 3
            archive.addfile(unrelated, io.BytesIO(b"bad"))
        with mock.patch("urllib.request.urlopen", side_effect=[
            self.downloaded(json.dumps([entry]).encode()), self.downloaded(output.getvalue()),
        ]):
            self.c.aux()
        aux = self.c.verified_aux()
        self.assertEqual((aux / "whosonfirst" / "sqlite" / entry["name"]).read_bytes(), content)
        self.assertFalse((aux.parent / "unselected").exists())

    def test_official_inventory_cannot_escape_cache_paths(self):
        _content, entry = self.automatic_aux_fixture()
        entry["name"] = "../escaped.db"
        with mock.patch("urllib.request.urlopen", return_value=self.downloaded(json.dumps([entry]).encode())):
            with self.assertRaisesRegex(RuntimeError, "unsafe SQLite filename"):
                self.c.aux()
        self.assertFalse((self.c.data / "aux" / "escaped.db").exists())

    def test_generation_native_config_isolated_and_keeps_csv_api_targets(self):
        self.record("g1")
        root = self.c.generations / "g1"
        aux = self.c.data / "aux" / "v1"
        for relative in ("whosonfirst/sqlite/whosonfirst-data-admin-de.db", "polylines/all.0sv", "csv/pois.csv"):
            path = aux / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        self.c.native["imports"] = {"csv": {"files": ["pois.csv"]}}
        self.c.native["api"] = {"targets": {"custom": ["venue"]}}
        self.c.native["elasticsearch"] = {
            "indexName": "ignored-old-value", "settings": {"index": {"number_of_shards": 1}},
        }
        self.c.native["schema"] = {"typeName": "guessed-type", "indexName": "old-schema"}
        path = self.c.make_config(root, "pelias-g1", aux)
        config = controller.read_json(path)
        for section in ("api", "schema"):
            self.assertEqual(config[section]["indexName"], "pelias-g1")
        self.assertEqual(config["imports"]["openstreetmap"]["datapath"], str(self.root))
        self.assertEqual(config["imports"]["openstreetmap"]["import"], [{"filename": "source.osm.pbf"}])
        self.assertEqual(config["imports"]["whosonfirst"]["countryCode"], ["DE"])
        self.assertEqual(config["api"]["targets"], {"custom": ["venue"]})
        self.assertEqual(config["api"]["services"]["interpolation"]["url"], "http://127.0.0.1:14300")
        self.assertEqual(config["services"]["pip"]["url"], "http://127.0.0.1:14200")
        self.assertNotIn("indexName", config["elasticsearch"])
        self.assertNotIn("typeName", config["schema"])
        self.assertEqual(config["elasticsearch"]["settings"]["index"]["number_of_shards"], 1)
        self.assertEqual(self.c.native["elasticsearch"]["indexName"], "ignored-old-value")

    def test_pipeline_runs_real_upstream_entrypoints_and_validates_databases(self):
        self.record("g1")
        root = self.c.generations / "g1"
        aux = self.c.data / "aux" / "v1"
        for relative in ("whosonfirst/sqlite/whosonfirst-data-admin-de.db", "polylines/all.0sv"):
            path = aux / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        config = self.c.make_config(root, "pelias-g1", aux)
        calls = []
        self.c.processes.run = mock.Mock(
            side_effect=lambda argv, **kwargs: Path(argv[3]).write_bytes(b"real-source-geometries"),
        )
        pip = mock.Mock()
        pip.poll.return_value = None
        self.c.processes.start = mock.Mock(return_value=pip)
        self.c.processes.stop = mock.Mock()
        self.c.wait_port = mock.Mock()

        def module_run(module, argv, env):
            calls.append((module, argv, env))
            if module != "interpolation" or not argv[-1].endswith("/vertices.sh"):
                return
            for relative, table in (
                ("placeholder/store.sqlite3", "docs"),
                ("interpolation/address.db", "address"), ("interpolation/street.db", "polyline"),
            ):
                with sqlite3.connect(root / relative) as database:
                    database.execute("CREATE TABLE " + table + " (id INTEGER)")
                    database.execute("INSERT INTO " + table + " VALUES (1)")

        self.c.module_run = mock.Mock(side_effect=module_run)
        self.c.pipeline(root, config)
        self.assertEqual([call[0] for call in calls], [
            "schema", "placeholder", "placeholder", "whosonfirst",
            "openstreetmap", "polylines", "interpolation", "interpolation", "interpolation",
        ])
        self.assertEqual([call[1][-1] for call in calls[-3:]], [
            "./script/import.sh", "./script/conflate_osm.sh", "./script/vertices.sh",
        ])
        self.assertEqual(calls[-1][2]["PBF2JSON_FILE"], self.args.source)
        self.assertEqual(calls[-1][2]["BUILDDIR"], str(root / "interpolation"))
        self.assertEqual(calls[-1][2]["POLYLINE_FILE"], str(root / "polylines" / "source.0sv"))
        self.assertEqual(
            calls[-1][2]["PBF2JSON_TMPDIR"], str(root / "scratch" / "interpolation-leveldb"),
        )
        self.assertTrue(self.c.processes.run.call_args.args[0][1].endswith("/streets.py"))
        self.c.wait_port.assert_called_once_with(14200, [pip])
        self.c.processes.stop.assert_called_once_with(pip)

    def test_processes_forward_signal_to_process_group(self):
        processes = controller.Processes()
        child = mock.Mock(pid=123)
        child.poll.return_value = None
        processes.children.append(child)
        with mock.patch("os.killpg") as kill:
            with self.assertRaises(controller.Cancelled):
                processes.signal(signal.SIGTERM, None)
            kill.assert_called_once_with(123, signal.SIGTERM)

    def test_serving_stack_pins_index_uses_config_copy_and_holds_readonly_lock(self):
        record = self.record("g1")
        root = self.c.generations / "g1"
        config = root / "pelias.json"
        controller.atomic_json(config, {
            "api": {"indexName": "pelias-g1", "services": {
                service: {"url": "http://candidate"}
                for service in ("placeholder", "pip", "interpolation")
            }},
        })
        record["config_sha256"] = controller.digest(config)
        original = config.read_bytes()
        self.c.save()
        self.args.generation = "g1"
        self.c.probe = mock.Mock()
        children = [mock.Mock()]
        children[0].poll.return_value = None

        @contextlib.contextmanager
        def stack(stack_root, env, port, helpers, public=False):
            self.assertEqual(stack_root, root)
            self.assertEqual(port, 4000)
            self.assertEqual(helpers, (4100, 4200, 4300))
            self.assertTrue(public)
            self.assertEqual(env["TMPDIR"], self.args.scratch)
            self.assertEqual(env["SQLITE_TMPDIR"], self.args.scratch)
            self.assertEqual(env["PBF2JSON_TMPDIR"], str(Path(self.args.scratch) / "pbf2json"))
            with open(root / ".serving.lock", "rb") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield children

        self.c.stack = stack
        with mock.patch("time.sleep", side_effect=controller.Cancelled("shutdown")):
            with self.assertRaises(controller.Cancelled):
                self.c.serve()
        self.assertEqual(config.read_bytes(), original)
        served = controller.read_json(Path(self.args.scratch) / "pelias.json")
        self.assertEqual(served["api"]["indexName"], "pelias-g1")
        self.assertEqual(served["api"]["services"]["interpolation"]["url"], "http://127.0.0.1:4300")
        self.assertEqual(served["services"]["pip"]["url"], "http://127.0.0.1:4200")
        self.assertFalse((self.c.data / ".lifecycle.lock").exists())


class KubernetesTests(unittest.TestCase):
    def test_default_selector_label_matches_generation_template_manifests(self):
        with mock.patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": "127.0.0.1"}):
            with mock.patch("ssl.create_default_context"):
                kube = controller.Kubernetes({"namespace": "osm"})
        self.assertEqual(kube.label, "generation")

    def test_projected_service_account_token_is_reread_after_long_imports(self):
        kube = controller.Kubernetes.__new__(controller.Kubernetes)
        kube.url = "https://kubernetes.invalid"
        kube.context = object()
        kube.token_path = mock.Mock()
        kube.token_path.read_text.side_effect = ["first", "rotated"]
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        with mock.patch("urllib.request.urlopen", return_value=response) as request:
            kube.request("GET", "/first")
            kube.request("GET", "/second")
        self.assertTrue(request.call_args_list[0].args[0].get_header("Authorization").endswith("first"))
        self.assertTrue(request.call_args_list[1].args[0].get_header("Authorization").endswith("rotated"))

    def test_generation_deployment_is_cloned_with_unique_selector_and_pinned_command(self):
        kube = controller.Kubernetes.__new__(controller.Kubernetes)
        kube.policy = {}
        kube.label = "generation"
        kube.deployments = "/deployments"
        template = {
            "spec": {"replicas": 0, "selector": {"matchLabels": {"app": "pelias"}},
                     "template": {"metadata": {"labels": {"app": "pelias"}},
                                  "spec": {"containers": [{"name": "pelias", "args": ["ignored"]}]}}},
        }
        untouched = copy.deepcopy(template)
        ready = {
            "metadata": {"generation": 1},
            "status": {"observedGeneration": 1, "updatedReplicas": 1,
                       "readyReplicas": 1, "availableReplicas": 1},
        }
        kube.request = mock.Mock(side_effect=[{}, template, {}, ready])
        self.assertEqual(kube.prepare("g1"), "pelias-gen-g1")
        body = kube.request.call_args_list[2].args[2]
        self.assertEqual(body["spec"]["replicas"], 1)
        self.assertEqual(body["spec"]["selector"]["matchLabels"]["generation"], "g1")
        container = body["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], [
            "python3", "/runtime/controller.py", "serve",
        ])
        self.assertNotIn("args", container)
        self.assertIn({"name": "PELIAS_GENERATION", "value": "g1"}, container["env"])
        self.assertEqual(template, untouched)

    def test_generation_clone_preserves_ca_bootstrap_and_policy_arguments(self):
        kube = controller.Kubernetes.__new__(controller.Kubernetes)
        kube.policy = {}
        kube.label = "generation"
        kube.deployments = "/deployments"
        bootstrap = "prepare-ca; exec python3 /runtime/controller.py serve --policy /policy/policy.json"
        template = {
            "spec": {"selector": {"matchLabels": {"app": "pelias"}},
                     "template": {"metadata": {"labels": {"app": "pelias"}},
                                  "spec": {"containers": [{
                                      "name": "pelias", "command": ["sh", "-ec"], "args": [bootstrap],
                                  }]}}},
        }
        ready = {"metadata": {"generation": 1}, "status": {
            "observedGeneration": 1, "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1,
        }}
        kube.request = mock.Mock(side_effect=[{}, template, {}, ready])
        kube.prepare("g1")
        container = kube.request.call_args_list[2].args[2]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], ["sh", "-ec"])
        self.assertEqual(container["args"], [bootstrap])

    def test_service_patch_has_resource_version_and_preserves_other_selector_labels(self):
        kube = controller.Kubernetes.__new__(controller.Kubernetes)
        kube.service_path = "/service"
        old = {"app": "pelias", "generation": "g1"}
        new = dict(old, **{"generation": "g2"})
        kube.request = mock.Mock(side_effect=[{
            "metadata": {"resourceVersion": "42"}, "spec": {"selector": old},
        }, {}])
        kube.select(old, new)
        body = kube.request.call_args_list[1].args[2]
        self.assertEqual(body["metadata"]["resourceVersion"], "42")
        self.assertEqual(body["spec"]["selector"], new)


class SmokeTests(unittest.TestCase):
    def test_all_four_endpoint_types_are_required(self):
        smoke.validate_cases(CASES)
        with self.assertRaisesRegex(ValueError, "cover"):
            smoke.validate_cases(CASES[:-1])
        with self.assertRaisesRegex(ValueError, "at least one"):
            smoke.validate_cases([{"endpoint": "search", "params": {}, "expect": {"min_features": 0}}])

    def test_wof_only_city_smokes_are_rejected(self):
        cases = [dict(case, expect={"source": "whosonfirst", "layer": "locality"}) for case in CASES]
        with self.assertRaisesRegex(ValueError, "search and autocomplete"):
            smoke.validate_cases(cases)
        cases = [dict(case, expect={"source": "openstreetmap", "layer": "street"}) for case in CASES]
        with self.assertRaisesRegex(ValueError, "address or venue"):
            smoke.validate_cases(cases)

    def response(self, features):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "type": "FeatureCollection", "features": features,
        }).encode()
        return response

    def test_empty_http_200_is_not_a_successful_smoke(self):
        with mock.patch("urllib.request.urlopen", return_value=self.response([])):
            with self.assertRaisesRegex(RuntimeError, "too few"):
                smoke.run("http://candidate:14000", CASES)

    def test_real_features_and_expected_semantics_are_checked(self):
        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [13.4, 52.5]},
            "properties": {"label": "Berlin", "layer": "address", "source": "openstreetmap"},
        }
        with mock.patch("urllib.request.urlopen", return_value=self.response([feature])):
            self.assertEqual(len(smoke.run("http://candidate:14000", CASES)), 4)
            cases = [
                dict(case, expect=dict(case["expect"], label_contains="not berlin")) for case in CASES
            ]
            with self.assertRaisesRegex(RuntimeError, "label"):
                smoke.run("http://candidate:14000", cases)

    def test_source_layer_and_label_must_match_the_same_feature(self):
        features = [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [13.4, 52.5]},
             "properties": {"label": "Berlin", "layer": "venue", "source": "openstreetmap"}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [13.4, 52.5]},
             "properties": {"label": "Berlin", "layer": "address", "source": "whosonfirst"}},
        ]
        with mock.patch("urllib.request.urlopen", return_value=self.response(features)):
            with self.assertRaisesRegex(RuntimeError, "one feature"):
                smoke.run("http://candidate:14000", CASES)


if __name__ == "__main__":
    unittest.main()
