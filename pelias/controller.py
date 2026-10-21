#!/usr/bin/env python3
"""Serialized, generation-based Pelias maintenance. Elasticsearch is never copied."""

import argparse
import bz2
import contextlib
import copy
import datetime
import fcntl
import hashlib
import gzip
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import smoke


GENERATION = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".next")
    with open(pending, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


class Cancelled(RuntimeError):
    pass


class Processes:
    def __init__(self):
        self.children = []
        self.cancelled = False

    def signal(self, number, _frame):
        self.cancelled = True
        for child in self.children:
            if child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, number)
        raise Cancelled("lifecycle interrupted by signal %d" % number)

    def start(self, argv, env=None, cwd=None, quiet=False, capture=False):
        if self.cancelled:
            raise Cancelled("lifecycle interrupted")
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, (signal.SIGTERM, signal.SIGINT))
        try:
            child = subprocess.Popen(
                argv, env=env, cwd=cwd, start_new_session=True,
                stdout=subprocess.PIPE if capture else (subprocess.DEVNULL if quiet else None),
                preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, blocked),
            )
            self.children.append(child)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
        return child

    def run(self, argv, env=None, cwd=None):
        child = self.start(argv, env, cwd)
        try:
            code = child.wait()
            if code:
                raise RuntimeError("command failed (%d): %s" % (code, argv))
        finally:
            self.stop(child)

    def stop(self, child):
        if child.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        if child in self.children:
            self.children.remove(child)

    def close(self):
        for child in list(reversed(self.children)):
            self.stop(child)

    def output(self, argv):
        child = self.start(argv, capture=True)
        try:
            output, _ = child.communicate()
            if child.returncode:
                raise RuntimeError("command failed: %s" % argv)
            return output.decode()
        finally:
            self.stop(child)


class Elasticsearch:
    def __init__(self, config):
        hosts = config["esclient"]["hosts"]
        host = hosts[0] if isinstance(hosts, list) else hosts
        if isinstance(host, dict):
            host = "%s://%s:%s" % (
                host.get("protocol", "http"), host["host"], host.get("port", 9200),
            )
        self.url = host.rstrip("/")
        self.headers = {}
        token = os.environ.get("PELIAS_ES_AUTHORIZATION")
        if token:
            self.headers["Authorization"] = token

    def request(self, method, path, body=None, missing=None):
        headers = dict(self.headers)
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, payload, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read()
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing is not None:
                return missing
            raise

    def target(self, alias):
        targets = self.request("GET", "/_alias/" + alias, missing={})
        if len(targets) > 1:
            raise RuntimeError("serving alias has multiple targets; refusing mutation")
        return next(iter(targets), None)

    def switch(self, alias, old, new):
        if self.target(alias) != old:
            raise RuntimeError("serving alias changed during validation")
        actions = []
        if old:
            actions.append({"remove": {"index": old, "alias": alias, "must_exist": True}})
        actions.append({"add": {"index": new, "alias": alias, "is_write_index": True}})
        result = self.request("POST", "/_aliases", {"actions": actions})
        if not result.get("acknowledged"):
            raise RuntimeError("alias switch was not acknowledged")


