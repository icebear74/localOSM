from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import concurrent.futures
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HOST = os.environ.get("STATUS_HOST", "0.0.0.0")
PORT = int(os.environ.get("STATUS_PORT", "8080"))
DATA_DIR = os.environ.get("OSM_DATA_DIR", "/mnt/data/OSM")
NAMESPACE = os.environ.get("OSM_NAMESPACE", "osm")
OSM_NODE_URL = os.environ.get("OSM_NODE_URL", "").strip()

STATUS_DIR = os.path.join(DATA_DIR, "status")
LIBRARY_DIR = os.path.join(DATA_DIR, "library")
IMPORT_DIR = os.path.join(DATA_DIR, "import")
VALHALLA_DIR = os.path.join(DATA_DIR, "valhalla")
VALHALLA_STAGING_DIR = os.path.join(DATA_DIR, "valhalla-staging")
VALHALLA_TILE_DIR = os.path.join(VALHALLA_DIR, "tiles")
NOMINATIM_DIR = os.path.join(DATA_DIR, "nominatim")
TILESERVER_DIR = os.path.join(DATA_DIR, "tileserver")
MERGED_PBF = os.path.join(IMPORT_DIR, "planet.osm.pbf")
COUNTRIES_FILE = os.path.join(STATUS_DIR, "countries.json")
STATE_FILE = os.path.join(STATUS_DIR, "library-state.json")
CONFIG_FILE = os.path.join(STATUS_DIR, "config.json")

CONFIG_DEFAULTS = {
    "node_url": "",
    "auto_update_enabled": False,
    "auto_update_time": "03:00",
}

SERVICES = [
    ("postgres", "postgres.osm.svc.cluster.local", 5432, "tcp", None),
    ("tileserver", "tileserver-gl.osm.svc.cluster.local", 80, "http", "/"),
    ("nominatim", "nominatim.osm.svc.cluster.local", 8080, "http", "/"),
    ("valhalla", "valhalla.osm.svc.cluster.local", 8002, "http", "/"),
    ("web", "web.osm.svc.cluster.local", 8080, "http", "/healthz"),
]

COUNTRY_LIBRARY_GROUPS = [
    (
        "Africa",
        "africa",
        [
            ("algeria", "Algeria"),
            ("angola", "Angola"),
            ("benin", "Benin"),
            ("botswana", "Botswana"),
            ("burkina-faso", "Burkina Faso"),
            ("burundi", "Burundi"),
            ("cameroon", "Cameroon"),
            ("canary-islands", "Canary Islands"),
            ("cape-verde", "Cape Verde"),
            ("central-african-republic", "Central African Republic"),
            ("chad", "Chad"),
            ("comores", "Comoros"),
            ("congo-brazzaville", "Congo (Brazzaville)"),
            ("congo-dem-rep", "Congo (Democratic Republic)"),
            ("djibouti", "Djibouti"),
            ("egypt", "Egypt"),
            ("equatorial-guinea", "Equatorial Guinea"),
            ("eritrea", "Eritrea"),
            ("ethiopia", "Ethiopia"),
            ("gabon", "Gabon"),
            ("ghana", "Ghana"),
            ("guinea", "Guinea"),
            ("guinea-bissau", "Guinea-Bissau"),
            ("ivory-coast", "Ivory Coast"),
            ("kenya", "Kenya"),
            ("lesotho", "Lesotho"),
            ("liberia", "Liberia"),
            ("libya", "Libya"),
            ("madagascar", "Madagascar"),
            ("malawi", "Malawi"),
            ("mali", "Mali"),
            ("mauritania", "Mauritania"),
            ("mauritius", "Mauritius"),
            ("morocco", "Morocco"),
            ("mozambique", "Mozambique"),
            ("namibia", "Namibia"),
            ("niger", "Niger"),
            ("nigeria", "Nigeria"),
            ("rwanda", "Rwanda"),
            ("senegal-and-gambia", "Senegal and Gambia"),
            ("sierra-leone", "Sierra Leone"),
            ("somalia", "Somalia"),
            ("south-africa", "South Africa"),
            ("south-sudan", "South Sudan"),
            ("sudan", "Sudan"),
            ("swaziland", "Eswatini"),
            ("tanzania", "Tanzania"),
            ("togo", "Togo"),
            ("tunisia", "Tunisia"),
            ("uganda", "Uganda"),
            ("zambia", "Zambia"),
            ("zimbabwe", "Zimbabwe"),
        ],
    ),
    (
        "Asia",
        "asia",
        [
            ("afghanistan", "Afghanistan"),
            ("armenia", "Armenia"),
            ("azerbaijan", "Azerbaijan"),
            ("bangladesh", "Bangladesh"),
            ("bhutan", "Bhutan"),
            ("cambodia", "Cambodia"),
            ("china", "China"),
            ("gcc-states", "GCC States"),
            ("india", "India"),
            ("indonesia", "Indonesia"),
            ("iran", "Iran"),
            ("iraq", "Iraq"),
            ("israel-and-palestine", "Israel and Palestine"),
            ("japan", "Japan"),
            ("jordan", "Jordan"),
            ("kazakhstan", "Kazakhstan"),
            ("kyrgyzstan", "Kyrgyzstan"),
            ("laos", "Laos"),
            ("lebanon", "Lebanon"),
            ("malaysia-singapore-brunei", "Malaysia / Singapore / Brunei"),
            ("maldives", "Maldives"),
            ("mongolia", "Mongolia"),
            ("myanmar", "Myanmar"),
            ("nepal", "Nepal"),
            ("north-korea", "North Korea"),
            ("pakistan", "Pakistan"),
            ("philippines", "Philippines"),
            ("saudi-arabia", "Saudi Arabia"),
            ("south-korea", "South Korea"),
            ("sri-lanka", "Sri Lanka"),
            ("syria", "Syria"),
            ("taiwan", "Taiwan"),
            ("tajikistan", "Tajikistan"),
            ("thailand", "Thailand"),
            ("timor-leste", "Timor-Leste"),
            ("turkey", "Turkey"),
            ("turkmenistan", "Turkmenistan"),
            ("united-arab-emirates", "United Arab Emirates"),
            ("uzbekistan", "Uzbekistan"),
            ("vietnam", "Vietnam"),
            ("yemen", "Yemen"),
        ],
    ),
    (
        "Australia & Oceania",
        "australia-oceania",
        [
            ("australia", "Australia"),
            ("fiji", "Fiji"),
            ("new-caledonia", "New Caledonia"),
            ("new-zealand", "New Zealand"),
            ("papua-new-guinea", "Papua New Guinea"),
        ],
    ),
    (
        "Central America",
        "central-america",
        [
            ("belize", "Belize"),
            ("costa-rica", "Costa Rica"),
            ("el-salvador", "El Salvador"),
            ("guatemala", "Guatemala"),
            ("haiti-and-domrep", "Haiti and Dominican Republic"),
            ("honduras", "Honduras"),
            ("nicaragua", "Nicaragua"),
            ("panama", "Panama"),
        ],
    ),
    (
        "Europe",
        "europe",
        [
            ("albania", "Albania"),
            ("andorra", "Andorra"),
            ("austria", "Austria"),
            ("azores", "Azores"),
            ("belarus", "Belarus"),
            ("belgium", "Belgium"),
            ("bosnia-herzegovina", "Bosnia and Herzegovina"),
            ("bulgaria", "Bulgaria"),
            ("croatia", "Croatia"),
            ("cyprus", "Cyprus"),
            ("czech-republic", "Czech Republic"),
            ("denmark", "Denmark"),
            ("estonia", "Estonia"),
            ("faroe-islands", "Faroe Islands"),
            ("finland", "Finland"),
            ("france", "France"),
            ("georgia", "Georgia"),
            ("germany", "Germany"),
            ("great-britain", "Great Britain"),
            ("greece", "Greece"),
            ("hungary", "Hungary"),
            ("iceland", "Iceland"),
            ("ireland-and-northern-ireland", "Ireland and Northern Ireland"),
            ("isle-of-man", "Isle of Man"),
            ("italy", "Italy"),
            ("kosovo", "Kosovo"),
            ("latvia", "Latvia"),
            ("liechtenstein", "Liechtenstein"),
            ("lithuania", "Lithuania"),
            ("luxembourg", "Luxembourg"),
            ("macedonia", "North Macedonia"),
            ("malta", "Malta"),
            ("moldova", "Moldova"),
            ("monaco", "Monaco"),
            ("montenegro", "Montenegro"),
            ("netherlands", "Netherlands"),
            ("norway", "Norway"),
            ("poland", "Poland"),
            ("portugal", "Portugal"),
            ("romania", "Romania"),
            ("russia", "Russia"),
            ("serbia", "Serbia"),
            ("slovakia", "Slovakia"),
            ("slovenia", "Slovenia"),
            ("spain", "Spain"),
            ("sweden", "Sweden"),
            ("switzerland", "Switzerland"),
            ("ukraine", "Ukraine"),
        ],
    ),
    (
        "North America",
        "north-america",
        [
            ("canada", "Canada"),
            ("greenland", "Greenland"),
            ("mexico", "Mexico"),
            ("us-northeast", "US Northeast"),
            ("us-midwest", "US Midwest"),
            ("us-south", "US South"),
            ("us-west", "US West"),
        ],
    ),
    (
        "South America",
        "south-america",
        [
            ("argentina", "Argentina"),
            ("bolivia", "Bolivia"),
            ("brazil", "Brazil"),
            ("chile", "Chile"),
            ("colombia", "Colombia"),
            ("ecuador", "Ecuador"),
            ("guyana", "Guyana"),
            ("paraguay", "Paraguay"),
            ("peru", "Peru"),
            ("suriname", "Suriname"),
            ("uruguay", "Uruguay"),
            ("venezuela", "Venezuela"),
        ],
    ),
]

