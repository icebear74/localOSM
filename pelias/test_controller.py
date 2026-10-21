import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import signal
import sqlite3
import tempfile
import unittest
from unittest import mock

import controller
import smoke


CASES = [
    {"endpoint": endpoint, "params": {"text": "Berlin"}}
    for endpoint in sorted(smoke.ENDPOINTS)
]


class FakeES:
    def __init__(self):
        self.active = None
        self.calls = []
        self.count = 20

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
            return {"count": self.count}
        if method == "GET":
            return {}
        return {"acknowledged": True}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.policy = {
            "enabled": True, "auto_update": True, "auto_promote": False,
            "min_documents": 10, "retain_generations": 2, "smoke": CASES,
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

    def test_rollback_keeps_previous_generation(self):
        self.record("g1", "retired")
        self.record("g2", "active")
        self.c.state.update(active="g2", last_good="g1")
        self.c.es.active = "pelias-g2"
        self.c.rollback()
        self.assertEqual(self.c.state["active"], "g1")
        self.assertEqual(self.c.state["last_good"], "g2")
        self.assertEqual(self.c.state["generations"]["g2"]["phase"], "retired")

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
            for relative in ("placeholder/store.sqlite3", "interpolation/address.db", "interpolation/street.db"):
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

    def test_publisher_metadata_and_queued_sha_are_verified(self):
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
        with self.assertRaisesRegex(RuntimeError, "publisher metadata"):
            controller.Controller.fingerprint(self.c)

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
        path = self.c.make_config(root, "pelias-g1", aux)
        config = controller.read_json(path)
        for section in ("api", "schema"):
            self.assertEqual(config[section]["indexName"], "pelias-g1")
        self.assertEqual(config["imports"]["openstreetmap"]["datapath"], str(self.root))
        self.assertEqual(config["imports"]["openstreetmap"]["import"], [{"filename": "source.osm.pbf"}])
        self.assertEqual(config["imports"]["whosonfirst"]["countryCode"], ["DE"])
        self.assertEqual(config["api"]["targets"], {"custom": ["venue"]})
        self.assertEqual(config["api"]["services"]["interpolation"]["url"], "http://127.0.0.1:14300")
        self.assertNotIn("elasticsearch", self.c.native)

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

        def module_run(module, argv, env):
            calls.append((module, argv, env))
            if module != "interpolation":
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
            "openstreetmap", "polylines", "interpolation",
        ])
        self.assertEqual(calls[-1][1], ["bash", "-eo", "pipefail", "./script/build.sh"])
        self.assertEqual(calls[-1][2]["PBF2JSON_FILE"], self.args.source)
        self.assertEqual(calls[-1][2]["BUILDDIR"], str(root / "interpolation"))

    def test_processes_forward_signal_to_process_group(self):
        processes = controller.Processes()
        child = mock.Mock(pid=123)
        child.poll.return_value = None
        processes.children.append(child)
        with mock.patch("os.killpg") as kill:
            with self.assertRaises(controller.Cancelled):
                processes.signal(signal.SIGTERM, None)
            kill.assert_called_once_with(123, signal.SIGTERM)


class SmokeTests(unittest.TestCase):
    def test_all_four_endpoint_types_are_required(self):
        smoke.validate_cases(CASES)
        with self.assertRaisesRegex(ValueError, "cover"):
            smoke.validate_cases(CASES[:-1])
        with self.assertRaisesRegex(ValueError, "at least one"):
            smoke.validate_cases([{"endpoint": "search", "params": {}, "expect": {"min_features": 0}}])

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
            "properties": {"label": "Berlin", "layer": "locality", "source": "whosonfirst"},
        }
        with mock.patch("urllib.request.urlopen", return_value=self.response([feature])):
            self.assertEqual(len(smoke.run("http://candidate:14000", CASES)), 4)
            cases = [dict(case, expect={"label_contains": "not berlin"}) for case in CASES]
            with self.assertRaisesRegex(RuntimeError, "label"):
                smoke.run("http://candidate:14000", cases)


if __name__ == "__main__":
    unittest.main()
