ARG NODE_IMAGE=node:22.22.0-bookworm@sha256:20a424ecd1d2064a44e12fe287bf3dae443aab31dc5e0c0cb6c74bef9c78911c
FROM ${NODE_IMAGE}

USER root
ENV TMPDIR=/var/cache/pelias-build
RUN mkdir -p "$TMPDIR" && apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git python3 build-essential pkg-config \
    sqlite3 libsqlite3-dev zlib1g-dev osmium-tool unzip bzip2 \
    && rm -rf /var/lib/apt/lists/*

COPY pelias/images.lock.json /opt/pelias/images.lock.json
RUN python3 - <<'PY'
import json
import pathlib
import subprocess

lock = json.loads(pathlib.Path("/opt/pelias/images.lock.json").read_text())
for name, source in lock["repositories"].items():
    if name == "libpostal-server":
        continue
    target = pathlib.Path("/code") / name
    target.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "remote", "add", "origin", source["url"]], check=True)
    subprocess.run(["git", "-C", str(target), "fetch", "--quiet", "--depth=1", "origin", source["commit"]], check=True)
    subprocess.run(["git", "-C", str(target), "checkout", "--quiet", "--detach", "FETCH_HEAD"], check=True)
    actual = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip()
    if actual != source["commit"]:
        raise RuntimeError("source revision mismatch: " + name)
    if (target / "package.json").exists():
        # Some Pelias repositories deliberately do not publish npm lockfiles.
        command = "ci" if (target / "package-lock.json").exists() else "install"
        subprocess.run(["npm", command, "--omit=dev", "--no-audit", "--no-fund"], cwd=target, check=True)
        subprocess.run(["npm", "ls", "--omit=dev", "--depth=0"], cwd=target, check=True)
PY

WORKDIR /code
ENV PELIAS_CONFIG=/config/pelias.json
CMD ["python3", "/runtime/controller.py", "--help"]
