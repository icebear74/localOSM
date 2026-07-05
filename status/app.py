from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

HOST = os.environ.get("STATUS_HOST", "0.0.0.0")
PORT = int(os.environ.get("STATUS_PORT", "8080"))
DATA_DIR = os.environ.get("OSM_DATA_DIR", "/mnt/data/OSM")
NAMESPACE = os.environ.get("OSM_NAMESPACE", "osm")

STATUS_DIR = os.path.join(DATA_DIR, "status")
LIBRARY_DIR = os.path.join(DATA_DIR, "library")
IMPORT_DIR = os.path.join(DATA_DIR, "import")
VALHALLA_DIR = os.path.join(DATA_DIR, "valhalla")
VALHALLA_TILE_DIR = os.path.join(VALHALLA_DIR, "tiles")
NOMINATIM_DIR = os.path.join(DATA_DIR, "nominatim")
MERGED_PBF = os.path.join(IMPORT_DIR, "planet.osm.pbf")
COUNTRIES_FILE = os.path.join(STATUS_DIR, "countries.json")
STATE_FILE = os.path.join(STATUS_DIR, "library-state.json")

SERVICES = [
    ("postgres", "postgres.osm.svc.cluster.local", 5432, "tcp", None),
    ("tileserver", "tileserver-gl.osm.svc.cluster.local", 80, "http", "/"),
    ("nominatim", "nominatim.osm.svc.cluster.local", 8080, "http", "/"),
    ("valhalla", "valhalla.osm.svc.cluster.local", 8002, "http", "/"),
    ("web", "web.osm.svc.cluster.local", 8080, "http", "/healthz"),
]

COUNTRY_LIBRARY = [
    {
        "slug": "netherlands",
        "name": "Netherlands",
        "url": "https://download.geofabrik.de/europe/netherlands-latest.osm.pbf",
    },
    {
        "slug": "germany",
        "name": "Germany",
        "url": "https://download.geofabrik.de/europe/germany-latest.osm.pbf",
    },
    {
        "slug": "belgium",
        "name": "Belgium",
        "url": "https://download.geofabrik.de/europe/belgium-latest.osm.pbf",
    },
    {
        "slug": "luxembourg",
        "name": "Luxembourg",
        "url": "https://download.geofabrik.de/europe/luxembourg-latest.osm.pbf",
    },
    {
        "slug": "france",
        "name": "France",
        "url": "https://download.geofabrik.de/europe/france-latest.osm.pbf",
    },
    {
        "slug": "switzerland",
        "name": "Switzerland",
        "url": "https://download.geofabrik.de/europe/switzerland-latest.osm.pbf",
    },
    {
        "slug": "austria",
        "name": "Austria",
        "url": "https://download.geofabrik.de/europe/austria-latest.osm.pbf",
    },
    {
        "slug": "denmark",
        "name": "Denmark",
        "url": "https://download.geofabrik.de/europe/denmark-latest.osm.pbf",
    },
    {
        "slug": "poland",
        "name": "Poland",
        "url": "https://download.geofabrik.de/europe/poland-latest.osm.pbf",
    },
    {
        "slug": "czech-republic",
        "name": "Czech Republic",
        "url": "https://download.geofabrik.de/europe/czech-republic-latest.osm.pbf",
    },
]

WORKFLOW_LOCK = threading.Lock()
ACTIVE_WORKFLOW = {"thread": None}


