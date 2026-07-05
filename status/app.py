from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import socket
import urllib.request
import urllib.error
from datetime import datetime

HOST = os.environ.get("STATUS_HOST", "0.0.0.0")
PORT = int(os.environ.get("STATUS_PORT", "8080"))
DATA_DIR = os.environ.get("OSM_DATA_DIR", "/mnt/data/OSM")

SERVICES = [
    ("postgres",   "postgres.osm.svc.cluster.local",    5432, "tcp",  None),
    ("tileserver", "tileserver-gl.osm.svc.cluster.local", 80, "http", "/"),
    ("nominatim",  "nominatim.osm.svc.cluster.local",   8080, "http", "/"),
    ("valhalla",   "valhalla.osm.svc.cluster.local",    8002, "http", "/"),
    ("web",        "web.osm.svc.cluster.local",         8080, "http", "/healthz"),
]


def check_tcp(host, port, timeout=2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"TCP ok ({host}:{port})"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def check_http(url, timeout=3):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "osm-status/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return True, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return True, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def dir_size_mb(path):
    total = 0
    try:
        for dirpath, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    except OSError:
        pass
    return round(total / (1024 * 1024), 1)


def collect_status():
    services = []
    for name, host, port, kind, path in SERVICES:
        if kind == "tcp":
            ok, detail = check_tcp(host, port)
        else:
            ok, detail = check_http(f"http://{host}:{port}{path or '/'}")
        services.append({"name": name, "ok": ok, "detail": detail})

    import_path = os.path.join(DATA_DIR, "import")
    import_files = []
    if os.path.isdir(import_path):
        for entry in sorted(os.listdir(import_path)):
            full = os.path.join(import_path, entry)
            try:
                size_mb = round(os.path.getsize(full) / (1024 * 1024), 1)
                mtime = datetime.utcfromtimestamp(os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M UTC")
            except OSError:
                size_mb, mtime = 0, "?"
            import_files.append({"name": entry, "size_mb": size_mb, "mtime": mtime})

    tile_size_mb = dir_size_mb(os.path.join(DATA_DIR, "valhalla", "tiles"))

    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "services": services,
        "import_dir": import_path,
        "import_files": import_files,
        "valhalla_tiles_mb": tile_size_mb,
    }


INDEX_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>localOSM – Status</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: Arial, sans-serif; background: #f0f2f5; padding: 1.5rem; }
    h1 { font-size: 1.3rem; margin-bottom: 1rem; color: #2c7ab5; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
    .card { background: #fff; border-radius: 8px; padding: 1rem; box-shadow: 0 1px 4px rgba(0,0,0,.1); }
    .card h2 { font-size: 0.85rem; color: #666; margin-bottom: 0.5rem; text-transform: uppercase; letter-spacing: .05em; }
    .svc { display: flex; align-items: center; gap: 0.5rem; padding: 0.4rem 0; border-bottom: 1px solid #f0f0f0; font-size: 0.85rem; }
    .svc:last-child { border-bottom: none; }
    .dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
    .dot.ok { background: #2ecc71; }
    .dot.err { background: #e74c3c; }
    .svc-name { font-weight: 600; min-width: 80px; }
    .svc-detail { color: #888; font-size: 0.75rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 160px; }
    .file-list { font-size: 0.82rem; }
    .file-row { display: flex; justify-content: space-between; padding: 0.3rem 0; border-bottom: 1px solid #f5f5f5; }
    .file-row:last-child { border-bottom: none; }
    .file-name { color: #333; }
    .file-meta { color: #888; font-size: 0.75rem; text-align: right; }
    .refresh { font-size: 0.75rem; color: #999; margin-bottom: 1rem; }
    .links a { display: inline-block; margin-right: 1rem; margin-bottom: 0.5rem; padding: 0.4rem 0.8rem;
               background: #2c7ab5; color: #fff; text-decoration: none; border-radius: 4px; font-size: 0.82rem; }
    .links a:hover { background: #1a5a8a; }
    .stat-big { font-size: 1.8rem; font-weight: 700; color: #2c7ab5; }
    .stat-label { font-size: 0.75rem; color: #888; }
    .all-ok { color: #2ecc71; font-weight: 700; }
    .has-err { color: #e74c3c; font-weight: 700; }
    #ts { color: #aaa; font-size: 0.75rem; }
  </style>
</head>
<body>
  <h1>&#127757; localOSM – Status-Dashboard</h1>
  <p class="refresh">Automatische Aktualisierung alle 10 Sekunden. <span id="ts"></span></p>

  <div class="links" id="links">
    <a href="http://NODEIP:30084/" id="web-link">&#128205; Routing-UI</a>
    <a href="http://NODEIP:30082/" id="valhalla-link">Valhalla API</a>
    <a href="http://NODEIP:30081/" id="nominatim-link">Nominatim</a>
    <a href="http://NODEIP:30080/" id="tileserver-link">TileServer GL</a>
  </div>

  <div class="grid">
    <div class="card">
      <h2>Services</h2>
      <div id="svc-list">Lade ...</div>
    </div>
    <div class="card">
      <h2>Import-Dateien</h2>
      <div id="import-list">Lade ...</div>
    </div>
    <div class="card">
      <h2>Valhalla Tiles</h2>
      <div id="tiles-info">Lade ...</div>
    </div>
  </div>

  <script>
  var h = location.hostname;
  ['web-link','valhalla-link','nominatim-link','tileserver-link'].forEach(function(id) {
    var a = document.getElementById(id);
    if (a) a.href = a.href.replace('NODEIP', h);
  });

  async function refresh() {
    try {
      var resp = await fetch('/api/status');
      var d = await resp.json();
      document.getElementById('ts').textContent = 'Stand: ' + (d.timestamp || '');

      var svcs = d.services || [];
      var okCount = svcs.filter(function(s){ return s.ok; }).length;
      var html = '';
      svcs.forEach(function(s) {
        html += '<div class="svc">' +
          '<span class="dot ' + (s.ok ? 'ok' : 'err') + '"></span>' +
          '<span class="svc-name">' + s.name + '</span>' +
          '<span class="svc-detail" title="' + s.detail + '">' + s.detail + '</span>' +
          '</div>';
      });
      if (svcs.length > 0) {
        var all = okCount === svcs.length;
        html = '<div style="margin-bottom:.5rem;font-size:.85rem">' +
          '<span class="' + (all ? 'all-ok' : 'has-err') + '">' +
          okCount + '/' + svcs.length + ' OK</span></div>' + html;
      }
      document.getElementById('svc-list').innerHTML = html || 'Keine Services.';

      var files = d.import_files || [];
      if (files.length === 0) {
        document.getElementById('import-list').innerHTML =
          '<span style="color:#888;font-size:.82rem">Noch keine Dateien in<br>' + d.import_dir + '</span>';
      } else {
        var fhtml = '<div class="file-list">';
        files.forEach(function(f) {
          fhtml += '<div class="file-row">' +
            '<span class="file-name">' + f.name + '</span>' +
            '<span class="file-meta">' + f.size_mb + ' MB<br>' + f.mtime + '</span>' +
            '</div>';
        });
        fhtml += '</div>';
        document.getElementById('import-list').innerHTML = fhtml;
      }

      var tMB = d.valhalla_tiles_mb;
      document.getElementById('tiles-info').innerHTML =
        '<div class="stat-big">' + tMB + ' MB</div>' +
        '<div class="stat-label">Valhalla Routing-Graph</div>' +
        (tMB > 0
          ? '<p style="color:#2ecc71;font-size:.82rem;margin-top:.5rem">&#10003; Tiles vorhanden – Routing bereit</p>'
          : '<p style="color:#e74c3c;font-size:.82rem;margin-top:.5rem">&#9888; Keine Tiles. Import starten mit run-import.sh</p>');
    } catch(e) {
      console.warn('Status-Abruf fehlgeschlagen:', e);
    }
    setTimeout(refresh, 10000);
  }
  refresh();
  </script>
</body>
</html>
"""


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
            body = INDEX_HTML.encode("utf-8")
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
            elif name == "web":
                ok, detail = check_http("http://web.osm.svc.cluster.local:8080/healthz")
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
