"""Structural contracts for the optional Pelias profiles (requires PyYAML)."""

import io
import json
from pathlib import Path
import unittest
from unittest import mock

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

    def test_elasticsearch_has_one_writer_and_no_public_port(self):
        for profile in PROFILES:
            claim = find(profile, "PersistentVolumeClaim", "pelias-elasticsearch")
            self.assertEqual(["ReadWriteOnce"], claim["spec"]["accessModes"])
            statefulset = find(profile, "StatefulSet", "pelias-elasticsearch")
            self.assertEqual(1, statefulset["spec"]["replicas"])
            pod = statefulset["spec"]["template"]["spec"]
            container = pod["containers"][0]
            self.assertRegex(container["image"], r"elasticsearch:7\.17\.\d+$")
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
        for name, repo in lock["repositories"].items():
            self.assertRegex(repo["commit"], r"^[a-f0-9]{40}$", name)
            self.assertTrue(repo["url"].startswith("https://github.com/"))
        for image in lock["bases"].values():
            self.assertNotIn(":latest", image)
            self.assertRegex(image, r"@sha256:[a-f0-9]{64}$")


if __name__ == "__main__":
    unittest.main()
