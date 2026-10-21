"""Exercise the real status functions and sourced orchestration entry points."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("osm_status", ROOT / "status/app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class PeliasIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / "tests" / (".pelias-test-" + uuid.uuid4().hex)
        self.work.mkdir()
        self.addCleanup(shutil.rmtree, self.work)
        self.state = self.work / "state"
        self.state.mkdir()
        self.env = dict(os.environ, OSM_STATE_DIR=str(self.state),
                        OSM_DATA_DIR=str(self.work / "data"),
                        OSM_TEMP_DIR=str(self.work / "scratch"),
                        OSM_MANIFEST_DIR=str(ROOT / "k8s"))
        self.policy = {"enabled": True, "auto_update": True, "auto_promote": False}
        for name, value in {
            "STATUS_DIR": self.state,
            "IMPORT_REQUEST_FILE": self.state / "import-request.json",
            "IMPORT_REQUEST_QUEUE_FILE": self.state / "import-request-queue.json",
            "IMPORT_REQUEST_LOCK_FILE": self.state / "import-request.lock",
            "PELIAS_STATE_FILE": self.state / "pelias.json",
            "ABORT_FLAG_FILE": self.state / "import-abort.flag",
        }.items():
            p = patch.object(app, name, str(value))
            p.start()
            self.addCleanup(p.stop)

    def shell(self, code, policy=None):
        policy = self.policy if policy is None else policy
        cm = json.dumps({"data": {"policy.json": json.dumps(policy)}})
        script = """
source scripts/import-orchestrator.sh
kubectl() {
  if [[ "$*" == *"get configmap osm-pelias-policy"* ]]; then
    printf '%s\\n' "$TEST_POLICY"
  else
    printf 'Unexpected kubectl call: %s\\n' "$*" >&2
    return 1
  fi
}
""" + code
        result = subprocess.run(["bash", "-c", script], cwd=ROOT,
                                env=dict(self.env, TEST_POLICY=cm),
                                text=True, capture_output=True)
        return result

    def request(self, **kwargs):
        (self.state / "import-request.json").write_text(json.dumps(kwargs))

    def test_default_steps_omit_disabled_and_explicit_request_fails(self):
        self.request()
        result = self.shell("request_steps", {"enabled": False})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "tileserver nominatim valhalla photon")
        self.request(steps=["pelias"])
        result = self.shell("request_steps", {"enabled": False})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Pelias is disabled", result.stderr)

    def test_published_trigger_is_gated_deduplicated_and_persisted(self):
        version = {"sha256": "a" * 64, "size": 8192, "mtime_ns": 42,
                   "header": {}, "published_at": "2026-10-21", "source": "/planet/europa.osm.pbf"}
        (self.state / "pelias-input.json").write_text(json.dumps(version))
        result = self.shell("poll_pelias_input", {"enabled": True, "auto_update": False})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.state / "import-request-queue.json").exists())
        result = self.shell("poll_pelias_input; poll_pelias_input")
        self.assertEqual(result.returncode, 0, result.stderr)
        queue = app.load_import_request_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["input_version"], version)
        self.assertEqual(queue[0]["steps"], ["pelias"])
        self.assertFalse(queue[0]["auto_promote"])
        # Losing/archiving a failed request must not create an infinite retry loop.
        app.save_import_request_queue([])
        result = self.shell("poll_pelias_input")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(app.load_import_request_queue(), [])

    def test_status_pelias_build_needs_no_library_and_does_not_merge(self):
        with patch.object(app, "pelias_policy", return_value=self.policy), \
             patch.object(app, "list_library_records", side_effect=AssertionError("library accessed")):
            result = app.start_step_build_workflow("pelias")
        self.assertEqual(result, {"step": "pelias", "count": 0})
        request = app.load_import_request_queue()[0]
        self.assertEqual(request["countries"], [])
        self.assertFalse(request["merge_requested"])
        self.assertFalse(request["auto_promote"])

    def test_status_full_default_and_disabled_explicit_request(self):
        with patch.object(app, "pelias_policy", return_value={"enabled": False}):
            request = app.write_import_request([])
            self.assertNotIn("pelias", request["steps"])
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                app.start_step_build_workflow("pelias")

    def test_status_reports_real_generation_and_explicit_actions(self):
        state = {"active": "old", "last_good": "older", "generations": {
            "old": {"id": "old", "phase": "active", "source": {"sha256": "b" * 64}},
            "new": {"id": "new", "phase": "built", "built_at": "2026-10-21"},
        }}
        Path(app.PELIAS_STATE_FILE).write_text(json.dumps(state))
        with patch.object(app, "pelias_policy", return_value=self.policy):
            status = app.pelias_status()
            self.assertEqual(status["candidate"]["id"], "new")
            self.assertEqual(status["generations"]["old"]["source"]["sha256"], "b" * 64)
            app.queue_pelias_action("promote", "new")
            app.queue_pelias_action("rollback", "older")
            with self.assertRaises(ValueError):
                app.queue_pelias_action("rollback", "")
            with self.assertRaises(ValueError):
                app.queue_pelias_action("promote", "Invalid_Generation")
            with self.assertRaises(ValueError):
                app.queue_pelias_action("promote", "g" * 64)
            for invalid in ("trailing-", "g-"):
                with self.assertRaises(ValueError):
                    app.queue_pelias_action("promote", invalid)
        queue = app.load_import_request_queue()
        self.assertEqual([(r["action"], r["generation"]) for r in queue],
                         [("promote", "new"), ("rollback", "older")])

    def test_main_pelias_only_bypasses_prep_and_generic_promotion(self):
        self.request(steps=["pelias"], countries=[{"slug": "must-not-download"}],
                     auto_promote=True)
        result = self.shell("""