INDEX_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>localOSM – Status</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: Arial, sans-serif; background: #f0f2f5; padding: 1.5rem; color: #23313f; }
    h1 { font-size: 1.3rem; margin-bottom: 1rem; color: #2c7ab5; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
    .card { background: #fff; border-radius: 8px; padding: 1rem; box-shadow: 0 1px 4px rgba(0,0,0,.1); }
    .card h2 { font-size: 0.85rem; color: #666; margin-bottom: 0.5rem; text-transform: uppercase; letter-spacing: .05em; }
    .svc { display: flex; align-items: center; gap: 0.5rem; padding: 0.4rem 0; border-bottom: 1px solid #f0f0f0; font-size: 0.85rem; }
    .svc:last-child, .file-row:last-child, .country-row:last-child { border-bottom: none; }
    .dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
    .dot.ok { background: #2ecc71; }
    .dot.err { background: #e74c3c; }
    .svc-name { font-weight: 600; min-width: 80px; }
    .svc-detail { color: #888; font-size: 0.75rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 180px; }
    .file-list, .country-list { font-size: 0.82rem; }
    .file-row, .country-row { display: flex; justify-content: space-between; gap: 0.75rem; padding: 0.45rem 0; border-bottom: 1px solid #f5f5f5; }
    .file-name, .country-name { color: #333; font-weight: 600; }
    .file-meta, .country-meta { color: #888; font-size: 0.75rem; text-align: right; }
    .refresh { font-size: 0.75rem; color: #999; margin-bottom: 1rem; }
    .links a { display: inline-block; margin-right: 1rem; margin-bottom: 0.5rem; padding: 0.4rem 0.8rem; background: #2c7ab5; color: #fff; text-decoration: none; border-radius: 4px; font-size: 0.82rem; }
    .links a:hover { background: #1a5a8a; }
    .stat-big { font-size: 1.8rem; font-weight: 700; color: #2c7ab5; }
    .stat-label { font-size: 0.75rem; color: #888; }
    .all-ok { color: #2ecc71; font-weight: 700; }
    .has-err { color: #e74c3c; font-weight: 700; }
    #ts { color: #aaa; font-size: 0.75rem; }
    .hint { font-size: 0.76rem; color: #667; line-height: 1.45; }
    .controls { display: grid; gap: 0.6rem; }
    input, select, button { width: 100%; padding: 0.55rem 0.65rem; border-radius: 4px; font-size: 0.85rem; }
    input, select { border: 1px solid #cfd7df; background: #fff; }
    button { border: none; background: #2c7ab5; color: #fff; font-weight: 700; cursor: pointer; }
    button:disabled { opacity: .6; cursor: not-allowed; }
    .subtle { background: #eef4f8; color: #31516d; }
    .progress-wrap { background: #e8edf2; border-radius: 999px; overflow: hidden; height: 12px; margin: 0.65rem 0; }
    .progress-bar { background: linear-gradient(90deg, #2c7ab5, #3aa0ff); height: 100%; width: 0%; transition: width .3s ease; }
    .status-pill { display: inline-block; border-radius: 999px; padding: 0.18rem 0.55rem; font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: .03em; }
    .status-ready { background: #e6f7ee; color: #1c7d47; }
    .status-pending { background: #fff2cc; color: #9a6700; }
    .status-error { background: #fdecea; color: #b42318; }
    .status-running { background: #e7f0fb; color: #1d5fa7; }
    .muted { color: #7c8a97; }
    .stack { display: grid; gap: 0.5rem; }
    .row2 { display: grid; gap: 0.5rem; grid-template-columns: 1fr 1fr; }
    .message { font-size: 0.82rem; line-height: 1.4; }
    pre { white-space: pre-wrap; word-break: break-word; font-size: 0.75rem; color: #5f6b76; }
  </style>
</head>
<body>
  <h1>&#127757; localOSM – Status-Dashboard</h1>
  <p class="refresh">Automatische Aktualisierung alle 5 Sekunden. <span id="ts"></span></p>

  <div class="links" id="links">
    <a href="http://NODEIP:30084/" id="web-link">&#128205; Routing-UI</a>
    <a href="http://NODEIP:30082/" id="valhalla-link">Valhalla API</a>
    <a href="http://NODEIP:30081/" id="nominatim-link">Nominatim</a>
    <a href="http://NODEIP:30085/" id="tileserver-link">TileServer GL</a>
  </div>

  <div class="grid">
    <div class="card">
      <h2>Country Library</h2>
      <div class="controls">
        <select id="country-select"></select>
        <div class="row2">
          <input id="custom-name" placeholder="Custom country name (optional)">
          <input id="custom-url" placeholder="Custom .osm.pbf URL (optional)">
        </div>
        <button id="add-country-btn" onclick="startLibraryAdd()">Country hinzufügen</button>
        <div class="hint">Wählt ein Land aus, lädt den Geofabrik-Extrakt herunter, merged alle bereits gewählten Länder erneut und baut Routing + Adress-/POI-Suche neu auf.</div>
      </div>
    </div>

    <div class="card">
      <h2>Workflow Fortschritt</h2>
      <div id="workflow-body">Lade ...</div>
    </div>

    <div class="card">
      <h2>Hinzugefügte Länder</h2>
      <div id="country-list">Lade ...</div>
    </div>

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

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function renderCountryOptions(countries) {
    var select = document.getElementById('country-select');
    var current = select.value;
    var html = '<option value="">Land wählen ...</option>';
    (countries || []).forEach(function(country) {
      var suffix = country.selected ? ' (bereits hinzugefügt)' : '';
      html += '<option value="' + esc(country.slug) + '">' + esc(country.name) + suffix + '</option>';
    });
    select.innerHTML = html;
    if (current) select.value = current;
  }

  function renderWorkflow(workflow) {
    var body = document.getElementById('workflow-body');
    var running = !!(workflow && workflow.running);
    var phase = workflow && workflow.phase ? workflow.phase : 'idle';
    var pct = workflow && typeof workflow.progress === 'number' ? workflow.progress : 0;
    var statusClass = running ? 'status-running' : (phase === 'error' ? 'status-error' : 'status-ready');
    var statusText = running ? 'läuft' : (phase === 'error' ? 'fehler' : 'bereit');
    var extra = workflow && workflow.error ? '<pre>' + esc(workflow.error) + '</pre>' : '';
    body.innerHTML = '' +
      '<span class="status-pill ' + statusClass + '">' + statusText + '</span>' +
      '<div class="progress-wrap"><div class="progress-bar" style="width:' + Math.max(0, Math.min(100, pct)) + '%"></div></div>' +
      '<div class="stack">' +
      '<div class="message"><b>' + esc(workflow && workflow.message ? workflow.message : 'Noch kein Build gestartet.') + '</b></div>' +
      '<div class="muted">Phase: ' + esc(phase) + (workflow && workflow.country ? ' · Land: ' + esc(workflow.country) : '') + '</div>' +
      '<div class="muted">' + esc(workflow && workflow.detail ? workflow.detail : 'Wählt ein Land aus, um die Bibliothek zu erweitern.') + '</div>' +
      (workflow && workflow.updated_at ? '<div class="muted">Zuletzt aktualisiert: ' + esc(workflow.updated_at) + '</div>' : '') +
      extra +
      '</div>';
    document.getElementById('add-country-btn').disabled = running;
  }

  function renderCountries(countries) {
    var el = document.getElementById('country-list');
    if (!countries || countries.length === 0) {
      el.innerHTML = '<span class="hint">Noch keine Länder in der Library.</span>';
      return;
    }
    var html = '<div class="country-list">';
    countries.forEach(function(country) {
      var badgeClass = country.status === 'ready' ? 'status-ready' : (country.status === 'error' ? 'status-error' : 'status-pending');
      var detail = [];
      if (country.pbf_size_mb != null) detail.push(country.pbf_size_mb + ' MB');
      if (country.imported_at) detail.push('fertig ' + country.imported_at);
      else if (country.added_at) detail.push('hinzugefügt ' + country.added_at);
      if (country.last_error) detail.push('Fehler vorhanden');
      html += '<div class="country-row">' +
        '<div><div class="country-name">' + esc(country.name) + '</div><div class="country-meta">' + esc(detail.join(' · ') || country.url) + '</div></div>' +
        '<div style="text-align:right"><span class="status-pill ' + badgeClass + '">' + esc(country.status || 'pending') + '</span></div>' +
        '</div>';
    });
    html += '</div>';
    el.innerHTML = html;
  }

  async function startLibraryAdd() {
    var payload = {
      country: document.getElementById('country-select').value,
      name: document.getElementById('custom-name').value.trim(),
      url: document.getElementById('custom-url').value.trim()
    };
    var workflowBody = document.getElementById('workflow-body');
    workflowBody.innerHTML = 'Starte Workflow ...';
    try {
      var resp = await fetch('/api/library/add', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      var data = await resp.json();
      if (!resp.ok) {
        workflowBody.innerHTML = '<span class="status-pill status-error">fehler</span><pre>' + esc(data.error || 'Unbekannter Fehler') + '</pre>';
        return;
      }
      document.getElementById('custom-name').value = '';
      document.getElementById('custom-url').value = '';
      await refresh();
    } catch (err) {
      workflowBody.innerHTML = '<span class="status-pill status-error">fehler</span><pre>' + esc(err) + '</pre>';
    }
  }

  async function refresh() {
    try {
      var resp = await fetch('/api/status');
      var d = await resp.json();
      document.getElementById('ts').textContent = 'Stand: ' + (d.timestamp || '');
      renderCountryOptions(d.available_countries || []);
      renderWorkflow(d.workflow || {});
      renderCountries(d.selected_countries || []);

      var svcs = d.services || [];
      var okCount = svcs.filter(function(s){ return s.ok; }).length;
      var html = '';
      svcs.forEach(function(s) {
        html += '<div class="svc">' +
          '<span class="dot ' + (s.ok ? 'ok' : 'err') + '"></span>' +
          '<span class="svc-name">' + esc(s.name) + '</span>' +
          '<span class="svc-detail" title="' + esc(s.detail) + '">' + esc(s.detail) + '</span>' +
          '</div>';
      });
      if (svcs.length > 0) {
        var all = okCount === svcs.length;
        html = '<div style="margin-bottom:.5rem;font-size:.85rem"><span class="' + (all ? 'all-ok' : 'has-err') + '">' +
          okCount + '/' + svcs.length + ' OK</span></div>' + html;
      }
      document.getElementById('svc-list').innerHTML = html || 'Keine Services.';

      var files = d.import_files || [];
      if (files.length === 0) {
        document.getElementById('import-list').innerHTML =
          '<span class="hint">Noch keine Dateien in<br>' + esc(d.import_dir) + '</span>';
      } else {
        var fhtml = '<div class="file-list">';
        files.forEach(function(f) {
          fhtml += '<div class="file-row">' +
            '<span class="file-name">' + esc(f.name) + '</span>' +
            '<span class="file-meta">' + esc(f.size_mb) + ' MB<br>' + esc(f.mtime) + '</span>' +
            '</div>';
        });
        fhtml += '</div>';
        document.getElementById('import-list').innerHTML = fhtml;
      }

      var tMB = d.valhalla_tiles_mb;
      document.getElementById('tiles-info').innerHTML =
        '<div class="stat-big">' + esc(tMB) + ' MB</div>' +
        '<div class="stat-label">Valhalla Routing-Graph</div>' +
        (tMB > 0
          ? '<p style="color:#2ecc71;font-size:.82rem;margin-top:.5rem">&#10003; Tiles vorhanden – Routing bereit</p>'
          : '<p style="color:#e74c3c;font-size:.82rem;margin-top:.5rem">&#9888; Noch keine Tiles.</p>');
    } catch(e) {
      console.warn('Status-Abruf fehlgeschlagen:', e);
    }
    setTimeout(refresh, 5000);
  }
  refresh();
  </script>
</body>
</html>
"""


def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def clone_default(value):
    return json.loads(json.dumps(value))


def ensure_dirs():
    for path in (STATUS_DIR, LIBRARY_DIR, IMPORT_DIR, VALHALLA_DIR, VALHALLA_TILE_DIR, NOMINATIM_DIR):
        os.makedirs(path, exist_ok=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return clone_default(default)


def save_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def slugify(value):
    cleaned = []
    last_dash = False
    for char in value.lower():
        if char.isalnum():
            cleaned.append(char)
            last_dash = False
        elif not last_dash:
            cleaned.append("-")
            last_dash = True
    slug = "".join(cleaned).strip("-")
    return slug or "custom-country"


def file_size_mb(path):
    try:
        return round(os.path.getsize(path) / (1024 * 1024), 1)
    except OSError:
        return None


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


def list_library_records():
    records = load_json(COUNTRIES_FILE, [])
    enriched = []
    for record in records:
        item = dict(record)
        if item.get("pbf_path"):
            size_mb = file_size_mb(item["pbf_path"])
            if size_mb is not None:
                item["pbf_size_mb"] = size_mb
        enriched.append(item)
    return enriched


def save_library_records(records):
    save_json(COUNTRIES_FILE, records)


def upsert_country_record(country, **updates):
    records = list_library_records()
    for record in records:
        if record.get("slug") == country["slug"]:
            record.update(updates)
            record["name"] = country["name"]
            record["url"] = country["url"]
            break
    else:
        record = {
            "slug": country["slug"],
            "name": country["name"],
            "url": country["url"],
            "status": "pending",
            "added_at": now_iso(),
        }
        record.update(updates)
        records.append(record)
    save_library_records(records)


def mark_library_ready():
    records = list_library_records()
    timestamp = now_iso()
    for record in records:
        record["status"] = "ready"
        record["imported_at"] = timestamp
        record["last_error"] = ""
        if record.get("pbf_path"):
            size_mb = file_size_mb(record["pbf_path"])
            if size_mb is not None:
                record["pbf_size_mb"] = size_mb
    save_library_records(records)


def read_workflow_state():
    return load_json(
        STATE_FILE,
        {
            "running": False,
            "phase": "idle",
            "progress": 0,
            "message": "Noch kein Workflow gestartet.",
            "detail": "",
            "country": "",
            "error": "",
            "updated_at": now_iso(),
        },
    )


def write_workflow_state(**updates):
    state = read_workflow_state()
    state.update(updates)
    state["updated_at"] = now_iso()
    save_json(STATE_FILE, state)


def country_catalog_with_flags(records):
    selected = {record.get("slug") for record in records}
    catalog = []
    for country in sorted(COUNTRY_LIBRARY, key=lambda item: item["name"]):
        item = dict(country)
        item["selected"] = item["slug"] in selected
        catalog.append(item)
    return catalog


def resolve_country_request(payload):
    slug = (payload.get("country") or "").strip()
    custom_name = (payload.get("name") or "").strip()
    custom_url = (payload.get("url") or "").strip()

    if custom_name or custom_url:
        if not custom_name or not custom_url:
            raise ValueError("Custom imports require both a name and a URL.")
        if not custom_url.startswith(("http://", "https://")):
            raise ValueError("Custom URL must start with http:// or https://.")
        return {"slug": slugify(custom_name), "name": custom_name, "url": custom_url}

    for country in COUNTRY_LIBRARY:
        if country["slug"] == slug:
            return dict(country)
    raise ValueError("Choose a country or provide custom country details.")


def collect_status():
    ensure_dirs()
    services = []
    for name, host, port, kind, path in SERVICES:
        if kind == "tcp":
            ok, detail = check_tcp(host, port)
        else:
            ok, detail = check_http(f"http://{host}:{port}{path or '/'}")
        services.append({"name": name, "ok": ok, "detail": detail})

    import_files = []
    if os.path.isdir(IMPORT_DIR):
        for entry in sorted(os.listdir(IMPORT_DIR)):
            full_path = os.path.join(IMPORT_DIR, entry)
            try:
                size_mb = round(os.path.getsize(full_path) / (1024 * 1024), 1)
                mtime = datetime.utcfromtimestamp(os.path.getmtime(full_path)).strftime("%Y-%m-%d %H:%M UTC")
            except OSError:
                size_mb, mtime = 0, "?"
            import_files.append({"name": entry, "size_mb": size_mb, "mtime": mtime})

    records = list_library_records()
    return {
        "timestamp": now_iso(),
        "services": services,
        "import_dir": IMPORT_DIR,
        "import_files": import_files,
        "valhalla_tiles_mb": dir_size_mb(VALHALLA_TILE_DIR),
        "available_countries": country_catalog_with_flags(records),
        "selected_countries": records,
        "workflow": read_workflow_state(),
    }


def run_command(args, *, input_text=None, check=True):
    result = subprocess.run(
        args,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(output or f"Command failed: {' '.join(args)}")
    return result.stdout.strip()


def clear_directory(path):
    os.makedirs(path, exist_ok=True)
    for entry in os.listdir(path):
        full_path = os.path.join(path, entry)
        if os.path.isdir(full_path) and not os.path.islink(full_path):
            shutil.rmtree(full_path)
        else:
            os.unlink(full_path)


def download_country_file(country):
    destination = os.path.join(LIBRARY_DIR, f"{country['slug']}.osm.pbf")
    temp_path = f"{destination}.part"
    if os.path.exists(destination) and os.path.getsize(destination) > 0:
        write_workflow_state(
            running=True,
            phase="download",
            progress=35,
            message=f"Using cached extract for {country['name']}.",
            detail=destination,
            country=country["name"],
            error="",
        )
        upsert_country_record(country, pbf_path=destination, pbf_size_mb=file_size_mb(destination))
        return destination

    request = urllib.request.Request(country["url"], headers={"User-Agent": "localosm-status/1.0"})
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    downloaded = 0
    total_size = 0
    with urllib.request.urlopen(request, timeout=30) as response, open(temp_path, "wb") as handle:
        total_size = int(response.headers.get("Content-Length", "0") or 0)
        last_update = 0.0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            downloaded += len(chunk)
            now = time.monotonic()
            if now - last_update >= 0.5:
                if total_size > 0:
                    percent = min(45, 5 + int((downloaded / total_size) * 40))
                    detail = f"{round(downloaded / (1024 * 1024), 1)} / {round(total_size / (1024 * 1024), 1)} MB"
                else:
                    percent = 25
                    detail = f"{round(downloaded / (1024 * 1024), 1)} MB downloaded"
                write_workflow_state(
                    running=True,
                    phase="download",
                    progress=percent,
                    message=f"Downloading {country['name']} extract ...",
                    detail=detail,
                    country=country["name"],
                    error="",
                )
                last_update = now
    os.replace(temp_path, destination)
    upsert_country_record(
        country,
        pbf_path=destination,
        pbf_size_mb=file_size_mb(destination),
        downloaded_at=now_iso(),
        status="pending",
        last_error="",
    )
    return destination


def merge_library_files(country):
    records = list_library_records()
    input_paths = [record.get("pbf_path") for record in records if record.get("pbf_path")]
    if not input_paths:
        raise RuntimeError("No downloaded country extracts available to merge.")

    tmp_path = f"{MERGED_PBF}.tmp"
    os.makedirs(IMPORT_DIR, exist_ok=True)
    write_workflow_state(
        running=True,
        phase="merge",
        progress=55,
        message="Merging selected country extracts ...",
        detail=f"{len(input_paths)} file(s) into {MERGED_PBF}",
        country=country["name"],
        error="",
    )
    if len(input_paths) == 1:
        shutil.copyfile(input_paths[0], tmp_path)
    else:
        run_command(["osmium", "merge", "--overwrite", "-o", tmp_path, *input_paths])
    os.replace(tmp_path, MERGED_PBF)

    meta_path = f"{MERGED_PBF}.meta"
    with open(meta_path, "w", encoding="utf-8") as handle:
        handle.write(f"downloaded_at={now_iso()}\n")
        handle.write(f"path={MERGED_PBF}\n")
        handle.write(f"countries={','.join(record['slug'] for record in records)}\n")
        for record in records:
            handle.write(f"source[{record['slug']}]={record['url']}\n")


def build_valhalla_job_manifest():
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "valhalla-import", "namespace": NAMESPACE},
        "spec": {
            "backoffLimit": 1,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "valhalla-import",
                            "image": "ghcr.io/gis-ops/valhalla:latest",
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/bin/sh", "-c"],
                            "args": [
                                "set -e\n"
                                "echo '=== Valhalla Import Job ==='\n"
                                "echo 'Looking for OSM PBF data at /data/import/planet.osm.pbf ...'\n"
                                "if [ ! -f /data/import/planet.osm.pbf ]; then\n"
                                "  echo 'ERROR: No OSM PBF file found at /data/import/planet.osm.pbf'\n"
                                "  exit 1\n"
                                "fi\n"
                                "PBF_SIZE=$(du -sh /data/import/planet.osm.pbf | cut -f1)\n"
                                "echo \"Found PBF file (${PBF_SIZE}). Building routing graph ...\"\n"
                                "mkdir -p /data/tiles\n"
                                "cp /config/valhalla.json /tmp/valhalla.json\n"
                                "valhalla_build_tiles -c /tmp/valhalla.json /data/import/planet.osm.pbf\n"
                                "echo '=== Import complete. Routing graph stored at /data/tiles ==='\n"
                            ],
                            "volumeMounts": [
                                {"name": "import-data", "mountPath": "/data/import"},
                                {"name": "valhalla-data", "mountPath": "/data"},
                                {"name": "valhalla-config", "mountPath": "/config"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "import-data",
                            "hostPath": {"path": "/mnt/data/OSM/import", "type": "DirectoryOrCreate"},
                        },
                        {
                            "name": "valhalla-data",
                            "hostPath": {"path": "/mnt/data/OSM/valhalla", "type": "DirectoryOrCreate"},
                        },
                        {
                            "name": "valhalla-config",
                            "configMap": {"name": "osm-valhalla-config"},
                        },
                    ],
                }
            },
        },
    }


def rebuild_valhalla(country):
    clear_directory(VALHALLA_TILE_DIR)
    run_command(["kubectl", "-n", NAMESPACE, "delete", "job", "valhalla-import", "--ignore-not-found=true"], check=False)
    write_workflow_state(
        running=True,
        phase="routing",
        progress=60,
        message="Starting Valhalla rebuild ...",
        detail="Submitting valhalla-import job.",
        country=country["name"],
        error="",
    )
    manifest = json.dumps(build_valhalla_job_manifest())
    run_command(["kubectl", "apply", "-f", "-"], input_text=manifest)

    deadline = time.time() + 7200
    while time.time() < deadline:
        job_text = run_command(["kubectl", "-n", NAMESPACE, "get", "job", "valhalla-import", "-o", "json"], check=False)
        if not job_text:
            time.sleep(3)
            continue
        job_data = json.loads(job_text)
        status = job_data.get("status", {})
        if status.get("succeeded", 0) >= 1:
            write_workflow_state(
                running=True,
                phase="routing",
                progress=78,
                message="Valhalla routing graph rebuilt.",
                detail="Routing tiles are ready.",
                country=country["name"],
                error="",
            )
            return
        if status.get("failed", 0) > 0:
            logs = run_command(["kubectl", "-n", NAMESPACE, "logs", "job/valhalla-import", "--tail=80"], check=False)
            raise RuntimeError(logs or "Valhalla import job failed.")
        active = status.get("active", 0)
        write_workflow_state(
            running=True,
            phase="routing",
            progress=68,
            message="Valhalla is rebuilding routing tiles ...",
            detail=f"Job active: {active}. Polling kubectl for completion.",
            country=country["name"],
            error="",
        )
        time.sleep(5)
    raise RuntimeError("Timed out waiting for the Valhalla import job to finish.")


def scale_nominatim(replicas):
    run_command(["kubectl", "-n", NAMESPACE, "scale", "deployment", "nominatim", f"--replicas={replicas}"])


def wait_for_nominatim_pods_to_stop(timeout_seconds=300):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        pod_data = run_command(
            ["kubectl", "-n", NAMESPACE, "get", "pods", "-l", "app=nominatim", "-o", "json"],
            check=False,
        )
        items = json.loads(pod_data or "{}") .get("items", [])
        if not items:
            return
        time.sleep(3)
    raise RuntimeError("Timed out waiting for the Nominatim pod to stop.")


def wait_for_nominatim_ready(country):
    deadline = time.time() + 7200
    while time.time() < deadline:
        deployment_text = run_command(
            ["kubectl", "-n", NAMESPACE, "get", "deployment", "nominatim", "-o", "json"],
            check=False,
        )
        deployment = json.loads(deployment_text or "{}")
        status = deployment.get("status", {})
        ready = status.get("readyReplicas", 0)
        ok, detail = check_http("http://nominatim.osm.svc.cluster.local:8080/")
        if ready >= 1 and ok:
            write_workflow_state(
                running=True,
                phase="search",
                progress=95,
                message="Nominatim address + POI search is ready.",
                detail=detail,
                country=country["name"],
                error="",
            )
            return
        write_workflow_state(
            running=True,
            phase="search",
            progress=88,
            message="Nominatim is importing the merged dataset ...",
            detail=f"Ready replicas: {ready}. Last probe: {detail}",
            country=country["name"],
            error="",
        )
        time.sleep(10)
    raise RuntimeError("Timed out waiting for Nominatim to become ready.")


def rebuild_nominatim(country):
    write_workflow_state(
        running=True,
        phase="search",
        progress=82,
        message="Refreshing Nominatim for address and POI search ...",
        detail="Scaling the Nominatim deployment down before reimport.",
        country=country["name"],
        error="",
    )
    scale_nominatim(0)
    wait_for_nominatim_pods_to_stop()
    clear_directory(NOMINATIM_DIR)
    scale_nominatim(1)
    wait_for_nominatim_ready(country)


def run_country_workflow(country):
    try:
        ensure_dirs()
        upsert_country_record(country, status="pending", added_at=now_iso(), last_error="")
        write_workflow_state(
            running=True,
            phase="queued",
            progress=2,
            message=f"Starting library update for {country['name']}.",
            detail="Preparing directories and state files.",
            country=country["name"],
            error="",
        )
        download_country_file(country)
        merge_library_files(country)
        rebuild_valhalla(country)
        rebuild_nominatim(country)
        mark_library_ready()
        write_workflow_state(
            running=False,
            phase="done",
            progress=100,
            message=f"{country['name']} added to the library.",
            detail="Merged extract, routing tiles, address search and POI search are ready.",
            country=country["name"],
            error="",
        )
    except Exception as exc:  # noqa: BLE001
        upsert_country_record(country, status="error", last_error=str(exc))
        write_workflow_state(
            running=False,
            phase="error",
            progress=100,
            message=f"Library update failed for {country['name']}.",
            detail="See the captured error below.",
            country=country["name"],
            error=str(exc),
        )
    finally:
        ACTIVE_WORKFLOW["thread"] = None
        WORKFLOW_LOCK.release()


def start_country_workflow(payload):
    country = resolve_country_request(payload)
    if not WORKFLOW_LOCK.acquire(blocking=False):
        raise RuntimeError("Another country library workflow is already running.")
    thread = threading.Thread(target=run_country_workflow, args=(country,), daemon=True)
    ACTIVE_WORKFLOW["thread"] = thread
    thread.start()
    return country


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

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            self._send_json({"error": "invalid json"}, 400)
            return

        if self.path == "/api/library/add":
            try:
                country = start_country_workflow(payload)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, 409)
                return
            self._send_json({"status": "started", "country": country})
            return

        self._send_json({"error": "not found"}, 404)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    ensure_dirs()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"OSM status server listening on {HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
