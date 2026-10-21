"""Structural contracts for the optional Pelias profiles (requires PyYAML)."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import unittest
from unittest import mock
import uuid

import yaml


ROOT = Path(__file__).resolve().parents[1]
PROFILES = (ROOT / "k8s", ROOT / "k8s" / "bigmemory")


def documents(profile):
    return [
        item
        for path in sorted(profile.glob("pelias*.yaml"))
        for item in yaml.safe_load_all(path.read_text())
        if item
    ]


def find(profile, kind, name):
    return next(
        item for item in documents(profile)
        if item["kind"] == kind and item["metadata"]["name"] == name
    )


class PeliasManifestsTest(unittest.TestCase):
    def test_both_profiles_have_identical_object_inventory(self):
        inventories = [
            {(d["kind"], d["metadata"]["name"]) for d in documents(p)}
            for p in PROFILES
        ]
        self.assertEqual(inventories[0], inventories[1])
        for profile in PROFILES:
            docs = documents(profile)
            self.assertEqual(len(docs), len(inventories[0]), "duplicate object")
            for doc in docs:
                self.assertEqual("osm", doc["metadata"]["namespace"])

    def test_native_config_and_lifecycle_policy_are_separate_and_disabled(self):
        for profile in PROFILES:
            native = find(profile, "ConfigMap", "osm-pelias-config")["data"]
            policy = find(profile, "ConfigMap", "osm-pelias-policy")["data"]
            self.assertEqual({"pelias.json"}, set(native))
            self.assertEqual({"policy.json"}, set(policy))
            config = json.loads(native["pelias.json"])
            lifecycle = json.loads(policy["policy.json"])
            self.assertIn("esclient", config)
            self.assertEqual("europa.osm.pbf", config["imports"]["openstreetmap"]["import"][0]["filename"])
            for key in ("enabled", "auto_update", "auto_promote"):
                self.assertIs(False, lifecycle[key])
                self.assertNotIn(key, config)
            self.assertNotIn("typeName", config["schema"])
            self.assertEqual("kubernetes", lifecycle["serving"]["mode"])
            self.assertEqual(
                "pelias-generation-template",
                lifecycle["serving"]["deployment_template"],
            )
            self.assertEqual(
                {"search", "autocomplete", "search/structured", "reverse"},
                {case["endpoint"] for case in lifecycle["smoke"]},
            )

    def test_jobs_have_persistent_data_status_bounded_resources_and_ca(self):
        for profile in PROFILES:
            for name in ("import", "promote", "cleanup", "aux"):
                job = find(profile, "Job", "pelias-" + name)
                self.assertEqual(0, job["spec"]["backoffLimit"])
                pod = job["spec"]["template"]["spec"]
                self.assertEqual("Never", pod["restartPolicy"])
                self.assertEqual({"osm": "true"}, pod["nodeSelector"])
                self.assertEqual("pelias-lifecycle", pod["serviceAccountName"])
                volumes = {v["name"]: v for v in pod["volumes"]}
                self.assertEqual("pelias-data", volumes["data"]["persistentVolumeClaim"]["claimName"])
                self.assertEqual("osm-status", volumes["status"]["persistentVolumeClaim"]["claimName"])
                self.assertEqual("osm-pelias-runtime", volumes["runtime"]["configMap"]["name"])
                for container in pod["containers"]:
                    self.assertNotIn(":latest", container["image"])
                    for bound in ("requests", "limits"):
                        self.assertEqual({"cpu", "memory"}, set(container["resources"][bound]))
                    env = {e["name"]: e.get("value") for e in container["env"]}
                    for key in ("SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"):
                        self.assertEqual("/run/pelias-ca.crt", env[key])
                    shell = "\n".join(container["args"])
                    self.assertIn("cp /etc/ssl/certs/ca-certificates.crt", shell)
                    self.assertIn("cat /custom-ca/ca.crt >>", shell)
                if name in ("import", "promote"):
                    self.assertTrue(volumes["planet"]["persistentVolumeClaim"]["readOnly"])
                    mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
                    self.assertTrue(mounts["/planet"]["readOnly"])

    def test_manual_builds_and_queued_automatic_promotions_remain_distinct(self):
        for profile in PROFILES:
            build = find(profile, "Job", "pelias-import")["spec"]["template"]["spec"]["containers"][0]
            env = {entry["name"]: entry["value"] for entry in build["env"]}
            self.assertEqual("true", env["PELIAS_AUTOMATIC"])
            self.assertIn("false) exec python3", build["args"][0])
            self.assertIn("--manual", build["args"][0])
            promote = find(profile, "Job", "pelias-promote")["spec"]["template"]["spec"]["containers"][0]
            self.assertIn('p.get("auto_promote") is True', promote["args"][0])
            self.assertIn('test -n "$PELIAS_GENERATION"', promote["args"][0])

    def test_legacy_initializer_can_only_create_the_shared_lock(self):
        for profile in PROFILES:
            for name in ("pelias-import", "pelias-promote"):
                pod = find(profile, "Job", name)["spec"]["template"]["spec"]
                initializer = pod["initContainers"][0]
                self.assertEqual(["python3", "-c"], initializer["command"])
                with mock.patch("os.open", return_value=42) as opened, \
                     mock.patch("os.close") as closed, mock.patch("os.umask"):
                    exec(compile(initializer["args"][0], "<lock-initializer>", "exec"), {})
                opened.assert_called_once_with(
                    "/planet/.pelias-source.lock", os.O_CREAT | os.O_RDONLY, 0o666,
                )
                closed.assert_called_once_with(42)
                self.assertEqual(
                    [{"name": "source-lock-initializer", "mountPath": "/planet"}],
                    initializer["volumeMounts"],
                )
                self.assertNotIn("europa.osm.pbf", initializer["args"][0])
                main = pod["containers"][0]
                source = next(m for m in main["volumeMounts"] if m["mountPath"] == "/planet")
                self.assertTrue(source["readOnly"])

    def test_embedded_shell_commands_parse(self):
        for profile in PROFILES:
            for doc in documents(profile):
                if doc["kind"] not in ("Deployment", "Job", "StatefulSet"):
                    continue
                for container in doc["spec"]["template"]["spec"]["containers"]:
                    if container.get("command", [None])[0] == "/bin/sh":
                        subprocess.run(
                            ["/bin/sh", "-n"], input=container["args"][0],
                            text=True, capture_output=True, check=True,
                        )

    def test_elasticsearch_has_one_writer_and_no_public_port(self):
        for profile in PROFILES:
            claim = find(profile, "PersistentVolumeClaim", "pelias-elasticsearch")
            self.assertEqual(["ReadWriteOnce"], claim["spec"]["accessModes"])
            statefulset = find(profile, "StatefulSet", "pelias-elasticsearch")
            self.assertEqual(1, statefulset["spec"]["replicas"])
            pod = statefulset["spec"]["template"]["spec"]
            container = pod["containers"][0]
            self.assertRegex(container["image"], r"elasticsearch:7\.17\.\d+$")
            lock = json.loads((ROOT / "pelias/images.lock.json").read_text())
            self.assertTrue(container["image"].endswith(":" + lock["elasticsearch"]))
            self.assertIn("startupProbe", container)
            self.assertIn("readinessProbe", container)
            self.assertIn("livenessProbe", container)
            service = find(profile, "Service", "pelias-elasticsearch")
            self.assertNotEqual("NodePort", service["spec"].get("type"))
            self.assertEqual("pelias-elasticsearch", pod["volumes"][0]["persistentVolumeClaim"]["claimName"])

    def test_helpers_are_shared_read_many_and_profile_storage_matches(self):
        for profile, storage in zip(PROFILES, ("longhorn-static", "csi-cephfs-replicated")):
            claim = find(profile, "PersistentVolumeClaim", "pelias-data")
            self.assertEqual(["ReadWriteMany"], claim["spec"]["accessModes"])
            self.assertEqual(storage, claim["spec"]["storageClassName"])

    def test_role_does_not_grant_secrets_or_cluster_privileges(self):
        for profile in PROFILES:
            role = find(profile, "Role", "pelias-lifecycle")
            for rule in role["rules"]:
                self.assertNotIn("*", rule["verbs"])
                self.assertNotIn("*", rule["resources"])
                self.assertNotIn("secrets", rule["resources"])
                self.assertNotIn("pods/exec", rule["resources"])
                if "services" in rule["resources"]:
                    self.assertEqual(["pelias"], rule["resourceNames"])
            self.assertFalse(any(d["kind"].startswith("ClusterRole") for d in documents(profile)))

    def test_generation_is_a_single_atomic_api_and_helper_bundle(self):
        for profile in PROFILES:
            deployment = find(profile, "Deployment", "pelias-generation-template")
            self.assertEqual(0, deployment["spec"]["replicas"])
            pod = deployment["spec"]["template"]["spec"]
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual(1, len(pod["containers"]))
            container = pod["containers"][0]
            self.assertEqual({4000, 4100, 4200, 4300}, {
                port["containerPort"] for port in container["ports"]
            })
            self.assertIn("controller.py serve", container["args"][0])
            self.assertIn('\"$PELIAS_GENERATION\"', container["args"][0])
            probe = container["readinessProbe"]["exec"]["command"][-1]
            self.assertIn("/v1/search?", probe)
            self.assertIn('body.get("features")', probe)
            self.assertIn('get("label")', probe)
            self.assertIn("startupProbe", container)
            self.assertIn("livenessProbe", container)
            for mount in container["volumeMounts"]:
                self.assertTrue(mount["readOnly"])
            service = find(profile, "Service", "pelias")
            self.assertEqual("NodePort", service["spec"]["type"])
            self.assertEqual(30088, service["spec"]["ports"][0]["nodePort"])
            self.assertEqual(
                deployment["spec"]["template"]["metadata"]["labels"],
                service["spec"]["selector"],
            )
            self.assertEqual("disabled", service["spec"]["selector"]["pelias-generation"])
            volumes = {v["name"]: v for v in pod["volumes"]}
            self.assertEqual(
                {"osm-pelias-config", "osm-pelias-policy"},
                {source["configMap"]["name"] for source in volumes["config"]["projected"]["sources"]},
            )
            for other in documents(profile):
                if other["kind"] == "Service" and other["metadata"]["name"] != "pelias":
                    self.assertNotEqual("NodePort", other["spec"].get("type"))

    def test_no_other_service_uses_pelias_nodeport(self):
        for profile in PROFILES:
            for path in profile.glob("*.yaml"):
                if path.name.startswith("pelias"):
                    continue
                self.assertNotRegex(path.read_text(), r"nodePort:\s*30088\b", str(path))

    def test_readiness_rejects_a_port_only_or_empty_search_response(self):
        deployment = find(PROFILES[0], "Deployment", "pelias-generation-template")
        source = deployment["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["exec"]["command"][-1]
        cases = [
            ({"status": "ok"}, False),
            ({"type": "FeatureCollection", "features": []}, False),
            ({"type": "FeatureCollection", "features": [{
                "geometry": {"type": "Point", "coordinates": [13.4, 52.5]},
                "properties": {"label": "Berlin"},
            }]}, True),
        ]
        for body, valid in cases:
            with self.subTest(body=body), mock.patch.dict("os.environ", {"PELIAS_READINESS_QUERY": "Berlin"}):
                with mock.patch("urllib.request.urlopen", return_value=io.StringIO(json.dumps(body))):
                    if valid:
                        exec(compile(source, "<readiness-probe>", "exec"), {})
                    else:
                        with self.assertRaises(AssertionError):
                            exec(compile(source, "<readiness-probe>", "exec"), {})

    def test_lock_has_exact_git_revisions_and_no_latest_base(self):
        lock = json.loads((ROOT / "pelias/images.lock.json").read_text())
        self.assertTrue(lock["repositories"])
        self.assertTrue({
            "api", "schema", "openstreetmap", "whosonfirst", "polylines",
            "placeholder", "pip-service", "interpolation", "csv-importer",
            "libpostal-server",
        }.issubset(lock["repositories"]))
        for name, repo in lock["repositories"].items():
            self.assertRegex(repo["commit"], r"^[a-f0-9]{40}$", name)
            self.assertTrue(repo["url"].startswith("https://github.com/"))
        for image in lock["bases"].values():
            self.assertNotIn(":latest", image)
            self.assertRegex(image, r"@sha256:[a-f0-9]{64}$")


@unittest.skipUnless(os.environ.get("PELIAS_REAL_SMOKE") == "1", "opt-in real Docker import")
class PeliasRealSmokeTest(unittest.TestCase):
    def test_tiny_upstream_fixtures_import_and_all_four_api_endpoints(self):
        work = ROOT / "tests" / (".pelias-real-" + uuid.uuid4().hex)
        work.mkdir()
        self.addCleanup(shutil.rmtree, work)

        def run(*args, input=None, timeout=300):
            result = subprocess.run(
                list(args), input=input, text=True, capture_output=True, timeout=timeout,
            )
            self.assertEqual(0, result.returncode, (result.stdout + result.stderr)[-14000:])
            return result.stdout.strip()

        def start(*args):
            identifier = run("docker", "run", "-d", *args)
            self.addCleanup(
                subprocess.run, ["docker", "rm", "-f", identifier],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return identifier

        es = start(
            "--memory=2g", "-e", "discovery.type=single-node",
            "-e", "xpack.security.enabled=false", "-e", "node.store.allow_mmap=false",
            "-e", "ES_JAVA_OPTS=-Xms512m -Xmx512m",
            "localosm/pelias-elasticsearch:7.17.27",
        )
        runtime = start(
            "--network", "container:" + es, "-v", str(work) + ":/data",
            "-v", str(ROOT / "pelias") + ":/runtime:ro",
            "--user", "%s:%s" % (os.getuid(), os.getgid()),
            "--entrypoint", "sleep", "localosm/pelias-runtime:2026-10-21", "infinity",
        )
        start(
            "--network", "container:" + es,
            "localosm/pelias-libpostal:2026-10-21",
        )
        for _ in range(90):
            ready = subprocess.run(
                ["docker", "exec", runtime, "curl", "-fsS",
                 "http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow"],
                capture_output=True, timeout=10,
            )
            if ready.returncode == 0:
                break
            time.sleep(2)
        else:
            self.fail(run("docker", "logs", es))
        native = json.loads(
            find(PROFILES[0], "ConfigMap", "osm-pelias-config")["data"]["pelias.json"],
        )
        native["esclient"]["hosts"][0]["host"] = "127.0.0.1"
        native["api"]["services"]["libpostal"]["url"] = "http://127.0.0.1:4400"
        (work / "native.json").write_text(json.dumps(native))
        policy = {
            "enabled": True, "auto_update": False, "auto_promote": False,
            "min_documents": 1, "startup_timeout": 60,
            "aux": {"version": "tiny-fixtures", "countries": ["FRA"]},
            "smoke": [
                {"endpoint": "search", "params": {"text": "900 Burrard Street"}},
                {"endpoint": "autocomplete", "params": {"text": "Burrard Str"}},
                {"endpoint": "search/structured", "params": {"address": "900 Burrard Street"}},
                {"endpoint": "reverse", "params": {"point.lat": 49.2819978, "point.lon": -123.1243463}},
            ],
        }
        (work / "policy.json").write_text(json.dumps(policy))
        run("docker", "exec", "-i", runtime, "node", input=r"""