COUNTRY_LIBRARY = [
    {
        "slug": slug,
        "name": name,
        "continent": continent,
        "url": f"https://download.geofabrik.de/{continent_path}/{slug}-latest.osm.pbf",
    }
    for continent, continent_path, countries in COUNTRY_LIBRARY_GROUPS
    for slug, name in countries
]

WORKFLOW_LOCK = threading.Lock()
ACTIVE_WORKFLOW = {"thread": None}
SCHEDULER_LOCK = threading.Lock()
LAST_AUTO_RUN = {"date": ""}


_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class KubeClient:
    """Minimal Kubernetes REST API client using in-cluster credentials."""

    def __init__(self, namespace, timeout=30):
        self.namespace = namespace
        self.timeout = timeout
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self._base = f"https://{host}:{port}"
        token_path = os.path.join(_SA_DIR, "token")
        ca_path = os.path.join(_SA_DIR, "ca.crt")
        try:
            with open(token_path) as fh:
                self._token = fh.read().strip()
        except OSError:
            self._token = None
        self._ssl = ssl.create_default_context(cafile=ca_path if os.path.exists(ca_path) else None)
        if not os.path.exists(ca_path):
            self._ssl.check_hostname = False
            self._ssl.verify_mode = ssl.CERT_NONE

    def _request(self, method, path, body=None, params=None, content_type="application/json"):
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data:
            headers["Content-Type"] = content_type
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self._ssl, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode(errors="replace")
            raise RuntimeError(f"K8s API {method} {path} → {exc.code}: {body_text}") from exc

    def get_job(self, name):
        return self._request("GET", f"/apis/batch/v1/namespaces/{self.namespace}/jobs/{name}")

    def create_job(self, manifest):
        return self._request("POST", f"/apis/batch/v1/namespaces/{self.namespace}/jobs", body=manifest)

    def delete_job(self, name):
        try:
            self._request(
                "DELETE",
                f"/apis/batch/v1/namespaces/{self.namespace}/jobs/{name}",
                body={"propagationPolicy": "Background"},
            )
        except RuntimeError as exc:
            if "404" not in str(exc):
                raise

    def get_job_logs(self, name, tail_lines=80):
        pods = self._request(
            "GET",
            f"/api/v1/namespaces/{self.namespace}/pods",
            params={"labelSelector": f"job-name={name}"},
        )
        items = pods.get("items", [])
        if not items:
            return ""
        pod_name = items[-1]["metadata"]["name"]
        url = self._base + f"/api/v1/namespaces/{self.namespace}/pods/{pod_name}/log?tailLines={tail_lines}"
        headers = {}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, context=self._ssl, timeout=self.timeout) as resp:
                return resp.read().decode(errors="replace")
        except urllib.error.HTTPError:
            return ""

    def get_deployment(self, name):
        return self._request("GET", f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}")

    def scale_deployment(self, name, replicas):
        self._request(
            "PATCH",
            f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}",
            body={"spec": {"replicas": replicas}},
            content_type="application/merge-patch+json",
        )

    def rollout_restart(self, name):
        ts = datetime.now(timezone.utc).isoformat()
        self._request(
            "PATCH",
            f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}",
            body={
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {"kubectl.kubernetes.io/restartedAt": ts}
                        }
                    }
                }
            },
            content_type="application/merge-patch+json",
        )

    def list_pods(self, label_selector):
        return self._request(
            "GET",
            f"/api/v1/namespaces/{self.namespace}/pods",
            params={"labelSelector": label_selector},
        )

    def get_node_ips(self):
        """Return a list of InternalIP addresses for all cluster nodes."""
        try:
            nodes = self._request("GET", "/api/v1/nodes")
            ips = []
            for node in nodes.get("items", []):
                for addr in node.get("status", {}).get("addresses", []):
                    if addr.get("type") == "InternalIP":
                        ips.append(addr["address"])
            return ips
        except Exception:  # noqa: BLE001
            return []


