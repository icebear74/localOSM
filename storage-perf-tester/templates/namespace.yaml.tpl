apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: storage-perf-tester
    app.kubernetes.io/managed-by: storage-perf-tester