const fs = require('fs');
const root = '/data/aux/tiny-fixtures';
const directory = root + '/whosonfirst/sqlite';
fs.mkdirSync(directory, {recursive:true});
fs.mkdirSync('/data/source', {recursive:true});
const entries = require('/code/whosonfirst/test/resources/france.json').tavel;
for (const entry of entries) {
  // This metadata-only upstream fixture uses bbox polygons only for isolated tests.
  const bbox = entry.properties['geom:bbox'].split(',').map(Number);
  const [w,s,e,n] = bbox;
  entry.geometry = {type:'Polygon',coordinates:[[[w,s],[e,s],[e,n],[w,n],[w,s]]]};
}
const db = require('/code/whosonfirst/test/generateWOFDB')(
  directory + '/whosonfirst-data-admin-fr.db', entries);
db.exec('CREATE TABLE ancestors (id INTEGER, ancestor_id INTEGER)');
for (const entry of entries) {
  for (const ancestor of entry.properties['wof:belongsto']) {
    db.prepare('INSERT INTO ancestors VALUES (?,?)').run(entry.id,ancestor);
  }
  db.prepare('UPDATE spr SET is_current=1, country=?, repo=? WHERE id=?')
    .run('FR','whosonfirst-data-admin-fr',entry.id);
}
db.close();
fs.copyFileSync('/code/openstreetmap/test/vancouver_canada.osm.pbf',
  '/data/source/official.osm.pbf');
