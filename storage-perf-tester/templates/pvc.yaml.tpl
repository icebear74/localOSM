apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${PVC_NAME}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: storage-perf-tester
    app.kubernetes.io/managed-by: storage-perf-tester
    spt-run-id: "${RUN_ID}"
    spt-storageclass: "${STORAGECLASS_LABEL}"
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: ${STORAGECLASS}
  resources:
    requests:
      storage: ${PVC_SIZE}
