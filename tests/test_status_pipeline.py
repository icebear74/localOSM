import importlib.util
import json
import pathlib
import unittest

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / 'status' / 'app.py'
spec = importlib.util.spec_from_file_location('status_app', MODULE_PATH)
status_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status_app)


class StatusPipelineTests(unittest.TestCase):
    def test_prepare_geojson_configmap_payload(self):
        name, data = status_app._prepare_configmap_payload(
            'geojson', json.dumps({'content': '{"type":"FeatureCollection","features":[]}'}).encode('utf-8')
        )
        self.assertEqual(name, 'cm-extract-polygon')
        self.assertIn('polygon.geojson', data)
        self.assertIn('FeatureCollection', data['polygon.geojson'])
        self.assertTrue(data['polygon.geojson'].endswith('\n'))

    def test_prepare_filter_configmap_payload(self):
        name, data = status_app._prepare_configmap_payload('filter', b'highway\nrailway')
        self.assertEqual(name, 'cm-worldmap-filter')
        self.assertEqual(data['filter.txt'], 'highway\nrailway\n')

    def test_clone_job_manifest_strips_server_metadata(self):
        manifest = {
            'apiVersion': 'batch/v1',
            'kind': 'Job',
            'metadata': {
                'name': 'old-name',
                'uid': '123',
                'resourceVersion': '9',
                'creationTimestamp': 'now',
                'managedFields': ['x'],
            },
            'spec': {
                'selector': {'matchLabels': {'job-name': 'old-name'}},
                'template': {
                    'metadata': {
                        'labels': {
                            'app': 'orchestrator',
                            'batch.kubernetes.io/controller-uid': 'abc',
                            'batch.kubernetes.io/job-name': 'old-name',
                            'controller-uid': 'abc',
                            'job-name': 'old-name',
                        }
                    },
                    'spec': {'containers': []},
                },
            },
            'status': {'succeeded': 1},
        }
        cloned = status_app._clone_job_manifest(manifest, new_name='new-name')
        self.assertEqual(cloned['metadata']['name'], 'new-name')
        self.assertNotIn('uid', cloned['metadata'])
        self.assertNotIn('resourceVersion', cloned['metadata'])
        self.assertNotIn('status', cloned)
        self.assertNotIn('selector', cloned['spec'])
        self.assertEqual(cloned['spec']['template']['metadata']['labels']['app'], 'orchestrator')
        self.assertNotIn('batch.kubernetes.io/controller-uid', cloned['spec']['template']['metadata']['labels'])
        self.assertNotIn('batch.kubernetes.io/job-name', cloned['spec']['template']['metadata']['labels'])
        self.assertNotIn('controller-uid', cloned['spec']['template']['metadata']['labels'])
        self.assertNotIn('job-name', cloned['spec']['template']['metadata']['labels'])

    def test_summarize_job_uses_latest_matching_resource(self):
        original = status_app._list_jobs
        try:
            status_app._list_jobs = lambda: [
                {
                    'metadata': {'name': 'planet-update-manual-1', 'creationTimestamp': '2026-01-01T00:00:00Z'},
                    'status': {'failed': 1},
                },
                {
                    'metadata': {'name': 'planet-update-manual-2', 'creationTimestamp': '2026-01-02T00:00:00Z'},
                    'status': {'succeeded': 1, 'completionTime': '2026-01-02T00:10:00Z'},
                },
            ]
            summary = status_app._summarize_job('planet-update')
            self.assertTrue(summary['exists'])
            self.assertEqual(summary['resource_name'], 'planet-update-manual-2')
            self.assertEqual(summary['status'], 'completed')
        finally:
            status_app._list_jobs = original

    def test_build_orchestrator_job_manifest_sets_ttl_and_name(self):
        manifest = status_app._build_orchestrator_job_manifest('tileserver-import-orchestrator')
        self.assertEqual(manifest['metadata']['name'], 'tileserver-import-orchestrator')
        self.assertEqual(manifest['spec']['ttlSecondsAfterFinished'], 43200)
        self.assertEqual(manifest['spec']['template']['spec']['containers'][0]['command'], ['/bin/sh', '-c'])

    def test_build_orchestrator_job_manifest_uses_configured_namespace(self):
        original_namespace = status_app.NAMESPACE
        try:
            status_app.NAMESPACE = 'custom-namespace'
            manifest = status_app._build_orchestrator_job_manifest('tileserver-import-orchestrator')
            script = manifest['spec']['template']['spec']['containers'][0]['args'][0]
            self.assertIn("NAMESPACE='custom-namespace'", script)
        finally:
            status_app.NAMESPACE = original_namespace

    def test_trigger_pipeline_job_reraises_404_for_unsupported_orchestrator(self):
        original_kube = status_app.KUBE

        class DummyKube:
            def get_job(self, name):
                raise RuntimeError('404 Not Found')

            def create_job(self, manifest):
                raise AssertionError('create_job should not be called for unsupported fallback targets')

        status_app.KUBE = DummyKube()
        try:
            with self.assertRaises(RuntimeError):
                status_app._trigger_pipeline_job('nominatim-import')
        finally:
            status_app.KUBE = original_kube

    def test_apply_auto_update_cronjob_state_patches_suspend_flag(self):
        original_kube = status_app.KUBE

        class DummyKube:
            def __init__(self):
                self.calls = []

            def patch_cronjob(self, name, body):
                self.calls.append((name, body))

        dummy_kube = DummyKube()
        status_app.KUBE = dummy_kube
        try:
            status_app._apply_auto_update_cronjob_state(False)
            self.assertEqual(dummy_kube.calls, [('planet-update', {'spec': {'suspend': True}})])
        finally:
            status_app.KUBE = original_kube


if __name__ == '__main__':
    unittest.main()