class Kubernetes:
    """The namespace-scoped serving adapter; never shells out to kubectl."""

    def __init__(self, policy):
        self.policy = policy
        credentials = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.namespace = policy.get("namespace") or (credentials / "namespace").read_text().strip()
        for name in (
            self.namespace, policy.get("service", "pelias"),
            policy.get("deployment_template", "pelias"), policy.get("container", "pelias"),
        ):
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", name):
                raise ValueError("invalid Kubernetes serving resource name")
        self.token_path = credentials / "token"
        self.context = ssl.create_default_context(cafile=str(credentials / "ca.crt"))
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        self.url = "https://%s:%s" % (host, port)
        self.deployments = "/apis/apps/v1/namespaces/%s/deployments" % self.namespace
        self.service_path = "/api/v1/namespaces/%s/services/%s" % (
            self.namespace, policy.get("service", "pelias"),
        )
        self.label = policy.get("generation_label", "generation")
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", self.label):
            raise ValueError("invalid generation selector label")

    def request(self, method, path, body=None, missing=None):
        # Large rebuilds can outlive projected service-account token validity.
        headers = {"Authorization": "Bearer " + self.token_path.read_text().strip()}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = (
                "application/merge-patch+json" if method == "PATCH" else "application/json"
            )
        request = urllib.request.Request(self.url + path, payload, headers, method=method)
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing is not None:
                return missing
            raise

    def selector(self):
        return self.request("GET", self.service_path)["spec"]["selector"]

    def select(self, old, new):
        service = self.request("GET", self.service_path)
        if service["spec"]["selector"] == new:
            return
        if service["spec"]["selector"] != old:
            raise RuntimeError("serving Service selector changed concurrently")
        # A resourceVersion precondition prevents overwriting a concurrent operator edit.
        self.request("PATCH", self.service_path, {
            "metadata": {"resourceVersion": service["metadata"]["resourceVersion"]},
            "spec": {"selector": dict(
                {key: None for key in old if key not in new}, **new,
            )},
        })

    def prepare(self, identifier):
        template_name = self.policy.get("deployment_template", "pelias")
        name = template_name + "-gen-" + identifier
        if len(name) > 63:
            raise ValueError("generation Deployment name exceeds 63 characters")
        path = self.deployments + "/" + name
        existing = self.request("GET", path, missing={})
        if existing:
            self.check_owned(existing, identifier)
        else:
            template = self.request("GET", self.deployments + "/" + template_name)
            spec = copy.deepcopy(template["spec"])
            spec["replicas"] = 1
            labels = spec["template"]["metadata"].setdefault("labels", {})
            labels[self.label] = identifier
            spec["selector"] = {"matchLabels": dict(spec["selector"]["matchLabels"], **{
                self.label: identifier,
            })}
            containers = spec["template"]["spec"]["containers"]
            container_name = self.policy.get("container", "pelias")
            container = next((item for item in containers if item["name"] == container_name), None)
            if not container:
                raise RuntimeError("serving template has no Pelias runtime container")
            if not container.get("command"):
                container["command"] = [
                    "python3", "/runtime/controller.py", "serve",
                ]
                container.pop("args", None)
            env = [item for item in container.get("env", []) if item["name"] != "PELIAS_GENERATION"]
            container["env"] = env + [{"name": "PELIAS_GENERATION", "value": identifier}]
            self.request("POST", self.deployments, {
                "apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": name, "labels": {
                    self.label: identifier, "app.kubernetes.io/managed-by": "pelias-lifecycle",
                }},
                "spec": spec,
            })
        deadline = time.monotonic() + self.policy.get("startup_timeout", 1800)
        while time.monotonic() < deadline:
            deployment = self.request("GET", path)
            status = deployment.get("status", {})
            if (
                status.get("observedGeneration", 0) >= deployment["metadata"].get("generation", 1)
                and status.get("updatedReplicas", 0) >= 1
                and status.get("readyReplicas", 0) >= 1
                and status.get("availableReplicas", 0) >= 1
            ):
                return name
            time.sleep(2)
        raise RuntimeError("generation serving Deployment did not become ready: " + name)

    def check_owned(self, deployment, identifier):
        labels = deployment["metadata"].get("labels", {})
        if (
            labels.get(self.label) != identifier
            or labels.get("app.kubernetes.io/managed-by") != "pelias-lifecycle"
        ):
            raise RuntimeError("refusing to mutate an unowned serving Deployment")

    def remove(self, name, identifier):
        path = self.deployments + "/" + name
        deployment = self.request("GET", path, missing={})
        if not deployment:
            return
        self.check_owned(deployment, identifier)
        if self.selector().get(self.label) == identifier:
            raise RuntimeError("cannot delete the Service-selected generation Deployment")
        self.request("DELETE", path, {
            "propagationPolicy": "Foreground",
            "preconditions": {"uid": deployment["metadata"]["uid"]},
        })


