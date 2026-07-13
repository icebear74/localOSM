from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import concurrent.futures
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HOST = os.environ.get('STATUS_HOST', '0.0.0.0')
PORT = int(os.environ.get('STATUS_PORT', '8080'))
DATA_DIR = os.environ.get('OSM_DATA_DIR', '/mnt/data/OSM')
NAMESPACE = os.environ.get('OSM_NAMESPACE', 'osm')

STATUS_DIR = os.path.join(DATA_DIR, 'status')
LIBRARY_DIR = os.path.join(DATA_DIR, 'library')
IMPORT_DIR = os.path.join(DATA_DIR, 'import')
VALHALLA_DIR = os.path.join(DATA_DIR, 'valhalla')
VALHALLA_TILE_DIR = os.path.join(VALHALLA_DIR, 'tiles')
NOMINATIM_DIR = os.path.join(DATA_DIR, 'nominatim')
TILESERVER_DIR = os.path.join(DATA_DIR, 'tileserver')
ORCHESTRATOR_STATE_FILE = os.path.join(STATUS_DIR, 'orchestrator.json')
COUNTRIES_FILE = os.path.join(STATUS_DIR, 'countries.json')
CONFIG_FILE = os.path.join(STATUS_DIR, 'config.json')
MERGED_PBF = os.path.join(IMPORT_DIR, 'planet.osm.pbf')

SERVICES = [
    ('postgres', 'postgres.osm.svc.cluster.local', 5432, 'tcp', None),
    ('tileserver', 'tileserver-gl.osm.svc.cluster.local', 80, 'http', '/'),
    ('nominatim', 'nominatim.osm.svc.cluster.local', 8080, 'http', '/'),
    ('valhalla', 'valhalla.osm.svc.cluster.local', 8002, 'http', '/'),
    ('web', 'web.osm.svc.cluster.local', 8080, 'http', '/healthz'),
]

CONFIG_DEFAULTS = {
    'node_url': '',
    'auto_update_enabled': False,
    'auto_update_time': '03:00',
    'routing_costing_models': {
        'car': {'enabled': True},
        'foot': {'enabled': True},
        'bicycle': {'enabled': True},
    },
    'routing_speeds': {
        'car': 120,
        'foot': 5,
        'bicycle': 25,
    },
    'routing_advanced': {
        'car': {'toll_factor': 1.0, 'unpaved_factor': 1.0, 'ferry_factor': 1.0},
        'foot': {'hill_factor': 1.0, 'unpaved_factor': 1.0},
        'bicycle': {'hill_factor': 1.0, 'unpaved_factor': 1.0},
    },
}

INDEX_HTML_PATH = os.path.join(os.path.dirname(__file__), 'index.html')


def load_index_html():
    try:
        with open(INDEX_HTML_PATH, encoding='utf-8') as handle:
            return handle.read()
    except OSError:
        return f'<!doctype html><html lang="de"><head><meta charset="utf-8"><title>localOSM Status</title></head><body><h1>localOSM Status</h1><p>Index template not available at {INDEX_HTML_PATH!r}.</p></body></html>'


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def clone_default(value):
    return json.loads(json.dumps(value))


def ensure_dirs():
    for path in (STATUS_DIR, LIBRARY_DIR, IMPORT_DIR, VALHALLA_DIR, VALHALLA_TILE_DIR, NOMINATIM_DIR, TILESERVER_DIR):
        os.makedirs(path, exist_ok=True)


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return clone_default(default)


def save_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f'{path}.{threading.get_ident()}.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def load_config():
    data = load_json(CONFIG_FILE, CONFIG_DEFAULTS)
    config = clone_default(CONFIG_DEFAULTS)
    if isinstance(data, dict):
        config.update({key: data.get(key, value) for key, value in CONFIG_DEFAULTS.items()})
    config['node_url'] = str(config.get('node_url') or '').strip()
    config['auto_update_enabled'] = bool(config.get('auto_update_enabled'))
    config['auto_update_time'] = str(config.get('auto_update_time') or '03:00')
    return config


def save_config(data):
    config = clone_default(CONFIG_DEFAULTS)
    config.update({key: data.get(key, value) for key, value in CONFIG_DEFAULTS.items()})
    save_json(CONFIG_FILE, config)
    return config