fs.writeFileSync('/data/source/.pelias-source.lock','');
""")
        run(
            "docker", "exec", runtime, "osmium", "extract", "/data/source/official.osm.pbf",
            "--bbox", "-123.127,49.280,-123.122,49.284", "--strategy", "complete_ways",
            "-o", "/data/source/europa.osm.pbf",
        )
        result = run("docker", "exec", "-i", runtime, "python3", input=r"""
import json, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, '/runtime')
from controller import Controller, digest
args = SimpleNamespace(
    command='build', config='/data/native.json', policy='/data/policy.json',
    data='/data', status='/data/status.json', modules='/code', manual=True,
    source='/data/source/europa.osm.pbf', source_lock='/data/source/.pelias-source.lock',
)
controller = Controller(args)
with controller.locked():
    root = Path('/data/generations/tiny')
    root.mkdir(parents=True)
    record = {'id':'tiny','index':'pelias-tiny','aux_version':'tiny-fixtures'}
    controller.state['generations']['tiny'] = record
    config = controller.make_config(root,'pelias-tiny',Path('/data/aux/tiny-fixtures'))
    try:
        controller.pipeline(root,config)
        controller.check_index(record)
        result = controller.candidate_smoke('tiny')
        record.update(phase='built', config_sha256=digest(config))
        (root / '.serving.lock').touch()
        controller.save()
        print('REAL_PELIAS_SMOKE=' + json.dumps(result))
    except BaseException:
        for error_log in root.rglob('*.err'):
            print(str(error_log) + '\n' + error_log.read_text(errors='replace')[-8000:])
        raise
    finally:
        controller.processes.close()
