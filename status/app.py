from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import socket
import sys
import urllib.request
import urllib.error
from datetime import datetime

HOST = os.environ.get("STATUS_HOST", "0.0.0.0")
PORT = int(os.environ.get("STATUS_PORT", "8080"))
DATA_DIR = os.environ.get("OSM_DATA_DIR", "/mnt/data/OSM")

SERVICES = [
    ("postgres", "postgres.osm.svc.cluster.local", 5432, "tcp", None),
    ("tileserver", "tileserver-gl.osm.svc.cluster.local", 80, "http", "/"),
    ("nominatim", "nominatim.osm.svc.cluster.local", 8080, "http", "/"),
    ("valhalla", "valhalla.osm.svc.cluster.local", 8002, "http", "/"),
]


def check_tcp(host, port, timeout=2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"tcp connect ok on {host}:{port}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def check_http(url, timeout=3):
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "osm-status/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, f"http {response.status} from {url}"
    except urllib.error.HTTPError as exc:
        return True, f"http {exc.code} from {url}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def collect_status():
    results = []
    for name, host, port, kind, path in SERVICES:
        if kind == "tcp":
            ok, detail = check_tcp(host, port)
        else:
            url = f"http://{host}:{port}{path or '/'}"
            ok, detail = check_http(url)
        results.append({"name": name, "ok": ok, "detail": detail})

    import_path = os.path.join(DATA_DIR, "import")
    import_files = []
    if os.path.isdir(import_path):
        for entry in sorted(os.listdir(import_path)):
            if entry.endswith(".osm.pbf") or entry.endswith(".pbf") or entry.endswith(".meta"):
                import_files.append(entry)

    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "services": results,
        "import_dir": import_path,
        "import_files": import_files,
    }


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in {"/", "/index.html"}:
            html = """
            <html><head><title>OSM Status</title></head>
            <body>
              <h1>OSM Local Stack Status</h1>
              <p>Use the endpoints below to test the stack.</p>
              <ul>
                <li><a href="/healthz">/healthz</a></li>
                <li><a href="/api/status">/api/status</a></li>
                <li><a href="/test/postgres">/test/postgres</a></li>
                <li><a href="/test/tileserver">/test/tileserver</a></li>
                <li><a href="/test/nominatim">/test/nominatim</a></li>
                <li><a href="/test/valhalla">/test/valhalla</a></li>
              </ul>
            </body></html>
            """
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/healthz":
            self._send_json({"status": "ok"})
            return

        if self.path == "/api/status":
            self._send_json(collect_status())
            return

        if self.path.startswith("/test/"):
            name = self.path.split("/", 2)[-1]
            if name == "postgres":
                ok, detail = check_tcp("postgres.osm.svc.cluster.local", 5432)
            elif name == "tileserver":
                ok, detail = check_http("http://tileserver-gl.osm.svc.cluster.local/")
            elif name == "nominatim":
                ok, detail = check_http("http://nominatim.osm.svc.cluster.local:8080/")
            elif name == "valhalla":
                ok, detail = check_http("http://valhalla.osm.svc.cluster.local:8002/")
            else:
                self._send_json({"error": "unknown test target"}, 404)
                return
            self._send_json({"test": name, "ok": ok, "detail": detail})
            return

        self._send_json({"error": "not found"}, 404)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), Handler)
    print(f"OSM status server listening on {HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