check_config_change() { :; }
poll_pelias_input() { :; }
prepare_import_data() { echo 'UNEXPECTED preparation'; return 1; }
run_import_promotion_job() { echo 'UNEXPECTED generic promotion'; return 1; }
run_pelias_step() { echo 'PELIAS JOB'; }
sleep() { exit 0; }
main
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PELIAS JOB", result.stdout)
        self.assertNotIn("UNEXPECTED", result.stdout)
        self.assertFalse((self.state / "import-request.json").exists())

    def test_pelias_job_propagates_expected_digest_and_manual_flag(self):
        # kubectl client rendering is replaced, but the real function mutates/submits it.
        template = {"metadata": {"name": "pelias-import"}, "spec": {"template": {"spec": {
            "containers": [{"name": "import", "args": [
                "exec python3 /runtime/controller.py build --policy /policy/policy.json\\n"],
                "env": []}]}}}}
        fixture = self.work / "job.json"
        fixture.write_text(json.dumps(template))
        self.env["TEST_JOB"] = str(fixture)
        self.request(steps=["pelias"], automatic=True, pelias_source_sha256="c" * 64)
        result = self.shell("""
kubectl() { cat "$TEST_JOB"; }
pelias_policy() { echo '{"enabled":true,"auto_update":true}'; }
submit_pelias_job() { cat > "$OSM_STATE_DIR/submitted.json"; }
wait_for_job() { :; }
sync_completed_steps
AUTO_PROMOTE=false
run_pelias_step
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        job = json.loads((self.state / "submitted.json").read_text())
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertIn({"name": "PELIAS_EXPECTED_SOURCE_SHA256", "value": "c" * 64}, container["env"])
        env = {item["name"]: item["value"] for item in container["env"]}
        self.assertEqual(env["PELIAS_AUTOMATIC"], "true")
        self.assertTrue(env["PELIAS_GENERATION"].startswith("osm-"))
        self.assertTrue((self.state / "import-completed-steps").read_text().endswith("pelias-build\n"))

    def test_rollback_submits_targeted_job_not_a_build(self):
        template = {"metadata": {"name": "pelias-promote"}, "spec": {"template": {"spec": {
            "containers": [{"name": "promote", "args": [
                'exec python3 /runtime/controller.py rollback --policy /policy/policy.json'],
                "env": [{"name": "PELIAS_ACTION", "value": "promote"},
                        {"name": "PELIAS_GENERATION", "value": ""}]}]}}}}
        fixture = self.work / "job.json"
        fixture.write_text(json.dumps(template))
        self.env["TEST_JOB"] = str(fixture)
        self.request(steps=["pelias"], action="rollback", generation="previous-good")
        result = self.shell("""
kubectl() { cat "$TEST_JOB"; }
pelias_policy() { echo '{"enabled":true}'; }
submit_pelias_job() { cat > "$OSM_STATE_DIR/submitted.json"; }
wait_for_job() { echo "WAIT $1"; }
run_pelias_step
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        job = json.loads((self.state / "submitted.json").read_text())
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertIn({"name": "PELIAS_AUTOMATIC", "value": "false"}, container["env"])
        self.assertIn({"name": "PELIAS_ACTION", "value": "rollback"}, container["env"])
        self.assertIn({"name": "PELIAS_GENERATION", "value": "previous-good"}, container["env"])
        self.assertIn("WAIT pelias-promote", result.stdout)

    def test_generation_label_length_boundary_in_status_and_shell(self):
        with patch.object(app, "pelias_policy", return_value=self.policy):
            request = app.queue_pelias_action("promote", "g" * 63)
            self.assertEqual(request["generation"], "g" * 63)
            with self.assertRaises(ValueError):
                app.queue_pelias_action("promote", "g" * 64)
        for invalid in ("g" * 64, "g-"):
            self.request(steps=["pelias"], action="promote", generation=invalid)
            result = self.shell("""
kubectl() { echo 'UNEXPECTED JOB RENDER'; return 1; }
pelias_policy() { echo '{"enabled":true}'; }
run_pelias_step
""")
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("UNEXPECTED JOB RENDER", result.stdout)

    def test_resume_reuses_identical_job_without_deleting_it(self):
        result = self.shell("""
kubectl() {
  if [[ "$*" == *"get job"* ]]; then cat "$OSM_STATE_DIR/submitted.json"
  elif [[ "$*" == *"apply -f -"* ]]; then cat > "$OSM_STATE_DIR/submitted.json"
  elif [[ "$*" == *"delete job"* ]]; then echo DELETE >> "$OSM_STATE_DIR/deletions"
  else return 1
  fi
}
echo '{}' > "$OSM_STATE_DIR/submitted.json"
job='{"metadata":{"name":"pelias-import"},"spec":{"template":{"spec":{"containers":[]}}}}'
printf '%s' "$job" | submit_pelias_job pelias-import
printf '%s' "$job" | submit_pelias_job pelias-import
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.state / "deletions").read_text().splitlines(), ["DELETE"])

    def test_actual_job_shells_consume_generation_action_and_manual_mode(self):
        smoke_spec = importlib.util.spec_from_file_location("pelias_smoke", ROOT / "pelias/smoke.py")
        smoke = importlib.util.module_from_spec(smoke_spec)
        smoke_spec.loader.exec_module(smoke)
        controller_spec = importlib.util.spec_from_file_location("pelias_cli", ROOT / "pelias/controller.py")
        controller = importlib.util.module_from_spec(controller_spec)
        with patch.dict(sys.modules, {"smoke": smoke}):
            controller_spec.loader.exec_module(controller)
        class CapturedArgs(Exception):
            pass
        bindir = self.work / "commands"
        bindir.mkdir()
        for name, body in {"cp": "exit 0", "python3": '[ "$1" = "-c" ] && exit 0\nprintf "%s\\n" "$@"'}.items():
            command = bindir / name
            command.write_text("#!/bin/sh\n" + body + "\n")
            command.chmod(0o755)
        for profile in ("k8s", "k8s/bigmemory"):
            for action in ("build", "promote", "rollback"):
                manifest = "pelias-import-job.yaml" if action == "build" else "pelias-promote-job.yaml"
                job = yaml.safe_load((ROOT / profile / manifest).read_text())
                container = job["spec"]["template"]["spec"]["containers"][0]
                for automatic in ("false", "true"):
                    with self.subTest(profile=profile, action=action, automatic=automatic):
                        env = dict(self.env, PATH=str(bindir) + ":" + os.environ["PATH"],
                                   PELIAS_GENERATION="target-generation", PELIAS_ACTION=action,
                                   PELIAS_AUTOMATIC=automatic)
                        result = subprocess.run(container["command"] + container["args"],
                                                env=env, cwd=ROOT, capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        args = result.stdout.splitlines()
                        self.assertEqual(args[:2], ["/runtime/controller.py", action])
                        self.assertEqual(args[args.index("--policy") + 1], "/policy/policy.json")
                        captured = []
                        def capture(parsed):
                            captured.append(parsed)
                            raise CapturedArgs
                        with patch.dict(os.environ, env), patch.object(controller, "Controller", side_effect=capture):
                            with self.assertRaises(CapturedArgs):
                                controller.main(args[1:])
                        self.assertEqual(captured[0].generation, "target-generation")
                        if action == "build":
                            self.assertEqual("--manual" in args, automatic == "false")

    def test_run_import_cli_pelias_only_queues_without_download(self):
        result = subprocess.run(["bash", "scripts/run-import.sh", "--pelias-only"],
                                env=dict(self.env, OSM_DATA_DIR=str(self.work)),
                                cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        request = json.loads((self.work / "status/import-request-queue.json").read_text())[0]
        self.assertEqual(request["steps"], ["pelias"])
        self.assertEqual(request["countries"], [])

    def test_deploy_pelias_only_applies_no_jobs_or_service_shutdown(self):
        bindir = self.work / "bin"
        bindir.mkdir()
        sudo = bindir / "sudo"
        sudo.write_text('#!/bin/sh\nexec "$@"\n')
        sudo.chmod(0o755)
        fake = bindir / "kubectl"
        fake.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
import yaml
args = sys.argv[1:]
with open(os.environ["TEST_KUBE_LOG"], "a") as f:
    f.write(json.dumps(args) + "\\n")
if "get" in args and "configmap" in args:
    print(json.dumps({"data": {"policy.json": '{"enabled":true,"auto_update":true}'}}))
elif "get" in args and "nodes" in args:
    nodes = [{"metadata":{"name":"node-"+str(i),"labels":{"osm":"true","kubernetes.io/hostname":"node-"+str(i)}},
              "spec":{}, "status":{"conditions":[{"type":"Ready","status":"True"}]}}
             for i in range(1, int(os.environ.get("TEST_NODE_COUNT", "1")) + 1)]
    print(json.dumps({"items":nodes}))
elif "get" in args and "service" in args:
    path = Path(os.environ["TEST_SERVICE_STATE"])
    if path.exists():
        print(path.read_text())
elif "get" in args and "jobs" in args:
    items = [{"metadata":{"name":"pelias-import"},"status":{"active":1}}] if os.environ.get("TEST_ACTIVE_JOBS") else []
    print(json.dumps({"items":items}))
elif "exec" in args and os.environ.get("TEST_IMPORT_RUNNING"):
    sys.exit("An import is running or queued.")
elif "create" in args and "--dry-run=client" in args and "-f" in args:
    source = args[args.index("-f") + 1]
    raw = sys.stdin.read() if source == "-" else Path(source).read_text()
    documents = []
    for doc in yaml.safe_load_all(raw):
        documents.extend(doc["items"] if doc.get("kind") == "List" else [doc])
    print(json.dumps(documents[0] if len(documents) == 1 else {"apiVersion":"v1","kind":"List","items":documents}))
elif "apply" in args and args[-1] == "-":
    raw = sys.stdin.read()
    if raw:
        obj = json.loads(raw)
        items = obj.get("items", [obj])
        with open(os.environ["TEST_KUBE_APPLIED"], "a") as f:
            for item in items:
                f.write(json.dumps(item) + "\\n")
                if item.get("kind") == "Service" and item["metadata"]["name"] == "pelias":
                    Path(os.environ["TEST_SERVICE_STATE"]).write_text(json.dumps(item))
""")
        fake.chmod(0o755)
        log = self.work / "kubectl.log"
        applied = self.work / "applied.jsonl"
        service_state = self.work / "live-service.json"
        self.env.update(TEST_SERVICE_STATE=str(service_state), TEST_KUBE_APPLIED=str(applied))
        result = subprocess.run(["bash", "scripts/deploy-osm.sh", "--pelias-only"],
                                cwd=ROOT, text=True, capture_output=True,
                                env=dict(self.env, PATH=str(bindir) + ":" + os.environ["PATH"],
                                         TEST_KUBE_LOG=str(log)))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertFalse(any("scale" in args or "delete" in args for args in calls))
        applied_paths = [args[-1] for args in calls if "apply" in args and args[-1] != "-"]
        self.assertTrue(any(path.endswith("pelias-elasticsearch.yaml") for path in applied_paths))
        self.assertFalse(any("job.yaml" in path for path in applied_paths))
        self.assertTrue(any("osm-pelias-runtime" in args for args in calls))
        self.assertTrue(any("osm-status-app" in args for args in calls))
        self.assertTrue((self.work / "data/scripts/import-orchestrator.sh").exists())
        self.assertIn("--rollout-management", result.stdout)
        documents = [json.loads(line) for line in applied.read_text().splitlines()]
        role = next(doc for doc in documents if doc.get("kind") == "Role" and doc["metadata"]["name"] == "status-manager")
        policy_rules = [rule for rule in role["rules"] if "configmaps" in rule["resources"]]
        self.assertEqual(policy_rules, [{"apiGroups": [""], "resources": ["configmaps"],
                                         "resourceNames": ["osm-pelias-policy"], "verbs": ["get"]}])
        self.assertTrue(service_state.exists(), "Initial install must create the Pelias Service")

        live = json.loads(service_state.read_text())
        live["spec"]["selector"] = {"app": "pelias-generation", "generation": "live-good"}
        live["spec"]["ports"][0]["nodePort"] = 30999
        service_state.write_text(json.dumps(live))
        for profile_args in ([], ["--bigmemory"]):
            applied.write_text("")
            result = subprocess.run(["bash", "scripts/deploy-osm.sh", "--pelias-only"] + profile_args,
                                    cwd=ROOT, text=True, capture_output=True,
                                    env=dict(self.env, PATH=str(bindir) + ":" + os.environ["PATH"],
                                             TEST_KUBE_LOG=str(log)))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(service_state.read_text()), live)
            documents = [json.loads(line) for line in applied.read_text().splitlines()]
            self.assertFalse(any(doc.get("kind") == "Service" and doc["metadata"]["name"] == "pelias"
                                 for doc in documents))
            self.assertTrue(any(doc.get("kind") == "Deployment" for doc in documents))

        log.write_text("")
        env = dict(self.env, PATH=str(bindir) + ":" + os.environ["PATH"],
                   TEST_KUBE_LOG=str(log))
        result = subprocess.run(["bash", "scripts/deploy-osm.sh", "--pelias-only", "--rollout-management"],
                                cwd=ROOT, text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        restarts = [args[-1] for args in calls if "restart" in args]
        self.assertEqual(restarts, ["deployment/status", "deployment/import-orchestrator"])
        self.assertTrue(any("exec" in args and "/state/import-request.lock" in args[-1] for args in calls))

        for refusal in ("TEST_ACTIVE_JOBS", "TEST_IMPORT_RUNNING"):
            log.write_text("")
            result = subprocess.run(["bash", "scripts/deploy-osm.sh", "--pelias-only", "--rollout-management"],
                                    cwd=ROOT, text=True, capture_output=True,
                                    env=dict(env, **{refusal: "1"}))
            self.assertNotEqual(result.returncode, 0)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertFalse(any("restart" in args for args in calls))

        for count in ("0", "2"):
            applied.write_text("")
            result = subprocess.run(["bash", "scripts/deploy-osm.sh", "--pelias-only"],
                                    cwd=ROOT, text=True, capture_output=True,
                                    env=dict(env, TEST_NODE_COUNT=count))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("placement", result.stderr)
            self.assertEqual(applied.read_text(), "", "Placement must fail before applying resources")

        result = subprocess.run(["bash", "scripts/deploy-osm.sh", "--pelias-only", "--pelias-node", "node-2"],
                                cwd=ROOT, text=True, capture_output=True,
                                env=dict(env, TEST_NODE_COUNT="2"))
        self.assertEqual(result.returncode, 0, result.stderr)
        job = yaml.safe_load((self.work / "data/manifests/pelias-import-job.yaml").read_text())
        self.assertEqual(job["spec"]["template"]["spec"]["nodeSelector"]["kubernetes.io/hostname"], "node-2")

        log.write_text("")
        result = subprocess.run(["bash", "scripts/deploy-osm.sh", "--pelias-only", "--bigmemory"],
                                cwd=ROOT, text=True, capture_output=True,
                                env=dict(env, TEST_NODE_COUNT="2"))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertFalse(any("nodes" in args for args in calls), "RWX profile must not impose standard RWO placement")

    def test_embedded_status_profiles_exactly_match_source(self):
        source = (ROOT / "status/app.py").read_text()
        for profile in ("k8s", "k8s/bigmemory"):
            path = ROOT / profile / "status.yaml"
            documents = list(yaml.safe_load_all(path.read_text()))
            app_config = next(doc for doc in documents if doc.get("metadata", {}).get("name") == "osm-status-app")
            role = next(doc for doc in documents if doc.get("kind") == "Role" and doc["metadata"]["name"] == "status-manager")
            self.assertIn({"apiGroups": [""], "resources": ["configmaps"],
                           "resourceNames": ["osm-pelias-policy"], "verbs": ["get"]}, role["rules"])
            embedded = app_config["data"]["app.py"]
            self.assertEqual(embedded, source, str(path))
            compile(embedded, str(path), "exec")
            code = {}
            exec(compile(embedded, str(path), "exec"), code)
            self.assertEqual(code["PELIAS_STATE_FILE"],
                             os.path.join(code["DATA_DIR"], "status", "pelias.json"))

    def test_abort_includes_both_lifecycle_jobs(self):
        self.request(steps=["pelias"])
        with patch.object(app, "read_orchestrator_state", return_value={"running": True}), \
             patch.object(app, "write_workflow_state"), \
             patch.object(app.KUBE, "delete_job") as delete:
            app.abort_import_workflow()
        deleted = [call.args[0] for call in delete.call_args_list]
        self.assertIn("pelias-import", deleted)
        self.assertIn("pelias-promote", deleted)
        self.assertTrue(Path(app.ABORT_FLAG_FILE).exists())


if __name__ == "__main__":
    unittest.main()