""", timeout=600)
        self.assertIn("REAL_PELIAS_SMOKE=", result)
        print(result[result.index("REAL_PELIAS_SMOKE="):])
        serving = start(
            "--network", "container:" + es, "-v", str(work) + ":/data:ro",
            "-v", str(ROOT / "pelias") + ":/runtime:ro",
            "--tmpfs", "/scratch:rw,mode=1777",
            "--user", "%s:%s" % (os.getuid(), os.getgid()),
            "localosm/pelias-runtime:2026-10-21",
            "python3", "/runtime/controller.py", "serve", "--generation", "tiny",
            "--config", "/data/native.json", "--policy", "/data/policy.json",
        )
        for _ in range(60):
            ready = subprocess.run(
                ["docker", "exec", runtime, "curl", "-fsS",
                 "http://127.0.0.1:4000/v1/search?text=Burrard%20Street"],
                capture_output=True, timeout=10,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            self.fail(run("docker", "logs", serving))
        production = run(
            "docker", "exec", runtime, "python3", "-c",
            "import json,sys; sys.path.insert(0,'/runtime'); import smoke; "
            "p=json.load(open('/data/policy.json')); "
            "print(json.dumps(smoke.run('http://127.0.0.1:4000',p['smoke'])))",
        )
        self.assertEqual(4, len(json.loads(production)))
        print("READONLY_GENERATION_SMOKE=" + production)


if __name__ == "__main__":
    unittest.main()
