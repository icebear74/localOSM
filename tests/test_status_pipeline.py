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
            'spec': {'template': {'spec': {'containers': []}}},
            'status': {'succeeded': 1},
        }
        cloned = status_app._clone_job_manifest(manifest, new_name='new-name')
        self.assertEqual(cloned['metadata']['name'], 'new-name')
        self.assertNotIn('uid', cloned['metadata'])
        self.assertNotIn('resourceVersion', cloned['metadata'])
        self.assertNotIn('status', cloned)

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


if __name__ == '__main__':
    unittest.main()
