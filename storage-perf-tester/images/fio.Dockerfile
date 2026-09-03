# Optional offline/pinned fio worker image.
#
# By default storage-perf-tester uses plain "alpine:3.20" and installs fio
# via `apk add` when the worker Job starts (see
# scripts-embedded/entrypoint-fio.sh). That requires outbound network
# access from cluster nodes to the Alpine package mirrors.
#
# If your cluster has no outbound internet access, build this image once,
# push it to a registry your cluster can pull from, and pass
# `--fio-image <your-registry>/spt-fio:latest` (or set FIO_IMAGE).
#
#   docker build -t <your-registry>/spt-fio:latest -f images/fio.Dockerfile .
#   docker push <your-registry>/spt-fio:latest

FROM alpine:3.20
RUN apk add --no-cache fio jq coreutils