def valid_time_value(value):
    import re
    return bool(re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value or ''))


def apply_config_update(payload):
    config = load_config()
    if 'node_url' in payload:
        config['node_url'] = str(payload.get('node_url') or '').strip()
    if 'auto_update_enabled' in payload:
        config['auto_update_enabled'] = bool(payload.get('auto_update_enabled'))
    if 'auto_update_time' in payload:
        time_value = str(payload.get('auto_update_time') or '').strip()
        if not valid_time_value(time_value):
            raise ValueError('auto_update_time must use HH:MM format.')
        config['auto_update_time'] = time_value
    for key in ('routing_costing_models', 'routing_speeds', 'routing_advanced'):
        if key in payload:
            config[key] = payload[key]
    return save_config(config)


def check_tcp(host, port, timeout=2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f'TCP ok ({host}:{port})'
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def check_http(url, timeout=3):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'osm-status/1.0'})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return True, f'HTTP {response.status}'
    except urllib.error.HTTPError as exc:
        return True, f'HTTP {exc.code}'
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def list_import_files():
    files = []
    if os.path.isdir(IMPORT_DIR):
        for entry in sorted(os.listdir(IMPORT_DIR)):
            full_path = os.path.join(IMPORT_DIR, entry)
            try:
                size_mb = round(os.path.getsize(full_path) / (1024 * 1024), 1)
                mtime = datetime.fromtimestamp(os.path.getmtime(full_path), timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
            except OSError:
                size_mb, mtime = 0, '?'
            files.append({'name': entry, 'size_mb': size_mb, 'mtime': mtime})
    return files


def read_orchestrator_state():
    return load_json(
        ORCHESTRATOR_STATE_FILE,
        {'phase': 'idle', 'progress': 0, 'message': 'Noch kein Import gestartet.', 'detail': '', 'updated_at': now_iso()},
    )


def dir_size_mb(path):
    total = 0
    try:
        for dirpath, _, files in os.walk(path):
            for filename in files:
                full_path = os.path.join(dirpath, filename)
                try:
                    total += os.path.getsize(full_path)
                except OSError:
                    pass
    except OSError:
        pass
    return round(total / (1024 * 1024), 1)


def collect_status():
    ensure_dirs()

    def _check(svc):
        name, host, port, kind, path = svc
        ok, detail = check_tcp(host, port) if kind == 'tcp' else check_http(f'http://{host}:{port}{path or "/"}')
        return {'name': name, 'ok': ok, 'detail': detail}

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(SERVICES)))
    try:
        futures = [pool.submit(_check, svc) for svc in SERVICES]
        services = [future.result(timeout=8) for future in futures]
    except Exception:  # noqa: BLE001
        services = [{'name': svc[0], 'ok': False, 'detail': 'unavailable'} for svc in SERVICES]
    finally:
        pool.shutdown(wait=False)

    return {
        'timestamp': now_iso(),
        'services': services,
        'import_dir': IMPORT_DIR,
        'import_files': list_import_files(),
        'valhalla_tiles_mb': dir_size_mb(VALHALLA_TILE_DIR),
        'orchestrator': read_orchestrator_state(),
    }