KUBE = KubeClient(NAMESPACE)

_detected_node_url_cache: "str | None" = None
_detected_node_url_cache_time: float = 0.0
_NODE_URL_EMPTY_CACHE_TTL = 30.0  # seconds to suppress retries after a failed lookup


def detect_node_url():
    """Return a node URL for service links, or empty string if unavailable.

    Priority: OSM_NODE_URL env var → first InternalIP from the K8s node list.
    Successful results are cached permanently; failed lookups are suppressed for
    _NODE_URL_EMPTY_CACHE_TTL seconds to avoid a K8s API round-trip on every page load.
    """
    global _detected_node_url_cache, _detected_node_url_cache_time
    if OSM_NODE_URL:
        return OSM_NODE_URL
    if _detected_node_url_cache is not None:
        return _detected_node_url_cache
    if time.monotonic() - _detected_node_url_cache_time < _NODE_URL_EMPTY_CACHE_TTL:
        return ""
    _detected_node_url_cache_time = time.monotonic()
    ips = KUBE.get_node_ips()
    result = f"http://{ips[0]}" if ips else ""
    if result:
        _detected_node_url_cache = result
    return result


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
    .danger { background: #e74c3c; color: #fff; }
    .danger:hover { background: #c0392b; }
    .progress-wrap { background: #e8edf2; border-radius: 999px; overflow: hidden; height: 12px; margin: 0.65rem 0; }
    .progress-bar { background: linear-gradient(90deg, #2c7ab5, #3aa0ff); height: 100%; width: 0%; transition: width .3s ease; }
    .status-pill { display: inline-block; border-radius: 999px; padding: 0.18rem 0.55rem; font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: .03em; }
    .status-ready { background: #e6f7ee; color: #1c7d47; }
    .status-pending { background: #fff2cc; color: #9a6700; }
    .status-error { background: #fdecea; color: #b42318; }
    .status-running { background: #e7f0fb; color: #1d5fa7; }
    .status-queued { background: #f3ecff; color: #6b46c1; }
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
    <a href="#" id="web-link" data-port="30084">&#128205; Routing-UI</a>
    <a href="#" id="valhalla-link" data-port="30082">Valhalla API</a>
    <a href="#" id="nominatim-link" data-port="30081">Nominatim</a>
    <a href="#" id="tileserver-link" data-port="30085">TileServer GL</a>
  </div>

  <div class="grid">
    <div class="card">
      <h2>Node-Konfiguration</h2>
      <div class="controls">
        <div>
          <label>Node URL</label>
          <input id="node-url-input" placeholder="http://192.168.1.100" value="{{NODE_URL}}">
        </div>
        <button onclick="saveConfig()">Speichern</button>
        <div id="node-url-hint" class="hint">Optional: explizite Node-URL für alle Service-Links.</div>
      </div>
    </div>

    <div class="card">
      <h2>Auto-Update</h2>
      <div class="controls">
        <label style="display:flex;align-items:center;gap:.5rem;cursor:pointer">
          <input type="checkbox" id="auto-update-enabled" style="width:auto">
          Auto-Update aktivieren
        </label>
        <div>
          <label>Uhrzeit (UTC)</label>
          <input type="time" id="auto-update-time" value="03:00">
        </div>
        <button onclick="saveAutoUpdate()">Speichern</button>
        <div id="auto-update-status" class="hint"></div>
      </div>
    </div>

    <div class="card">
      <h2>Country Library</h2>
      <div class="controls">
        <select id="continent-select" onchange="filterCountriesByContinent()"></select>
        <select id="country-select"></select>
        <div class="row2">
          <input id="custom-name" placeholder="Custom country name (optional)">
          <input id="custom-url" placeholder="Custom .osm.pbf URL (optional)">
        </div>
        <div class="row2">
          <button id="add-country-btn" onclick="startLibraryQueue()">Land zur Queue hinzufügen</button>
          <button id="build-btn" class="subtle" onclick="startBuild()">Build starten</button>
        </div>
        <div id="queue-count" class="hint">Keine Länder in der Queue.</div>
        <div class="hint">Wählt ein Land aus, lädt den Geofabrik-Extrakt herunter und stellt es in die Queue. Der Build merged alle gewählten Länder und baut Routing, Tiles sowie Adress-/POI-Suche neu auf.</div>
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
  var NODE_URL = "{{NODE_URL}}";
  var ALL_COUNTRIES = [];
  var refreshTimer = null;
  var WORKFLOW_RUNNING = false;

  function updateLinks() {
    var baseUrl = (NODE_URL || (location.protocol + '//' + location.hostname)).replace(/\\/+$/, '');
    ['web-link','valhalla-link','nominatim-link','tileserver-link'].forEach(function(id) {
      var a = document.getElementById(id);
      if (a && a.dataset.port) a.href = baseUrl + ':' + a.dataset.port + '/';
    });
  }
  updateLinks();

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function renderContinentOptions(countries) {
    var select = document.getElementById('continent-select');
    var current = select.value;
    var seen = {};
    var values = [];
    (countries || []).forEach(function(country) {
      var continent = country.continent || '';
      if (continent && !seen[continent]) {
        seen[continent] = true;
        values.push(continent);
      }
    });
    values.sort();
    var html = '<option value="">Alle Kontinente</option>';
    values.forEach(function(continent) {
      html += '<option value="' + esc(continent) + '">' + esc(continent) + '</option>';
    });
    select.innerHTML = html;
    if (current && seen[current]) select.value = current;
  }

  function renderCountryOptions(countries) {
    var select = document.getElementById('country-select');
    var current = select.value;
    var continent = document.getElementById('continent-select').value;
    var html = '<option value="">Land wählen ...</option>';
    (countries || []).filter(function(country) {
      return !continent || country.continent === continent;
    }).forEach(function(country) {
      var suffix = country.selected ? ' (bereits hinzugefügt)' : '';
      html += '<option value="' + esc(country.slug) + '">' + esc(country.name) + suffix + '</option>';
    });
    select.innerHTML = html;
    if (current) select.value = current;
  }

  function filterCountriesByContinent() {
    renderCountryOptions(ALL_COUNTRIES);
  }

  function renderWorkflow(workflow) {
    var body = document.getElementById('workflow-body');
    var running = !!(workflow && workflow.running);
    WORKFLOW_RUNNING = running;
    var phase = workflow && workflow.phase ? workflow.phase : 'idle';
    var pct = workflow && typeof workflow.progress === 'number' ? workflow.progress : 0;
    var statusClass = 'status-ready';
    var statusText = 'bereit';
    if (running) {
      statusClass = 'status-running';
      statusText = phase === 'building' ? 'build' : 'läuft';
    } else if (phase === 'queued') {
      statusClass = 'status-queued';
      statusText = 'queue';
    } else if (phase === 'error') {
      statusClass = 'status-error';
      statusText = 'fehler';
    }
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
    document.getElementById('build-btn').disabled = running;
    document.querySelectorAll('.remove-country-btn').forEach(function(btn) {
      btn.disabled = running;
    });
  }

  function renderCountries(countries) {
    var el = document.getElementById('country-list');
    if (!countries || countries.length === 0) {
      el.innerHTML = '<span class="hint">Noch keine Länder in der Library.</span>';
      return;
    }
    var html = '<div class="country-list">';
    countries.forEach(function(country) {
      var status = country.status || 'pending';
      var badgeClass = status === 'ready'
        ? 'status-ready'
        : (status === 'error' ? 'status-error' : (status === 'queued' ? 'status-queued' : 'status-pending'));
      var detail = [];
      if (country.continent) detail.push(country.continent);
      if (country.pbf_size_mb != null) detail.push(country.pbf_size_mb + ' MB');
      if (country.imported_at) detail.push('fertig ' + country.imported_at);
      else if (country.added_at) detail.push('hinzugefügt ' + country.added_at);
      if (country.last_error) detail.push('Fehler vorhanden');
      var slugAttr = esc(country.slug);
      html += '<div class="country-row">' +
        '<div><div class="country-name">' + esc(country.name) + '</div><div class="country-meta">' + esc(detail.join(' · ') || country.url) + '</div></div>' +
        '<div style="text-align:right;display:flex;align-items:center;gap:0.4rem;justify-content:flex-end">' +
        '<span class="status-pill ' + badgeClass + '">' + esc(status) + '</span>' +
        '<button class="remove-country-btn danger" data-slug="' + slugAttr + '" onclick="removeCountry(this.dataset.slug)" style="width:auto;padding:0.18rem 0.5rem;font-size:0.72rem;font-weight:700" ' + (WORKFLOW_RUNNING ? 'disabled' : '') + ' title="Land entfernen">×</button>' +
        '</div>' +
        '</div>';
    });
    html += '</div>';
    el.innerHTML = html;
  }

  function renderQueuedCount(countries) {
    var count = (countries || []).filter(function(country) { return country.status === 'queued'; }).length;
    var el = document.getElementById('queue-count');
    if (!el) return;
    el.textContent = count > 0
      ? count + ' Land/Länder warten aktuell auf den Build.'
      : 'Keine Länder in der Queue.';
  }

  function buildCountryPayload() {
    return {
      country: document.getElementById('country-select').value,
      name: document.getElementById('custom-name').value.trim(),
      url: document.getElementById('custom-url').value.trim()
    };
  }

  function clearCountryInputs() {
    document.getElementById('custom-name').value = '';
    document.getElementById('custom-url').value = '';
  }

  async function startLibraryQueue() {
    var workflowBody = document.getElementById('workflow-body');
    workflowBody.innerHTML = 'Starte Queue-Workflow ...';
    try {
      var resp = await fetch('/api/library/queue', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(buildCountryPayload())
      });
      var data = await resp.json();
      if (!resp.ok) {
        workflowBody.innerHTML = '<span class="status-pill status-error">fehler</span><pre>' + esc(data.error || 'Unbekannter Fehler') + '</pre>';
        return;
      }
      clearCountryInputs();
      await refresh();
    } catch (err) {
      workflowBody.innerHTML = '<span class="status-pill status-error">fehler</span><pre>' + esc(err) + '</pre>';
    }
  }

  async function startLibraryAdd() {
    return startLibraryQueue();
  }

  async function startBuild() {
    var workflowBody = document.getElementById('workflow-body');
    workflowBody.innerHTML = 'Starte Build-Workflow ...';
    try {
      var resp = await fetch('/api/library/build', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{}'
      });
      var data = await resp.json();
      if (!resp.ok) {
        workflowBody.innerHTML = '<span class="status-pill status-error">fehler</span><pre>' + esc(data.error || 'Unbekannter Fehler') + '</pre>';
        return;
      }
      await refresh();
    } catch (err) {
      workflowBody.innerHTML = '<span class="status-pill status-error">fehler</span><pre>' + esc(err) + '</pre>';
    }
  }

  async function removeCountry(slug) {
    if (!confirm('Land wirklich aus der Library entfernen?\\nAlle verbleibenden Länder werden anschließend für einen Rebuild markiert.')) return;
    try {
      var resp = await fetch('/api/library/remove', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({slug: slug})
      });
      var data = await resp.json();
      if (!resp.ok) {
        alert('Fehler: ' + (data.error || 'Unbekannter Fehler'));
        return;
      }
      await refresh();
    } catch (err) {
      alert('Fehler: ' + err);
    }
  }

  async function loadConfig() {
    try {
      var resp = await fetch('/api/config');
      var cfg = await resp.json();
      var hintEl = document.getElementById('node-url-hint');
      if (cfg.node_url) {
        document.getElementById('node-url-input').value = cfg.node_url;
        NODE_URL = cfg.node_url;
        if (hintEl) hintEl.textContent = 'Explizite Node-URL für alle Service-Links.';
      } else if (cfg.detected_node_url) {
        document.getElementById('node-url-input').value = cfg.detected_node_url;
        NODE_URL = cfg.detected_node_url;
        if (hintEl) hintEl.textContent = 'Automatisch erkannte Node-URL. Zum Überschreiben speichern.';
      } else {
        document.getElementById('node-url-input').value = '';
        NODE_URL = '';
        if (hintEl) hintEl.textContent = 'Node-URL konnte nicht ermittelt werden. Bitte manuell eintragen.';
      }
      document.getElementById('auto-update-enabled').checked = !!cfg.auto_update_enabled;
      document.getElementById('auto-update-time').value = cfg.auto_update_time || '03:00';
      updateLinks();
      updateNextRunDisplay(cfg);
    } catch(e) {}
  }

  function updateNextRunDisplay(cfg) {
    var el = document.getElementById('auto-update-status');
    if (!el) return;
    if (cfg.auto_update_enabled) {
      el.textContent = 'Nächstes Update: täglich um ' + (cfg.auto_update_time || '03:00') + ' UTC';
    } else {
      el.textContent = 'Auto-Update deaktiviert.';
    }
  }

  async function saveNodeUrl() {
    var nodeUrl = document.getElementById('node-url-input').value.trim();
    var resp = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({node_url: nodeUrl})});
    if (resp.ok) { NODE_URL = nodeUrl; updateLinks(); location.reload(); }
  }

  async function saveConfig() {
    return saveNodeUrl();
  }

  async function saveAutoUpdate() {
    var enabled = document.getElementById('auto-update-enabled').checked;
    var t = document.getElementById('auto-update-time').value;
    var resp = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({auto_update_enabled: enabled, auto_update_time: t})});
    if (resp.ok) {
      var cfg = await resp.json();
      updateNextRunDisplay(cfg);
    }
  }

  async function refresh() {
    try {
      var resp = await fetch('/api/status');
      var d = await resp.json();
      document.getElementById('ts').textContent = 'Stand: ' + (d.timestamp || '');
      ALL_COUNTRIES = d.available_countries || [];
      renderContinentOptions(ALL_COUNTRIES);
      filterCountriesByContinent();
      renderWorkflow(d.workflow || {});
      renderCountries(d.selected_countries || []);
      renderQueuedCount(d.selected_countries || []);

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
      var errMsg = '<span class="hint" style="color:#e74c3c">&#9888; Fehler beim Laden (' + esc(String(e)) + ')</span>';
      ['workflow-body','country-list','svc-list','import-list','tiles-info'].forEach(function(id) {
        var el = document.getElementById(id);
        if (el && el.textContent === 'Lade ...') el.innerHTML = errMsg;
      });
    }
    if (refreshTimer) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 5000);
  }

  loadConfig();
  refresh();
  </script>
</body>
</html>
"""


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clone_default(value):
    return json.loads(json.dumps(value))


def ensure_dirs():
    for path in (
        STATUS_DIR,
        LIBRARY_DIR,
        IMPORT_DIR,
        VALHALLA_DIR,
        VALHALLA_STAGING_DIR,
        VALHALLA_TILE_DIR,
        NOMINATIM_DIR,
        TILESERVER_DIR,
    ):
        os.makedirs(path, exist_ok=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return clone_default(default)


def save_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{threading.get_ident()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def load_config():
    data = load_json(CONFIG_FILE, CONFIG_DEFAULTS)
    config = clone_default(CONFIG_DEFAULTS)
    if isinstance(data, dict):
        config.update({key: data.get(key, value) for key, value in CONFIG_DEFAULTS.items()})
    config["node_url"] = str(config.get("node_url") or "").strip()
    config["auto_update_enabled"] = bool(config.get("auto_update_enabled"))
    config["auto_update_time"] = str(config.get("auto_update_time") or "03:00")
    config["detected_node_url"] = detect_node_url() if not config["node_url"] else ""
    return config


def save_config(data):
    config = clone_default(CONFIG_DEFAULTS)
    config.update({key: data.get(key, value) for key, value in CONFIG_DEFAULTS.items()})
    save_json(CONFIG_FILE, config)
    return config


def valid_time_value(value):
    return bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value or ""))


def apply_config_update(payload):
    config = load_config()
    if "node_url" in payload:
        config["node_url"] = str(payload.get("node_url") or "").strip()
    if "auto_update_enabled" in payload:
        config["auto_update_enabled"] = bool(payload.get("auto_update_enabled"))
    if "auto_update_time" in payload:
        time_value = str(payload.get("auto_update_time") or "").strip()
        if not valid_time_value(time_value):
            raise ValueError("auto_update_time must use HH:MM format.")
        config["auto_update_time"] = time_value
    return save_config(config)


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
            if country.get("continent"):
                record["continent"] = country["continent"]
            break
    else:
        record = {
            "slug": country["slug"],
            "name": country["name"],
            "url": country["url"],
            "continent": country.get("continent", ""),
            "status": "pending",
            "added_at": now_iso(),
        }
        record.update(updates)
        records.append(record)
    save_library_records(records)


def delete_country_record(slug):
    records = list_library_records()
    to_delete = next((r for r in records if r.get("slug") == slug), None)
    if to_delete is None:
        raise ValueError(f"Country '{slug}' not found in library.")
    pbf_path = to_delete.get("pbf_path")
    if pbf_path and os.path.exists(pbf_path):
        os.remove(pbf_path)
    records = [r for r in records if r.get("slug") != slug]
    for record in records:
        if record.get("status") == "ready":
            record["status"] = "queued"
    save_library_records(records)


def mark_library_ready():
    records = list_library_records()
    timestamp = now_iso()
    for record in records:
        if not record.get("pbf_path"):
            continue
        record["status"] = "ready"
        record["imported_at"] = timestamp
        record["last_error"] = ""
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
    for country in sorted(COUNTRY_LIBRARY, key=lambda item: (item["continent"], item["name"])):
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
        return {"slug": slugify(custom_name), "name": custom_name, "url": custom_url, "continent": "Custom"}

    for country in COUNTRY_LIBRARY:
        if country["slug"] == slug:
            return dict(country)
    raise ValueError("Choose a country or provide custom country details.")


def collect_status():
    try:
        ensure_dirs()
    except OSError:
        pass

    def _check_service(svc):
        name, host, port, kind, path = svc
        if kind == "tcp":
            ok, detail = check_tcp(host, port)
        else:
            ok, detail = check_http(f"http://{host}:{port}{path or '/'}")
        return {"name": name, "ok": ok, "detail": detail}

    # Use wait() with an explicit timeout so a hung DNS lookup cannot block the
    # entire /api/status response indefinitely.  The executor is shut down
    # without waiting so that the few threads that are still resolving DNS can
    # finish in the background rather than blocking the response.
    svc_pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(SERVICES)))
    try:
        fs = {svc_pool.submit(_check_service, svc): svc for svc in SERVICES}
        done, pending = concurrent.futures.wait(fs, timeout=8)
        services = []
        for f in done:
            try:
                services.append(f.result())
            except Exception:  # noqa: BLE001
                services.append({"name": fs[f][0], "ok": False, "detail": "check error"})
        for f in pending:
            services.append({"name": fs[f][0], "ok": False, "detail": "timed out"})
    except Exception:  # noqa: BLE001
        services = [{"name": svc[0], "ok": False, "detail": "unavailable"} for svc in SERVICES]
    finally:
        svc_pool.shutdown(wait=False)

    import_files = []
    if os.path.isdir(IMPORT_DIR):
        for entry in sorted(os.listdir(IMPORT_DIR)):
            full_path = os.path.join(IMPORT_DIR, entry)
            try:
                size_mb = round(os.path.getsize(full_path) / (1024 * 1024), 1)
                mtime = datetime.fromtimestamp(os.path.getmtime(full_path), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
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
        "queued_count": len([record for record in records if record.get("status") == "queued"]),
        "workflow": read_workflow_state(),
    }


def run_command(args, *, input_text=None, check=True, timeout=60):
    try:
        result = subprocess.run(
            args,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise RuntimeError(f"Command not found: {args[0]!r}. Make sure it is installed and available in PATH.")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(args)}")
    if check and result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(output or f"Command failed: {' '.join(args)}")
    return result.stdout.strip()


def validate_pbf_file(path, *, label="OSM extract"):
    if not os.path.isfile(path):
        raise RuntimeError(f"{label} not found: {path}")
    if os.path.getsize(path) <= 0:
        raise RuntimeError(f"{label} is empty: {path}")
    run_command(["osmium", "fileinfo", "-F", "pbf", path], timeout=120)


def clear_directory(path):
    os.makedirs(path, exist_ok=True)
    for entry in os.listdir(path):
        full_path = os.path.join(path, entry)
        if os.path.isdir(full_path) and not os.path.islink(full_path):
            shutil.rmtree(full_path)
        else:
            os.unlink(full_path)


def wait_for_job_deletion(name, timeout_seconds=60):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            KUBE.get_job(name)
        except RuntimeError as exc:
            if "404" in str(exc):
                return
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for job deletion: {name}")


def download_country_file(country):
    destination = os.path.join(LIBRARY_DIR, f"{country['slug']}.osm.pbf")
    temp_path = f"{destination}.part"
    if os.path.exists(destination) and os.path.getsize(destination) > 0:
        try:
            validate_pbf_file(destination, label=f"Cached extract for {country['name']}")
        except RuntimeError:
            os.unlink(destination)
        else:
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
    validate_pbf_file(temp_path, label=f"Downloaded extract for {country['name']}")
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


def merge_library_files(country, records=None):
    merge_records = records or [
        record for record in list_library_records() if record.get("pbf_path") and record.get("status") != "error"
    ]
    input_paths = [record.get("pbf_path") for record in merge_records if record.get("pbf_path")]
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
        run_command(["osmium", "merge", "--overwrite", "-o", tmp_path, "-f", "pbf", *input_paths], timeout=7200)
    validate_pbf_file(tmp_path, label="Merged library extract")
    os.replace(tmp_path, MERGED_PBF)

    meta_path = f"{MERGED_PBF}.meta"
    with open(meta_path, "w", encoding="utf-8") as handle:
        handle.write(f"downloaded_at={now_iso()}\n")
        handle.write(f"path={MERGED_PBF}\n")
        handle.write(f"countries={','.join(record['slug'] for record in merge_records)}\n")
        for record in merge_records:
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
                            "image": "ghcr.io/gis-ops/docker-valhalla/valhalla:latest",
                            "imagePullPolicy": "Always",
                            "securityContext": {"runAsUser": 0, "runAsGroup": 0},
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
                                "echo '=== Tile build complete. Creating tile extract archive ...'\n"
                                "valhalla_build_extract -c /tmp/valhalla.json -e /data/valhalla_tiles.tar -O\n"
                                "echo '=== Import complete. Routing graph and tile extract stored at /data ==='\n"
                            ],
                            "volumeMounts": [
                                {"name": "import-data", "mountPath": "/data/import"},
                                # /data is the staging dir; rebuild_valhalla() atomically renames
                                # valhalla-staging → valhalla after this job succeeds, so
                                # /data/valhalla_tiles.tar ends up at the production path
                                # /mnt/data/OSM/valhalla/valhalla_tiles.tar expected by the
                                # valhalla deployment init container and the service itself.
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
                            "hostPath": {"path": "/mnt/data/OSM/valhalla-staging", "type": "DirectoryOrCreate"},
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


def build_tileserver_job_manifest():
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "tilemaker-import", "namespace": NAMESPACE},
        "spec": {
            "backoffLimit": 1,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "initContainers": [
                        {
                            "name": "preflight",
                            "image": "busybox:1.36",
                            "command": ["/bin/sh", "-c"],
                            "args": [
                                "echo '=== Planetiler Tile Generation ==='\n"
                                "if [ ! -f /data/import/planet.osm.pbf ]; then\n"
                                "  echo 'ERROR: /data/import/planet.osm.pbf not found'\n"
                                "  exit 1\n"
                                "fi\n"
                                "mkdir -p /data/tileserver\n"
                            ],
                            "volumeMounts": [
                                {"name": "osm-data", "mountPath": "/data"},
                            ],
                        }
                    ],
                    "containers": [
                        {
                            "name": "tilemaker-import",
                            "image": "ghcr.io/onthegomap/planetiler:latest",
                            "imagePullPolicy": "Always",
                            "resources": {"requests": {"memory": "2Gi"}, "limits": {"memory": "6Gi"}},
                            "securityContext": {"runAsUser": 0, "runAsGroup": 0},
                            "args": [
                                "--osm-path=/data/import/planet.osm.pbf",
                                "--output=/data/tileserver/map.mbtiles",
                                "--download",
                                "--force",
                            ],
                            "volumeMounts": [
                                {"name": "osm-data", "mountPath": "/data"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "osm-data",
                            "hostPath": {"path": "/mnt/data/OSM", "type": "DirectoryOrCreate"},
                        }
                    ],
                }
            },
        },
    }


def _routing_progress(elapsed_seconds):
    ramp_seconds = 1800
    pct = 60 + 17 * min(elapsed_seconds, ramp_seconds) / ramp_seconds
    return int(pct)


def _tiles_progress(elapsed_seconds):
    ramp_seconds = 2400
    pct = 50 + 8 * min(elapsed_seconds, ramp_seconds) / ramp_seconds
    return int(pct)


def rebuild_valhalla(country):
    clear_directory(VALHALLA_STAGING_DIR)
    KUBE.delete_job("valhalla-import")
    wait_for_job_deletion("valhalla-import")
    write_workflow_state(
        running=True,
        phase="routing",
        progress=60,
        message="Starting Valhalla rebuild ...",
        detail="Submitting valhalla-import job to the staging directory.",
        country=country["name"],
        error="",
    )
    KUBE.create_job(build_valhalla_job_manifest())

    job_start = time.time()
    deadline = job_start + 7200
    while time.time() < deadline:
        try:
            job_data = KUBE.get_job("valhalla-import")
        except RuntimeError:
            time.sleep(3)
            continue
        status = job_data.get("status", {})
        if status.get("succeeded", 0) >= 1:
            write_workflow_state(
                running=True,
                phase="routing",
                progress=78,
                message="Swapping staged Valhalla graph into production ...",
                detail="Routing graph built successfully. Performing atomic swap and rolling restart.",
                country=country["name"],
                error="",
            )
            valhalla_old = os.path.join(DATA_DIR, "valhalla-old")
            if os.path.exists(valhalla_old):
                shutil.rmtree(valhalla_old, ignore_errors=True)
            if os.path.exists(VALHALLA_DIR):
                os.rename(VALHALLA_DIR, valhalla_old)
            os.rename(VALHALLA_STAGING_DIR, VALHALLA_DIR)
            if os.path.exists(valhalla_old):
                shutil.rmtree(valhalla_old, ignore_errors=True)
            KUBE.rollout_restart("valhalla")
            write_workflow_state(
                running=True,
                phase="routing",
                progress=78,
                message="Valhalla routing graph rebuilt.",
                detail="Staged graph swapped in and rollout restart triggered.",
                country=country["name"],
                error="",
            )
            return
        if status.get("failed", 0) > 0:
            logs = KUBE.get_job_logs("valhalla-import")
            raise RuntimeError(logs or "Valhalla import job failed.")

        elapsed = time.time() - job_start
        elapsed_min = int(elapsed // 60)
        elapsed_sec = int(elapsed % 60)
        last_log = ""
        try:
            raw_logs = KUBE.get_job_logs("valhalla-import", tail_lines=10)
            lines = [line.strip() for line in raw_logs.splitlines() if line.strip()]
            if lines:
                last_log = lines[-1]
        except Exception:  # noqa: BLE001
            pass

        detail_parts = [f"Läuft seit {elapsed_min:02d}:{elapsed_sec:02d} min."]
        if last_log:
            detail_parts.append(f"Log: {last_log}")

        write_workflow_state(
            running=True,
            phase="routing",
            progress=_routing_progress(elapsed),
            message="Valhalla baut den Routing-Graphen ...",
            detail=" | ".join(detail_parts),
            country=country["name"],
            error="",
        )
        time.sleep(5)
    raise RuntimeError("Timed out waiting for the Valhalla import job to finish.")


def rebuild_tileserver(country):
    KUBE.delete_job("tilemaker-import")
    wait_for_job_deletion("tilemaker-import")
    write_workflow_state(
        running=True,
        phase="tiles",
        progress=50,
        message="Starting TileServer rebuild ...",
        detail="Submitting tilemaker-import job.",
        country=country["name"],
        error="",
    )
    KUBE.create_job(build_tileserver_job_manifest())

    job_start = time.time()
    deadline = job_start + 7200
    while time.time() < deadline:
        try:
            job_data = KUBE.get_job("tilemaker-import")
        except RuntimeError:
            time.sleep(3)
            continue
        status = job_data.get("status", {})
        if status.get("succeeded", 0) >= 1:
            mbtiles_path = os.path.join(TILESERVER_DIR, "map.mbtiles")
            if not os.path.exists(mbtiles_path) or os.path.getsize(mbtiles_path) == 0:
                raise RuntimeError("Tile generation job succeeded but map.mbtiles was not created or is empty.")
            KUBE.rollout_restart("tileserver-gl")
            write_workflow_state(
                running=True,
                phase="tiles",
                progress=58,
                message="TileServer MBTiles rebuilt.",
                detail="Local vector tiles are ready. Rollout restart triggered.",
                country=country["name"],
                error="",
            )
            return
        if status.get("failed", 0) > 0:
            logs = KUBE.get_job_logs("tilemaker-import")
            raise RuntimeError(logs or "Tile generation job failed.")

        elapsed = time.time() - job_start
        elapsed_min = int(elapsed // 60)
        elapsed_sec = int(elapsed % 60)
        last_log = ""
        try:
            raw_logs = KUBE.get_job_logs("tilemaker-import", tail_lines=10)
            lines = [line.strip() for line in raw_logs.splitlines() if line.strip()]
            if lines:
                last_log = lines[-1]
        except Exception:  # noqa: BLE001
            pass

        detail_parts = [f"Läuft seit {elapsed_min:02d}:{elapsed_sec:02d} min."]
        if last_log:
            detail_parts.append(f"Log: {last_log}")

        write_workflow_state(
            running=True,
            phase="tiles",
            progress=_tiles_progress(elapsed),
            message="Planetiler erzeugt lokale Karten-Tiles ...",
            detail=" | ".join(detail_parts),
            country=country["name"],
            error="",
        )
        time.sleep(10)
    raise RuntimeError("Timed out waiting for the TileServer import job to finish.")


def scale_nominatim(replicas):
    KUBE.scale_deployment("nominatim", replicas)


def wait_for_nominatim_pods_to_stop(timeout_seconds=300):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            pods = KUBE.list_pods("app=nominatim")
        except RuntimeError:
            time.sleep(3)
            continue
        if not pods.get("items"):
            return
        time.sleep(3)
    raise RuntimeError("Timed out waiting for the Nominatim pod to stop.")


def wait_for_nominatim_ready(country):
    deadline = time.time() + 7200
    while time.time() < deadline:
        try:
            deployment = KUBE.get_deployment("nominatim")
        except RuntimeError:
            time.sleep(10)
            continue
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


def run_parallel_build_steps(country, records=None):
    merge_library_files(country, records=records)
    valhalla_err = [None]
    tileserver_err = [None]

    def _valhalla_thread():
        try:
            rebuild_valhalla(country)
        except Exception as exc:  # noqa: BLE001
            valhalla_err[0] = exc

    def _tileserver_thread():
        try:
            rebuild_tileserver(country)
        except Exception as exc:  # noqa: BLE001
            tileserver_err[0] = exc

    t_v = threading.Thread(target=_valhalla_thread, daemon=True)
    t_t = threading.Thread(target=_tileserver_thread, daemon=True)
    t_v.start()
    t_t.start()
    t_v.join()
    t_t.join()
    if valhalla_err[0]:
        raise valhalla_err[0]
    if tileserver_err[0]:
        raise tileserver_err[0]
    rebuild_nominatim(country)
    mark_library_ready()


def finish_workflow_thread():
    ACTIVE_WORKFLOW["thread"] = None
    if WORKFLOW_LOCK.locked():
        WORKFLOW_LOCK.release()


def run_queue_workflow(country):
    try:
        ensure_dirs()
        upsert_country_record(country, status="pending", added_at=now_iso(), last_error="")
        write_workflow_state(
            running=True,
            phase="queued",
            progress=2,
            message=f"Preparing queue download for {country['name']}.",
            detail="Preparing directories and state files.",
            country=country["name"],
            error="",
        )
        download_country_file(country)
        upsert_country_record(country, status="queued", last_error="")
        queued_count = len([record for record in list_library_records() if record.get("status") == "queued"])
        write_workflow_state(
            running=False,
            phase="queued",
            progress=45,
            message=f"{country['name']} downloaded and queued.",
            detail=f"{queued_count} country/countries waiting for the next build.",
            country=country["name"],
            error="",
        )
    except Exception as exc:  # noqa: BLE001
        upsert_country_record(country, status="error", last_error=str(exc))
        write_workflow_state(
            running=False,
            phase="error",
            progress=100,
            message=f"Queue workflow failed for {country['name']}.",
            detail="See the captured error below.",
            country=country["name"],
            error=str(exc),
        )
    finally:
        finish_workflow_thread()


def run_build_workflow(country=None):
    country = country or {"name": "Library", "slug": "library", "url": ""}
    try:
        ensure_dirs()
        records = [
            record
            for record in list_library_records()
            if record.get("pbf_path") and record.get("status") in {"queued", "ready"}
        ]
        if not records:
            raise RuntimeError("No queued or ready countries available for a build.")
        write_workflow_state(
            running=True,
            phase="building",
            progress=40,
            message="Starting library build ...",
            detail=f"Preparing {len(records)} queued/ready country extract(s) for the shared build pipeline.",
            country=country["name"],
            error="",
        )
        run_parallel_build_steps(country, records=records)
        write_workflow_state(
            running=False,
            phase="done",
            progress=100,
            message="Library build completed.",
            detail="Merged extract, local tiles, routing and search data are ready.",
            country=country["name"],
            error="",
        )
    except Exception as exc:  # noqa: BLE001
        write_workflow_state(
            running=False,
            phase="error",
            progress=100,
            message="Library build failed.",
            detail="See the captured error below.",
            country=country["name"],
            error=str(exc),
        )
    finally:
        finish_workflow_thread()


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
        run_parallel_build_steps(country)
        write_workflow_state(
            running=False,
            phase="done",
            progress=100,
            message=f"{country['name']} added to the library.",
            detail="Merged extract, local tiles, routing and address search are ready.",
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
        finish_workflow_thread()


def start_country_workflow(payload):
    country = resolve_country_request(payload)
    if not WORKFLOW_LOCK.acquire(blocking=False):
        raise RuntimeError("Another country library workflow is already running.")
    thread = threading.Thread(target=run_country_workflow, args=(country,), daemon=True)
    ACTIVE_WORKFLOW["thread"] = thread
    thread.start()
    return country


def start_queue_workflow(payload):
    country = resolve_country_request(payload)
    if not WORKFLOW_LOCK.acquire(blocking=False):
        raise RuntimeError("Another country library workflow is already running.")
    thread = threading.Thread(target=run_queue_workflow, args=(country,), daemon=True)
    ACTIVE_WORKFLOW["thread"] = thread
    thread.start()
    return country


def start_build_workflow():
    if not WORKFLOW_LOCK.acquire(blocking=False):
        raise RuntimeError("Another country library workflow is already running.")
    thread = threading.Thread(target=run_build_workflow, daemon=True)
    ACTIVE_WORKFLOW["thread"] = thread
    thread.start()
    return {
        "count": len(
            [record for record in list_library_records() if record.get("pbf_path") and record.get("status") in {"queued", "ready"}]
        )
    }


def run_scheduler_loop():
    while True:
        time.sleep(60)
        if not SCHEDULER_LOCK.acquire(blocking=False):
            continue
        try:
            cfg = load_config()
            if not cfg.get("auto_update_enabled"):
                continue
            schedule_time = cfg.get("auto_update_time", "03:00")
            now_utc = datetime.now(timezone.utc)
            now_hm = now_utc.strftime("%H:%M")
            today = now_utc.strftime("%Y-%m-%d")
            if now_hm == schedule_time and LAST_AUTO_RUN["date"] != today:
                LAST_AUTO_RUN["date"] = today
                records = list_library_records()
                if records and not WORKFLOW_LOCK.locked():
                    if WORKFLOW_LOCK.acquire(blocking=False):
                        thread = threading.Thread(target=run_build_workflow, daemon=True)
                        ACTIVE_WORKFLOW["thread"] = thread
                        thread.start()
        except Exception as exc:  # noqa: BLE001
            print(f"Auto-update scheduler error: {exc}", flush=True)
        finally:
            SCHEDULER_LOCK.release()


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
            cfg = load_config()
            effective_url = cfg.get("node_url") or cfg.get("detected_node_url", "")
            body = INDEX_HTML.replace("{{NODE_URL}}", effective_url).encode("utf-8")
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
            try:
                self._send_json(collect_status())
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, 500)
            return

        if self.path == "/api/config":
            self._send_json(load_config())
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

        if self.path == "/api/library/queue":
            try:
                country = start_queue_workflow(payload)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, 409)
                return
            self._send_json({"status": "queued", "country": country})
            return

        if self.path == "/api/library/remove":
            slug = payload.get("slug", "").strip()
            if not slug:
                self._send_json({"error": "slug is required"}, 400)
                return
            if WORKFLOW_LOCK.locked():
                self._send_json({"error": "A workflow is currently running."}, 409)
                return
            try:
                delete_country_record(slug)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 404)
                return
            self._send_json({"status": "removed", "slug": slug})
            return

        if self.path == "/api/library/build":
            try:
                build = start_build_workflow()
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, 409)
                return
            self._send_json({"status": "started", "build": build})
            return

        if self.path == "/api/config":
            try:
                config = apply_config_update(payload)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json(config)
            return

        self._send_json({"error": "not found"}, 404)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    ensure_dirs()
    _startup_state = read_workflow_state()
    if _startup_state.get("running"):
        write_workflow_state(
            running=False,
            phase="interrupted",
            progress=0,
            message="Workflow was interrupted by a pod restart.",
            detail="Start a new workflow to continue.",
            error="",
        )
    sched = threading.Thread(target=run_scheduler_loop, daemon=True)
    sched.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"OSM status server listening on {HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
