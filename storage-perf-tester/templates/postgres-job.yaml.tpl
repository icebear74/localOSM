apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: storage-perf-tester
    app.kubernetes.io/managed-by: storage-perf-tester
    spt-run-id: "${RUN_ID}"
    spt-storageclass: "${STORAGECLASS_LABEL}"
    spt-workload: "${WORKLOAD}"
    spt-queue-depth: "${QUEUE_DEPTH}"
    spt-repeat: "${REPEAT}"
spec:
  backoffLimit: 0
  activeDeadlineSeconds: ${JOB_TIMEOUT}
  ttlSecondsAfterFinished: ${TTL_SECONDS}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: storage-perf-tester
        spt-run-id: "${RUN_ID}"
        spt-workload: "${WORKLOAD}"
    spec:
      restartPolicy: Never
      containers:
        - name: worker
          image: ${POSTGRES_IMAGE}
          command: ["/bin/bash", "/scripts/entrypoint-postgres.sh"]
          env:
            - name: WORKLOAD
              value: "${WORKLOAD}"
            - name: QUEUE_DEPTH
              value: "${QUEUE_DEPTH}"
            - name: DURATION
              value: "${DURATION}"
            - name: REPEAT
              value: "${REPEAT}"
            - name: MOUNT_PATH
              value: "${MOUNT_PATH}"
            - name: PGBENCH_SCALE
              value: "${PGBENCH_SCALE}"
          resources:
            requests:
              cpu: "${CPU_REQUEST}"
              memory: "${MEMORY_REQUEST}"
            limits:
              cpu: "${CPU_LIMIT}"
              memory: "${MEMORY_LIMIT}"
          volumeMounts:
            - name: data
              mountPath: ${MOUNT_PATH}
            - name: scripts
              mountPath: /scripts
              readOnly: true
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: ${PVC_NAME}
        - name: scripts
          configMap:
            name: ${CONFIGMAP_NAME}
            defaultMode: 0555