def run_command(args, *, input_text=None, check=True, timeout=60):
    try:
        result = subprocess.run(args, input=input_text, text=True, capture_output=True, check=False, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(f"Command not found: {args[0]!r}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(args)}")
    if check and result.returncode != 0:
        output = (result.stderr or result.stdout or '').strip()
        raise RuntimeError(output or f"Command failed: {' '.join(args)}")
    return result.stdout.strip()


def calculate_multi_leg_route(payload):
    waypoints = payload.get('waypoints', [])
    costing = payload.get('costing', 'auto')
    if not waypoints or len(waypoints) < 2:
        raise ValueError('At least 2 waypoints required.')

    locations = []
    for wp in waypoints:
        parts = wp.strip().split(',')
        if len(parts) != 2:
            raise ValueError(f"Invalid waypoint format: {wp}")
        try:
            locations.append({'lat': float(parts[0].strip()), 'lon': float(parts[1].strip())})
        except ValueError as exc:
            try:
                resp = urllib.request.urlopen(
                    f'http://nominatim.osm.svc.cluster.local:8080/search?q={urllib.parse.quote(wp.strip())}&format=json&limit=1',
                    timeout=5,
                )
                data = json.loads(resp.read().decode('utf-8'))
                if data:
                    locations.append({'lat': float(data[0]['lat']), 'lon': float(data[0]['lon'])})
                else:
                    raise ValueError(f'Could not geocode address {wp!r}')
            except Exception as geocode_exc:  # noqa: BLE001
                raise ValueError(f"Invalid waypoint '{wp}': {exc}; geocoding failed: {geocode_exc}")

    costing_map = {'auto': 'auto', 'car': 'auto', 'foot': 'pedestrian', 'pedestrian': 'pedestrian', 'bicycle': 'bicycle', 'bike': 'bicycle'}
    valhalla_costing = costing_map.get(str(costing).lower(), 'auto')
    request = {'locations': locations, 'costing': valhalla_costing, 'directions_options': {'language': 'en'}}
    try:
        req = urllib.request.Request(
            'http://valhalla.osm.svc.cluster.local:8002/route',
            data=json.dumps(request).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode('utf-8'))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f'Valhalla routing failed: {exc}')

    legs = result.get('trip', {}).get('legs', [])
    if not legs:
        raise ValueError('No route found for the given waypoints.')
    total_distance = sum(leg.get('distance', 0) for leg in legs)
    total_time = sum(leg.get('time', 0) for leg in legs)
    return {
        'distance': total_distance,
        'time': total_time,
        'distance_km': round(total_distance / 1000, 2),
        'time_minutes': int(round(total_time / 60)),
        'waypoints_count': len(waypoints),
        'costing': costing,
    }


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        body = json.dumps(payload, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in {'/', '/index.html'}:
            body = load_index_html().encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/healthz':
            self._send_json({'status': 'ok'})
            return
        if self.path == '/api/status':
            try:
                self._send_json(collect_status())
            except Exception as exc:  # noqa: BLE001
                self._send_json({'error': str(exc)}, 500)
            return
        if self.path == '/api/config':
            self._send_json(load_config())
            return
        if self.path.startswith('/test/'):
            name = self.path.split('/', 2)[-1]
            if name == 'postgres':
                ok, detail = check_tcp('postgres.osm.svc.cluster.local', 5432)
            elif name == 'tileserver':
                ok, detail = check_http('http://tileserver-gl.osm.svc.cluster.local/')
            elif name == 'nominatim':
                ok, detail = check_http('http://nominatim.osm.svc.cluster.local:8080/')
            elif name == 'valhalla':
                ok, detail = check_http('http://valhalla.osm.svc.cluster.local:8002/')
            elif name == 'web':
                ok, detail = check_http('http://web.osm.svc.cluster.local:8080/healthz')
            else:
                self._send_json({'error': 'unknown test target'}, 404)
                return
            self._send_json({'test': name, 'ok': ok, 'detail': detail})
            return
        self._send_json({'error': 'not found'}, 404)

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', '0'))
        raw_body = self.rfile.read(content_length) if content_length else b'{}'
        try:
            payload = json.loads(raw_body.decode('utf-8'))
        except Exception:  # noqa: BLE001
            self._send_json({'error': 'invalid json'}, 400)
            return
        if self.path == '/api/config':
            try:
                config = apply_config_update(payload)
            except ValueError as exc:
                self._send_json({'error': str(exc)}, 400)
                return
            self._send_json(config)
            return
        if self.path == '/api/routing/calculate':
            try:
                self._send_json(calculate_multi_leg_route(payload))
                return
            except ValueError as exc:
                self._send_json({'error': str(exc)}, 400)
            except RuntimeError as exc:
                self._send_json({'error': str(exc)}, 503)
            return
        self._send_json({'error': 'not found'}, 404)

    def log_message(self, format, *args):
        return


if __name__ == '__main__':
    ensure_dirs()
    if read_orchestrator_state().get('running'):
        save_json(ORCHESTRATOR_STATE_FILE, {
            'running': False,
            'phase': 'interrupted',
            'progress': 0,
            'message': 'Workflow was interrupted by a pod restart.',
            'detail': 'Start a new workflow to continue.',
            'updated_at': now_iso(),
        })
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f'OSM status server listening on {HOST}:{PORT}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