class Controller:
    def __init__(self, args):
        self.args = args
        self.data = Path(args.data)
        self.generations = self.data / "generations"
        self.state_path = self.data / "lifecycle.json"
        self.status_path = Path(args.status)
        self.policy = read_json(args.policy)
        self.native = read_json(args.config)
        self.expected_source = getattr(args, "expected_source_sha256", None)
        if self.expected_source and not re.fullmatch(r"[0-9a-f]{64}", self.expected_source):
            raise ValueError("expected source must be a lowercase SHA256")
        self.alias = self.policy.get("alias", "pelias")
        if not GENERATION.fullmatch(self.alias):
            raise ValueError("invalid Elasticsearch alias")
        self.es = Elasticsearch(self.native)
        self.processes = Processes()
        self.state = {}
        serving = self.policy.get("serving", {})
        if serving.get("mode") not in (None, "kubernetes"):
            raise ValueError("unsupported serving mode")
        if os.environ.get("KUBERNETES_SERVICE_HOST") and serving.get("mode") != "kubernetes":
            raise ValueError("cluster lifecycle requires explicit serving.mode=kubernetes")
        self.serving = (
            Kubernetes(serving)
            if serving.get("mode") == "kubernetes"
            and getattr(args, "command", "build") not in ("serve", "probe")
            else None
        )

    @contextlib.contextmanager
    def locked(self):
        self.data.mkdir(parents=True, exist_ok=True)
        with open(self.data / ".lifecycle.lock", "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.state = read_json(self.state_path, {
                "active": None, "last_good": None, "building": None,
                "generations": {},
            })
            yield

    def save(self):
        self.state["updated_at"] = now()
        atomic_json(self.state_path, self.state)
        atomic_json(self.status_path, self.state)

    def check_enabled(self):
        if not self.policy.get("enabled", False):
            raise RuntimeError("Pelias lifecycle is disabled by policy")

    @contextlib.contextmanager
    def source_locked(self):
        # The publisher creates this file; readers must not need write permission.
        with open(self.args.source_lock, "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            yield

    def fingerprint(self):
        path = Path(self.args.source)
        before = path.stat()
        if not path.is_file() or before.st_size == 0:
            raise RuntimeError("source PBF is missing or empty")
        header = self.processes.output(["osmium", "fileinfo", "--json", str(path)])
        result = {
            "sha256": digest(path), "size": before.st_size,
            "mtime_ns": before.st_mtime_ns, "inode": before.st_ino,
            "device": before.st_dev, "header": json.loads(header),
        }
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise RuntimeError("source PBF changed during fingerprinting")
        metadata_path = path.with_name(path.name + ".meta.json")
        if metadata_path.exists():
            try:
                metadata = read_json(metadata_path)
            except (OSError, ValueError):
                metadata = None
            if isinstance(metadata, dict) and all(
                metadata.get(key) == result[key] for key in ("sha256", "size", "mtime_ns")
            ):
                result["published_at"] = metadata.get("published_at")
                result["publisher_metadata"] = "matched"
            else:
                # A publisher can crash after PBF rename and before sidecar rename.
                # Only the independently read bytes identify the consumed source.
                result["publisher_metadata"] = "stale-or-unreadable"
        if self.expected_source and result["sha256"] != self.expected_source:
            raise RuntimeError("source PBF no longer matches the queued SHA256")
        return result

    def generation(self, identifier):
        if not identifier or not GENERATION.fullmatch(identifier):
            raise ValueError("a valid explicit generation is required")
        try:
            record = self.state["generations"][identifier]
        except KeyError:
            raise RuntimeError("unknown generation: " + identifier) from None
        if record.get("id") != identifier or record.get("index") != self.alias + "-" + identifier:
            raise RuntimeError("generation metadata does not match its owned index")
        return record

    def aux_spec(self):
        spec = self.policy["aux"]
        version = spec["version"]
        if not isinstance(version, str) or not GENERATION.fullmatch(version):
            raise ValueError("aux.version must be an immutable safe version name")
        if not spec.get("countries") or not spec.get("files"):
            raise ValueError("aux countries and pinned files are required")
        if not isinstance(spec["countries"], list) or not all(
            isinstance(country, str) and re.fullmatch(r"[A-Z]{2,3}", country)
            for country in spec["countries"]
        ):
            raise ValueError("aux countries must be explicit ISO country codes")
        seen = set()
        for item in spec["files"]:
            path = Path(item["path"])
            if (
                path.is_absolute() or ".." in path.parts or str(path) in seen
                or str(path) in (".", "manifest.json")
            ):
                raise ValueError("unsafe or duplicate auxiliary path")
            seen.add(str(path))
            if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                raise ValueError("every auxiliary file requires a SHA256")
            compression = item.get("compression", "none")
            if compression not in ("none", "gzip", "bz2", "xz"):
                raise ValueError("auxiliary compression must be none, gzip, bz2 or xz")
            if compression != "none" and not re.fullmatch(
                r"[0-9a-f]{64}", item.get("content_sha256", ""),
            ):
                raise ValueError("compressed auxiliary files require an expanded content_sha256")
            if compression == "none" and item.get("content_sha256", item["sha256"]) != item["sha256"]:
                raise ValueError("uncompressed auxiliary checksums must agree")
            if urllib.parse.urlsplit(item["url"]).scheme != "https":
                raise ValueError("auxiliary downloads require explicit HTTPS URLs")
        return spec

    def verified_aux(self):
        spec = self.aux_spec()
        root = self.data / "aux" / spec["version"]
        if read_json(root / "manifest.json") != spec:
            raise RuntimeError("auxiliary cache does not match policy; run aux explicitly")
        self.verify_aux_files(root, spec)
        return root

    def verify_aux_files(self, root, spec):
        expected = {item["path"] for item in spec["files"]} | {"manifest.json"}
        actual = set()
        for path in root.rglob("*"):
            if path.is_symlink():
                raise RuntimeError("auxiliary cache cannot contain symlinks")
            if path.is_file():
                actual.add(path.relative_to(root).as_posix())
        if actual != expected:
            raise RuntimeError("auxiliary cache contains missing or unpinned files")
        for item in spec["files"]:
            if digest(root / item["path"]) != item.get("content_sha256", item["sha256"]):
                raise RuntimeError("auxiliary checksum mismatch: " + item["path"])

    def aux(self):
        self.check_enabled()
        spec = self.aux_spec()
        root = self.data / "aux" / spec["version"]
        if root.exists():
            self.verified_aux()
            return
        pending = root.with_name(root.name + ".next")
        archive = root.with_name(root.name + ".download")
        if pending.exists():
            shutil.rmtree(pending)
        if archive.exists():
            archive.unlink()
        pending.mkdir(parents=True)
        try:
            for item in spec["files"]:
                target = pending / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                compression = item.get("compression", "none")
                downloaded = target if compression == "none" else archive
                with urllib.request.urlopen(item["url"], timeout=120) as source:
                    with open(downloaded, "xb") as destination:
                        shutil.copyfileobj(source, destination, 1024 * 1024)
                if digest(downloaded) != item["sha256"]:
                    raise RuntimeError("auxiliary download checksum mismatch")
                if compression != "none":
                    opener = {"gzip": gzip.open, "bz2": bz2.open, "xz": lzma.open}[compression]
                    with opener(archive, "rb") as source, open(target, "xb") as destination:
                        shutil.copyfileobj(source, destination, 1024 * 1024)
                    archive.unlink()
                    if digest(target) != item["content_sha256"]:
                        raise RuntimeError("auxiliary expanded checksum mismatch")
            atomic_json(pending / "manifest.json", spec)
            os.replace(pending, root)
        finally:
            if pending.exists():
                shutil.rmtree(pending)
            if archive.exists():
                archive.unlink()
        self.state["aux_version"] = spec["version"]
        self.save()

    def child_env(self, config):
        env = dict(os.environ)
        env["PELIAS_CONFIG"] = str(config)
        return env

    def build(self):
        self.check_enabled()
        if not self.args.manual and not self.policy.get("auto_update", False):
            raise RuntimeError("automatic updates are disabled")
        smoke.validate_cases(self.policy.get("smoke"))
        aux_root = self.verified_aux()
        with self.source_locked():
            source = self.fingerprint()
            identifier = self.args.generation or (
                datetime.datetime.now(datetime.timezone.utc).strftime("g%Y%m%d%H%M%S-")
                + uuid.uuid4().hex[:8]
            )
            if not GENERATION.fullmatch(identifier) or identifier in self.state["generations"]:
                raise ValueError("generation name is invalid or already exists")
            root = self.generations / identifier
            index = self.alias + "-" + identifier
            if self.es.request("GET", "/" + index, missing={}):
                raise RuntimeError("generation index already exists; refusing to reuse it")
            root.mkdir(parents=True, exist_ok=False)
            (root / ".serving.lock").touch(exist_ok=False)
            record = {
                "id": identifier, "index": index, "phase": "building",
                "created_at": now(), "source": source,
                "aux_version": self.policy["aux"]["version"],
            }
            self.state["generations"][identifier] = record
            self.state["building"] = identifier
            self.save()
            try:
                config_path = self.make_config(root, index, aux_root)
                record["config_sha256"] = digest(config_path)
                self.save()
                self.pipeline(root, config_path)
                if source != self.fingerprint():
                    raise RuntimeError("source changed while importing")
                record["artifacts"] = {
                    relative: digest(root / relative) for relative in (
                        "placeholder/store.sqlite3", "interpolation/address.db",
                        "interpolation/street.db", "polylines/source.0sv",
                    )
                }
                record["aux_manifest_sha256"] = digest(aux_root / "manifest.json")
                self.check_index(record)
                self.es.request("PUT", "/" + index + "/_settings", {
                    "index.blocks.write": True,
                })
                record["phase"] = "built"
                record["built_at"] = now()
                self.save()
                if self.policy.get("auto_promote", False):
                    self.activate(identifier, rollback=False, source=source)
            except BaseException as error:
                if self.state.get("active") != identifier:
                    record["phase"] = "failed"
                record["error"] = str(error)
                record["failed_at"] = now()
                raise
            finally:
                self.state["building"] = None
                self.save()

    def check_index(self, record):
        index = record["index"]
        health = self.es.request(
            "GET", "/_cluster/health/" + index + "?wait_for_status=yellow&timeout=60s",
        )
        if health.get("timed_out") or health.get("status") not in ("yellow", "green"):
            raise RuntimeError("candidate Elasticsearch index is unhealthy")
        self.es.request("POST", "/" + index + "/_refresh")
        count = self.es.request("GET", "/" + index + "/_count")["count"]
        minimum = self.policy.get("min_documents", 1)
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
            raise ValueError("min_documents must be a positive integer")
        if count < minimum:
            raise RuntimeError("candidate has %d documents; minimum is %d" % (count, minimum))
        record["documents"] = count

    def activate(self, identifier, rollback=False, source=None):
        self.check_enabled()
        record = self.generation(identifier)
        if record["phase"] not in ("built", "active", "retired"):
            raise RuntimeError("only complete generations can be activated")
        config_path = self.generations / identifier / "pelias.json"
        if digest(config_path) != record.get("config_sha256"):
            raise RuntimeError("candidate configuration differs from the completed build")
        config = read_json(config_path)
        if any(
            config.get(section, {}).get("indexName") != record["index"]
            for section in ("api", "schema")
        ):
            raise RuntimeError("candidate configuration does not address its generation index")
        for relative, checksum in record.get("artifacts", {}).items():
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("invalid recorded artifact path")
            if digest(self.generations / identifier / path) != checksum:
                raise RuntimeError("candidate helper dataset differs from the completed build")
        if record.get("aux_manifest_sha256"):
            if not GENERATION.fullmatch(record["aux_version"]):
                raise RuntimeError("invalid auxiliary version in generation metadata")
            aux_root = self.data / "aux" / record["aux_version"]
            manifest = aux_root / "manifest.json"
            if digest(manifest) != record["aux_manifest_sha256"]:
                raise RuntimeError("candidate auxiliary version differs from the completed build")
            self.verify_aux_files(aux_root, read_json(manifest))
        if source is not None and source != record["source"]:
            raise RuntimeError("candidate source fingerprint does not match")
        if self.expected_source and record["source"]["sha256"] != self.expected_source:
            raise RuntimeError("candidate does not match the expected source SHA256")
        old_identifier = self.state.get("active")
        old = self.es.target(self.alias)
        expected_old = self.generation(old_identifier)["index"] if old_identifier else None
        if old != expected_old:
            raise RuntimeError("alias and persisted lifecycle disagree; refusing activation")
        self.check_index(record)
        results = self.candidate_smoke(identifier)
        service = {}
        if self.serving:
            old_selector = self.serving.selector()
            selected = old_selector.get(self.serving.label)
            if old_identifier and selected != old_identifier:
                raise RuntimeError("Service selector and persisted lifecycle disagree")
            if not old_identifier and selected not in (None, "", "none", "uninitialized", "disabled"):
                raise RuntimeError("untracked generation is selected by the serving Service")
            template = self.policy["serving"].get("deployment_template", "pelias")
            deployment_name = template + "-gen-" + identifier
            if len(deployment_name) > 63:
                raise ValueError("generation Deployment name exceeds 63 characters")
            record["serving_deployment"] = deployment_name
            self.save()
            self.serving.prepare(identifier)
            service = {
                "service_old": old_selector,
                "service_new": dict(old_selector, **{self.serving.label: identifier}),
            }
        # Persist the intent first: a crash after Elasticsearch commits is reconciled
        # against this exact old/new pair, never by guessing an index name.
        self.state["pending_activation"] = {
            "old": old_identifier, "new": identifier, "validated_at": now(),
            "rollback": rollback,
            **service,
        }
        self.save()
        self.es.switch(self.alias, old, record["index"])
        if self.serving:
            self.serving.select(service["service_old"], service["service_new"])
        self.finish_activation(identifier, old_identifier, results)

    def finish_activation(self, identifier, previous, results=None):
        record = self.generation(identifier)
        if previous and previous != identifier:
            self.generation(previous)["phase"] = "retired"
        self.state["last_good"] = previous if previous != identifier else self.state.get("last_good")
        self.state["active"] = identifier
        record["phase"] = "active"
        record["activated_at"] = now()
        if results is not None:
            record["smoke"] = results
        self.state.pop("pending_activation", None)
        self.save()

    def reconcile(self):
        pending = self.state.get("pending_activation")
        if pending:
            target = self.es.target(self.alias)
            new = self.generation(pending["new"])["index"]
            old = self.generation(pending["old"])["index"] if pending["old"] else None
            if target == new:
                if "service_new" in pending:
                    if not self.serving:
                        raise RuntimeError("interrupted cluster activation requires Kubernetes serving policy")
                    self.serving.prepare(pending["new"])
                    self.serving.select(pending["service_old"], pending["service_new"])
                self.finish_activation(pending["new"], pending["old"])
            elif target == old:
                if "service_new" in pending and self.serving.selector() != pending["service_old"]:
                    raise RuntimeError("interrupted precommit has an unexpected serving selector")
                self.state.pop("pending_activation")
                self.save()
            else:
                raise RuntimeError("ambiguous interrupted activation; operator recovery required")
        building = self.state.get("building")
        if building:
            record = self.generation(building)
            if record["phase"] == "building":
                record.update(phase="failed", error="previous lifecycle process interrupted")
            self.state["building"] = None
            self.save()

    def promote(self):
        with self.source_locked():
            source = self.fingerprint()
            self.activate(self.args.generation, source=source)

    def rollback(self):
        identifier = self.args.generation or self.state.get("last_good")
        self.activate(identifier, rollback=True)

    def cleanup(self):
        identifier = self.args.generation
        record = self.generation(identifier)
        protected = {
            self.state.get("active"), self.state.get("last_good"),
            self.state.get("building"),
        }
        pending = self.state.get("pending_activation", {})
        protected.update((pending.get("old"), pending.get("new")))
        if identifier in protected or self.es.target(self.alias) == record["index"]:
            raise RuntimeError("cannot clean active, last-good, building or pending generation")
        retain = self.policy.get("retain_generations", 2)
        if not isinstance(retain, int) or isinstance(retain, bool) or retain < 2:
            raise ValueError("retain_generations must be at least two")
        successful = sorted(
            (r for r in self.state["generations"].values()
             if r["phase"] in ("built", "active", "retired")),
            key=lambda r: r.get("built_at", r["created_at"]), reverse=True,
        )
        if identifier in {r["id"] for r in successful[:retain]}:
            raise RuntimeError("generation is protected by retention policy")
        root = self.generations / identifier
        if record.get("serving_deployment"):
            if not self.serving:
                raise RuntimeError("cluster generation cleanup requires Kubernetes serving policy")
            self.serving.remove(record["serving_deployment"], identifier)
        # Serving supervisors hold a shared read-only lock for the entire life of
        # a pinned stack, including while draining requests after a newer activation.
        with contextlib.ExitStack() as stack:
            if root.exists():
                serving_lock = stack.enter_context(open(root / ".serving.lock", "rb"))
                deadline = time.monotonic() + (120 if record.get("serving_deployment") else 0)
                while True:
                    try:
                        fcntl.flock(serving_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("generation is still held by a serving stack") from None
                        time.sleep(1)
            self.es.request("DELETE", "/" + record["index"], missing={})
            if root.exists():
                shutil.rmtree(root)
        del self.state["generations"][identifier]
        self.save()

    def make_config(self, root, index, aux_root):
        config = copy.deepcopy(self.native)
        config.get("elasticsearch", {}).pop("indexName", None)
        config.get("schema", {}).pop("typeName", None)
        if "spatial" in config.get("services", {}) or "spatial" in config.get("api", {}).get("services", {}):
            raise ValueError("this generation runtime requires WOF PIP, not a shared spatial service")
        config.setdefault("schema", {})["indexName"] = index
        config.setdefault("api", {})["indexName"] = index
        imports = config.setdefault("imports", {})
        imports.setdefault("adminLookup", {})["enabled"] = True
        # This is the native importer lookup URL; API helper URLs are separate.
        config.setdefault("services", {}).setdefault("pip", {})["url"] = "http://127.0.0.1:14200"
        wof = imports.setdefault("whosonfirst", {})
        wof["datapath"] = str(aux_root / "whosonfirst")
        wof["countryCode"] = self.policy["aux"]["countries"]
        # A cache must contain ready-to-import SQLite, not an archive or a URL.
        if not list((aux_root / "whosonfirst" / "sqlite").glob("whosonfirst-data-*.db")):
            raise RuntimeError("aux cache needs whosonfirst/sqlite/whosonfirst-data-*.db")
        osm = imports.setdefault("openstreetmap", {})
        osm["datapath"] = str(Path(self.args.source).parent)
        osm["import"] = [{"filename": Path(self.args.source).name}]
        osm["leveldbpath"] = str(root / "scratch" / "osm")
        imports.setdefault("polyline", {}).update(
            datapath=str(root / "polylines"), files=["source.0sv"],
        )
        csv = imports.get("csv", {})
        if csv.get("files"):
            csv["datapath"] = str(aux_root / "csv")
            for filename in csv["files"]:
                if Path(filename).name != filename or not (aux_root / "csv" / filename).is_file():
                    raise RuntimeError("CSV files must be explicitly pinned in aux/csv")
        services = config["api"].setdefault("services", {})
        for service, port in (("placeholder", 14100), ("pip", 14200), ("interpolation", 14300)):
            services.setdefault(service, {})["url"] = "http://127.0.0.1:%d" % port
        for path in (
            root / "scratch" / "osm", root / "scratch" / "tmp", root / "placeholder",
            root / "interpolation", root / "polylines",
        ):
            path.mkdir(parents=True, exist_ok=True)
        path = root / "pelias.json"
        atomic_json(path, config)
        return path

    def runtime_env(self, root, config_path):
        env = self.child_env(config_path)
        aux = self.data / "aux" / self.generation(root.name)["aux_version"]
        env.update({
            "PLACEHOLDER_DATA": str(root / "placeholder"),
            "WOF_DIR": str(aux / "whosonfirst" / "sqlite"),
            "WORKINGDIR": str(root),
            "BUILDDIR": str(root / "interpolation"),
            "PBF2JSON_FILE": str(self.args.source),
            "PBF2JSON_TMPDIR": str(root / "scratch" / "interpolation-leveldb"),
            "ADDRESS_DB": str(root / "interpolation" / "address.db"),
            "STREET_DB": str(root / "interpolation" / "street.db"),
            "TMPDIR": str(root / "scratch" / "tmp"),
            "SQLITE_TMPDIR": str(root / "scratch" / "tmp"),
            "CPUS": "1",
            # Propagate strict pipeline handling to upstream child bash scripts.
            "SHELLOPTS": "errexit:pipefail",
        })
        return env

    def module(self, name):
        return Path(self.args.modules) / name

    def module_run(self, name, argv, env):
        self.processes.run(argv, env=env, cwd=self.module(name))

    def pipeline(self, root, config_path):
        env = self.runtime_env(root, config_path)
        self.processes.run([
            sys.executable, str(Path(__file__).with_name("streets.py")),
            str(self.args.source), str(root / "polylines" / "source.0sv"),
            "--scratch", str(root / "scratch" / "streets"),
        ], env=env)
        self.module_run("schema", ["./bin/create_index"], env)
        self.module_run("placeholder", ["bash", "-eo", "pipefail", "./cmd/extract.sh"], env)
        self.module_run("placeholder", ["bash", "-eo", "pipefail", "./cmd/build.sh"], env)
        config = read_json(config_path)
        pip = self.processes.start(
            ["./bin/start"], env=dict(env, PORT="14200", HOST="127.0.0.1"),
            cwd=self.module("pip-service"),
        )
        try:
            self.wait_port(14200, [pip])
            importers = [
                ("whosonfirst", ["./bin/start"]), ("openstreetmap", ["./bin/start"]),
                ("polylines", ["./bin/start"]),
            ]
            if config["imports"].get("csv", {}).get("files"):
                importers.append(("csv-importer", ["./bin/parallel", "1"]))
            for module, argv in importers:
                self.module_run(module, argv, env)
                if pip.poll() is not None:
                    raise RuntimeError("candidate PIP service exited during import")
        finally:
            self.processes.stop(pip)
        env["POLYLINE_FILE"] = str(root / "polylines" / "source.0sv")
        for script in ("import.sh", "conflate_osm.sh", "vertices.sh"):
            self.module_run("interpolation", ["bash", "-eo", "pipefail", "./script/" + script], env)
        for path in (
            root / "placeholder" / "store.sqlite3",
            root / "interpolation" / "address.db", root / "interpolation" / "street.db",
        ):
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError("upstream pipeline did not produce " + str(path))
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as database:
                if database.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise RuntimeError("upstream produced an invalid SQLite database: " + str(path))
                table = {"address.db": "address", "street.db": "polyline"}.get(path.name)
                if table and database.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] < 1:
                    raise RuntimeError("upstream produced an empty helper dataset: " + str(path))

    def candidate_smoke(self, identifier):
        root = self.generations / identifier
        env = self.runtime_env(root, root / "pelias.json")
        with self.stack(root, env, 14000, (14100, 14200, 14300)):
            return smoke.run("http://127.0.0.1:14000", self.policy["smoke"])

    @contextlib.contextmanager
    def stack(self, root, env, api_port, helper_ports, public=False):
        processes = []
        commands = (
            ("placeholder", ["./cmd/server.sh"], helper_ports[0]),
            ("pip-service", ["./bin/start"], helper_ports[1]),
            ("interpolation", [
                "node", "./cmd/server.js", str(root / "interpolation" / "address.db"),
                str(root / "interpolation" / "street.db"),
            ], helper_ports[2]),
            ("api", ["./bin/start"], api_port),
        )
        try:
            for module, argv, port in commands:
                host = "0.0.0.0" if public and module == "api" else "127.0.0.1"
                child_env = dict(env, PORT=str(port), HOST=host)
                child = self.processes.start(argv, env=child_env, cwd=self.module(module))
                processes.append(child)
                self.wait_port(port, processes)
            yield processes
            if any(child.poll() is not None for child in processes):
                raise RuntimeError("candidate service exited during feature validation")
        finally:
            for child in reversed(processes):
                self.processes.stop(child)

    def probe(self):
        smoke.run("http://127.0.0.1:%d" % self.args.port, self.policy["smoke"])

    def serve(self):
        self.state = read_json(self.state_path)
        record = self.generation(self.args.generation)
        if record["phase"] not in ("built", "active", "retired"):
            raise RuntimeError("serving requires a completed generation")
        root = self.generations / self.args.generation
        with open(root / ".serving.lock", "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            original = root / "pelias.json"
            if digest(original) != record["config_sha256"]:
                raise RuntimeError("serving configuration differs from its completed build")
            config = read_json(original)
            if config["api"]["indexName"] != record["index"]:
                raise RuntimeError("serving API must be pinned to its concrete generation index")
            helpers = (4100, 4200, 4300)
            for service, port in zip(("placeholder", "pip", "interpolation"), helpers):
                config["api"]["services"][service]["url"] = "http://127.0.0.1:%d" % port
            config.setdefault("services", {}).setdefault("pip", {})["url"] = "http://127.0.0.1:4200"
            scratch = Path(self.args.scratch)
            scratch.mkdir(parents=True, exist_ok=True)
            config_path = scratch / "pelias.json"
            atomic_json(config_path, config)
            env = self.runtime_env(root, config_path)
            env["TMPDIR"] = str(scratch)
            env["SQLITE_TMPDIR"] = str(scratch)
            env["PBF2JSON_TMPDIR"] = str(scratch / "pbf2json")
            with self.stack(root, env, self.args.port, helpers, public=True) as children:
                self.probe()
                while True:
                    if any(child.poll() is not None for child in children):
                        raise RuntimeError("generation helper/API process exited")
                    time.sleep(1)

    def wait_port(self, port, children):
        deadline = time.monotonic() + self.policy.get("startup_timeout", 600)
        while time.monotonic() < deadline:
            if any(child.poll() is not None for child in children):
                raise RuntimeError("candidate service exited before readiness")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    return
            except OSError:
                time.sleep(0.25)
        raise RuntimeError("candidate service did not listen on port %d" % port)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "promote", "rollback", "cleanup", "aux", "serve", "probe"))
    parser.add_argument("--config", default=os.environ.get("PELIAS_CONFIG", "/config/pelias.json"))
    parser.add_argument("--policy", default=os.environ.get("PELIAS_POLICY", "/config/policy.json"))
    parser.add_argument("--data", default="/data")
    parser.add_argument("--status", default="/status/pelias.json")
    parser.add_argument("--source", default="/planet/europa.osm.pbf")
    parser.add_argument("--source-lock", default="/planet/.pelias-source.lock")
    parser.add_argument(
        "--expected-source-sha256", default=os.environ.get("PELIAS_EXPECTED_SOURCE_SHA256"),
        help="reject a queued build or candidate whose source digest differs",
    )
    parser.add_argument("--modules", default="/code")
    parser.add_argument("--generation", default=os.environ.get("PELIAS_GENERATION"))
    parser.add_argument("--scratch", default="/scratch")
    parser.add_argument("--port", type=int, default=4000)
    parser.add_argument("--manual", action="store_true", help="explicit build despite auto_update=false")
    args = parser.parse_args(argv)
    controller = Controller(args)
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, controller.processes.signal)
    try:
        if args.command in ("serve", "probe"):
            getattr(controller, args.command)()
            return 0
        with controller.locked():
            try:
                controller.reconcile()
                controller.state["operation"] = {
                    "command": args.command, "phase": "running", "started_at": now(),
                }
                controller.save()
                getattr(controller, args.command)()
                controller.state["operation"].update(phase="succeeded", finished_at=now())
                controller.save()
            except BaseException as error:
                controller.state["operation"] = {
                    "command": args.command, "phase": "failed",
                    "finished_at": now(), "error": str(error),
                }
                controller.save()
                raise
            finally:
                # No child may outlive the lifecycle lock, including on SIGTERM.
                controller.processes.close()
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print("Pelias lifecycle failed: %s" % error, file=sys.stderr)
        return 1
    finally:
        controller.processes.close()


if __name__ == "__main__":
    sys.exit(main())
