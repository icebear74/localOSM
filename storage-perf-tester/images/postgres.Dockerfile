# Optional offline/pinned PostgreSQL worker image.
#
# By default storage-perf-tester uses the official "postgres:16-alpine"
# image directly (it already ships initdb/pg_ctl/psql/pgbench and
# su-exec, so no extra packages need to be installed at container start).
#
# This Dockerfile only exists so you can pin/mirror a specific digest into
# your own registry for offline/air-gapped clusters:
#
#   docker build -t <your-registry>/spt-postgres:latest -f images/postgres.Dockerfile .
#   docker push <your-registry>/spt-postgres:latest

FROM postgres:16-alpine
