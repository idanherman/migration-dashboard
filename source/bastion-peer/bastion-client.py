# bastion-client.py — web-first dashboard, background probes, per-protocol internal tables, Clear History button
import asyncio
import aiohttp
import websockets
import logging
import socket
import ssl as ssl_module
import os
import json
from datetime import datetime, timezone
from typing import Union
from aiohttp import web, TCPConnector

# Initialize logging early
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')

# ---------- Config ----------
# Load configuration from environment variables or use defaults
def load_config():
    """Load configuration from environment variables with sensible defaults."""
    config = {}
    
    # MetalLB peers (JSON format: {"peer-1-lb": "IP", "peer-2-lb": "IP", "peer-3-lb": "IP"})
    metallb_json = os.getenv("METALLB_PEERS", '{"peer-1-lb": "172.17.95.211", "peer-2-lb": "172.17.95.212", "peer-3-lb": "172.17.95.210"}')
    try:
        config["METALLB_PEERS"] = json.loads(metallb_json)
    except json.JSONDecodeError:
        logging.warning("Invalid METALLB_PEERS JSON, using defaults")
        config["METALLB_PEERS"] = {"peer-1-lb": "172.17.95.211", "peer-2-lb": "172.17.95.212", "peer-3-lb": "172.17.95.210"}
    
    # NodePort peers (JSON format)
    nodeport_json = os.getenv("NODEPORT_PEERS", '{"peer-1-np": {"host": "172.17.95.101", "ws_port": 30926, "tcp_port": 30808, "http_port": 30402}, "peer-2-np": {"host": "172.17.95.102", "ws_port": 31183, "tcp_port": 30565, "http_port": 31865}, "peer-3-np": {"host": "172.17.95.103", "ws_port": 31560, "tcp_port": 31004, "http_port": 30067}}')
    try:
        config["NODEPORT_PEERS"] = json.loads(nodeport_json)
    except json.JSONDecodeError:
        logging.warning("Invalid NODEPORT_PEERS JSON, using defaults")
        config["NODEPORT_PEERS"] = {"peer-1-np": {"host": "172.17.95.101", "ws_port": 30926, "tcp_port": 30808, "http_port": 30402}, "peer-2-np": {"host": "172.17.95.102", "ws_port": 31183, "tcp_port": 30565, "http_port": 31865}, "peer-3-np": {"host": "172.17.95.103", "ws_port": 31560, "tcp_port": 31004, "http_port": 30067}}
    
    # Route peers (comma-separated URLs) - optional, for legacy external tests
    route_str = os.getenv("ROUTE_PEERS", "")
    config["ROUTE_PEERS"] = [url.strip() for url in route_str.split(",") if url.strip()]

    # Per-node status endpoints (NodePort with externalTrafficPolicy: Local) - one URL per node
    node_endpoints = os.getenv("NODE_STATUS_ENDPOINTS", "")
    config["NODE_STATUS_ENDPOINTS"] = [url.strip() for url in node_endpoints.split(",") if url.strip()]

    # Shared OpenShift Route (bastion → router → Service); from deploy.sh when APPLY_ROUTE_PROBE=yes
    config["ROUTE_PROBE_URL"] = os.getenv("ROUTE_PROBE_URL", "").strip()

    # Intervals (in seconds)
    config["HTTP_INTERVAL"] = float(os.getenv("HTTP_INTERVAL", "1.0"))
    config["WS_INTERVAL"] = float(os.getenv("WS_INTERVAL", "0.5"))
    config["TCP_INTERVAL"] = float(os.getenv("TCP_INTERVAL", "0.5"))
    config["POLL_INTERVAL"] = float(os.getenv("POLL_INTERVAL", "1.0"))
    config["RECONNECT_DELAY"] = float(os.getenv("RECONNECT_DELAY", "1.0"))
    
    # Timeouts (in seconds)
    config["HTTP_TIMEOUT"] = float(os.getenv("HTTP_TIMEOUT", "1.0"))
    config["WS_OPEN_TIMEOUT"] = float(os.getenv("WS_OPEN_TIMEOUT", "1.0"))
    config["WS_PONG_TIMEOUT"] = config["WS_INTERVAL"] + 0.3
    config["TCP_CONNECT_TIMEOUT"] = float(os.getenv("TCP_CONNECT_TIMEOUT", "1.0"))
    config["TCP_ECHO_TIMEOUT"] = config["TCP_INTERVAL"] + 0.3
    
    # Dashboard settings
    config["DASHBOARD_PORT"] = int(os.getenv("DASHBOARD_PORT", "9091"))
    config["MAX_HISTORY"] = int(os.getenv("MAX_HISTORY", "200"))
    
    # SSL verification for HTTPS (e.g. OpenShift routes with self-signed cert). Set to false to skip.
    config["SSL_VERIFY"] = os.getenv("SSL_VERIFY", "true").lower() not in ("0", "false", "no")
    
    return config

CONFIG = load_config()
METALLB_PEERS = CONFIG["METALLB_PEERS"]
NODEPORT_PEERS = CONFIG["NODEPORT_PEERS"]
ROUTE_PEERS = CONFIG["ROUTE_PEERS"]
NODE_STATUS_ENDPOINTS = CONFIG["NODE_STATUS_ENDPOINTS"]
ROUTE_PROBE_URL = CONFIG["ROUTE_PROBE_URL"]
HTTP_INTERVAL = CONFIG["HTTP_INTERVAL"]
WS_INTERVAL = CONFIG["WS_INTERVAL"]
TCP_INTERVAL = CONFIG["TCP_INTERVAL"]
POLL_INTERVAL = CONFIG["POLL_INTERVAL"]
RECONNECT_DELAY = CONFIG["RECONNECT_DELAY"]
HTTP_TIMEOUT = CONFIG["HTTP_TIMEOUT"]
WS_OPEN_TIMEOUT = CONFIG["WS_OPEN_TIMEOUT"]
WS_PONG_TIMEOUT = CONFIG["WS_PONG_TIMEOUT"]
TCP_CONNECT_TIMEOUT = CONFIG["TCP_CONNECT_TIMEOUT"]
TCP_ECHO_TIMEOUT = CONFIG["TCP_ECHO_TIMEOUT"]
DASHBOARD_PORT = CONFIG["DASHBOARD_PORT"]
MAX_HISTORY = CONFIG["MAX_HISTORY"]
SSL_VERIFY = CONFIG["SSL_VERIFY"]


def _http_connector() -> TCPConnector:
    """aiohttp outbound HTTPS; use unverified context when SSL_VERIFY is false (lab / OpenShift routes)."""
    if SSL_VERIFY:
        return TCPConnector()
    ctx = ssl_module.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl_module.CERT_NONE
    return TCPConnector(ssl=ctx)


now_iso = lambda: datetime.now(timezone.utc).isoformat()

STATE = {
    "external_tests": {},   # { "<name> (HTTP|TCP|WS)": {status, error, last_update} }
    "internal_status": {},  # { "peer-1-route": {.../status json...}, ... }
    "history": []           # merged outages (bastion + peers)
}

# cutoff to ignore peer events older than the last "clear"
HISTORY_IGNORE_BEFORE = None  # ISO string or None
# NODE_STATUS_ENDPOINTS url -> node_name (so poll failures update the right row, not a stale duplicate)
URL_NODE_KEYS: dict[str, str] = {}
# node_name -> pod IPs seen this session (keeps mesh history after a pod IP changes)
NODE_IP_HISTORY: dict[str, set[str]] = {}
# Drop pod→IP events only when older than this and IP is not a known IP for that node
STALE_MESH_MAX_AGE_SEC = float(os.getenv("STALE_MESH_MAX_AGE_SEC", "900"))

# ---------- Helpers ----------
def set_state(section: str, name: str, status: str, error: Union[Exception, str] = ""):
    STATE[section][name] = {"status": status, "error": "" if not error else str(error), "last_update": now_iso()}
    if status == "error":
        logging.warning(f"[{section.upper()}] {name} -> error: {error}")

def _probe_target_node(probe_name: str) -> str:
    """Map peer-N-np / ingress-N probe name to OpenShift node name (NODE_STATUS_ENDPOINTS order)."""
    parts = (probe_name or "").split("-")
    if len(parts) >= 3 and parts[0] == "peer" and parts[-1] == "np":
        try:
            idx = int(parts[1]) - 1
            if 0 <= idx < len(NODE_STATUS_ENDPOINTS):
                return URL_NODE_KEYS.get(NODE_STATUS_ENDPOINTS[idx], "") or ""
        except ValueError:
            pass
    return ""


def _build_pod_to_node() -> dict:
    pod_to_node = {}
    for _key, data in STATE["internal_status"].items():
        if _key.startswith("http://") or _key.startswith("https://"):
            continue
        if not isinstance(data, dict) or "error" in data:
            continue
        self_info = data.get("self") or {}
        pod = self_info.get("pod_name")
        node = self_info.get("node_name") or _key
        if pod and node:
            pod_to_node[pod] = node
    return pod_to_node


def _note_node_pod_ip(node: str, ip: str) -> None:
    if node and ip:
        NODE_IP_HISTORY.setdefault(node, set()).add(ip)


def _ip_to_node_map() -> dict:
    """Current + previously seen pod IPs per node (for mesh history labels and stale filter)."""
    m: dict[str, str] = {}
    for node, ips in NODE_IP_HISTORY.items():
        for ip in ips:
            m[ip] = node
    _, live = _build_node_order_and_ip_map()
    m.update(live)
    return m


def _current_peer_ips() -> set:
    ips = set()
    for _key, data in STATE["internal_status"].items():
        if _key.startswith("http://") or _key.startswith("https://"):
            continue
        if not isinstance(data, dict):
            continue
        pod_ip = (data.get("self") or {}).get("pod_ip")
        if pod_ip:
            ips.add(pod_ip)
    return ips


def _is_stale_history_event(ev: dict, current_ips: set) -> bool:
    """Drop ancient pod→IP events; keep recent mesh outages and IPs seen on a known node."""
    if ev.get("source") != "pod":
        return False
    target_ip = ev.get("name") or ""
    if not target_ip or "." not in target_ip:
        return False
    if target_ip in current_ips:
        return False
    target_node = ev.get("target_node")
    if target_node and target_ip in NODE_IP_HISTORY.get(target_node, set()):
        return False
    try:
        end = datetime.fromisoformat(ev.get("end_time", ""))
        if (datetime.now(timezone.utc) - end).total_seconds() < STALE_MESH_MAX_AGE_SEC:
            return False
    except Exception:
        return False
    return True


def _history_label(ev: dict, ip_to_node: dict, pod_to_node: dict) -> str:
    src = ev.get("source") or ""
    proto = ev.get("protocol") or "?"
    if src == "bastion":
        if ev.get("name") == "route-probe":
            return f"Bastion → OpenShift Route ({proto})"
        node = ev.get("target_node") or _probe_target_node(ev.get("name", ""))
        return f"Bastion → {node or ev.get('name', '?')} ({proto})"
    if src == "pod":
        reporter = ev.get("reporter_node") or pod_to_node.get(ev.get("reporter"), ev.get("reporter", "?"))
        target_ip = ev.get("name", "")
        target = ev.get("target_node") or ip_to_node.get(target_ip, target_ip)
        if target != target_ip:
            return f"Mesh {reporter} → {target} ({proto})"
        return f"Mesh {reporter} → {target_ip} ({proto})"
    return ev.get("name", "?")


def _fmt_time_range(ev: dict) -> str:
    s = (ev.get("start_time") or "")[:19].replace("T", " ")
    e = (ev.get("end_time") or "")[:19].replace("T", " ")
    return f"{s} – {e} UTC"


def _prune_stale_history() -> None:
    current_ips = _current_peer_ips()
    if not current_ips:
        return
    STATE["history"] = [
        ev for ev in STATE["history"]
        if _is_after_cutoff(ev) and not _is_stale_history_event(ev, current_ips)
    ]


def _history_for_api(ip_to_node: dict) -> list:
    _prune_stale_history()
    ip_map = _ip_to_node_map()
    pod_to_node = _build_pod_to_node()
    enriched = []
    for ev in STATE["history"]:
        if not _is_after_cutoff(ev):
            continue
        row = dict(ev)
        if row.get("source") == "pod":
            row["target_node"] = row.get("target_node") or ip_map.get(row.get("name", ""), "")
            row["reporter_node"] = row.get("reporter_node") or pod_to_node.get(
                row.get("reporter", ""), row.get("reporter", "")
            )
        elif row.get("source") == "bastion" and not row.get("target_node"):
            row["target_node"] = _probe_target_node(row.get("name", ""))
        row["label"] = _history_label(row, ip_map, pod_to_node)
        row["time_range"] = _fmt_time_range(row)
        enriched.append(row)
    enriched.sort(key=lambda h: h.get("end_time") or "", reverse=True)
    return enriched


def log_disconnection_event(name: str, proto: str, start_time_str: str, *, source: str = "bastion"):
    try:
        start = datetime.fromisoformat(start_time_str)
        end = datetime.now(timezone.utc)
        dur = (end - start).total_seconds()
        entry = {
            "name": name,
            "protocol": proto,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "duration_sec": round(dur, 2),
            "source": source,
        }
        if source == "bastion":
            entry["target_node"] = _probe_target_node(name)
        STATE["history"].insert(0, entry)
        del STATE["history"][MAX_HISTORY:]
        _prune_stale_history()
        logging.info(f"--- OUTAGE ENDED: {name} ({proto}, {source}) {dur:.2f}s ---")
    except Exception as e:
        logging.error(f"History error: {e}")

def _is_after_cutoff(ev: dict) -> bool:
    """Return True if event should be kept given HISTORY_IGNORE_BEFORE."""
    global HISTORY_IGNORE_BEFORE
    if not HISTORY_IGNORE_BEFORE:
        return True
    try:
        end_t = ev.get("end_time") or ev.get("start_time")
        if not end_t:
            return True
        return datetime.fromisoformat(end_t) >= datetime.fromisoformat(HISTORY_IGNORE_BEFORE)
    except Exception:
        return True

# ---------- Probes ----------
async def http_client_task(name: str, base_url: str, ping_path="/ping", *, log_history: bool = True):
    url, label = f"{base_url}{ping_path}", f"{name} (HTTP)"
    conn = {"status": "unknown", "since": now_iso()}
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    connector = _http_connector()
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        while True:
            try:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    if log_history and conn["status"] == "error":
                        log_disconnection_event(name, "HTTP", conn["since"], source="bastion")
                    conn["status"] = "connected"; conn["since"] = now_iso()
                    set_state("external_tests", label, "connected")
            except Exception as e:
                if conn["status"] != "error":
                    conn["status"] = "error"; conn["since"] = now_iso()
                set_state("external_tests", label, "error", e)
            await asyncio.sleep(HTTP_INTERVAL)

async def ws_client_task(name: str, host: str, port: int):
    uri, label = f"ws://{host}:{port}", f"{name} (WS)"
    conn = {"status": "unknown", "since": now_iso()}
    while True:
        try:
            async with websockets.connect(uri, open_timeout=WS_OPEN_TIMEOUT) as ws:
                if conn["status"] == "error":
                    log_disconnection_event(name, "WS", conn["since"], source="bastion")
                conn["status"] = "connected"; conn["since"] = now_iso()
                set_state("external_tests", label, "connected")
                while True:
                    await ws.send(f"ping {now_iso()}")
                    pong_waiter = ws.ping()
                    await asyncio.wait_for(pong_waiter, timeout=WS_PONG_TIMEOUT)
                    await asyncio.sleep(WS_INTERVAL)
        except Exception as e:
            if conn["status"] != "error":
                conn["status"] = "error"; conn["since"] = now_iso()
            set_state("external_tests", label, "error", e)
            await asyncio.sleep(RECONNECT_DELAY)

async def tcp_client_task(name: str, host: str, port: int):
    label = f"{name} (TCP)"
    conn = {"status": "unknown", "since": now_iso()}
    while True:
        reader = writer = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=TCP_CONNECT_TIMEOUT)
            try:
                sock = writer.get_extra_info("socket")
                if sock: sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except Exception:
                pass
            if conn["status"] == "error":
                log_disconnection_event(name, "TCP", conn["since"], source="bastion")
            conn["status"] = "connected"; conn["since"] = now_iso()
            set_state("external_tests", label, "connected")
            while True:
                line = f"ping {now_iso()}\n".encode()
                writer.write(line); await writer.drain()
                echo = await asyncio.wait_for(reader.readline(), timeout=TCP_ECHO_TIMEOUT)
                if echo != line: raise RuntimeError("TCP echo mismatch")
                await asyncio.sleep(TCP_INTERVAL)
        except Exception as e:
            if conn["status"] != "error":
                conn["status"] = "error"; conn["since"] = now_iso()
            set_state("external_tests", label, "error", e)
        finally:
            if writer:
                try: writer.close(); await writer.wait_closed()
                except Exception: pass
            await asyncio.sleep(RECONNECT_DELAY)

def _merge_peer_history(peer_hist: list) -> None:
    """Merge peer history into STATE['history'], dedupe, sort, cap."""
    ip_map = _ip_to_node_map()
    filtered = []
    for ev in peer_hist:
        ev = dict(ev)
        ev.setdefault("source", "pod")
        target_ip = ev.get("name") or ""
        if target_ip:
            ev["target_node"] = ip_map.get(target_ip, ev.get("target_node", ""))
            if ev["target_node"]:
                _note_node_pod_ip(ev["target_node"], target_ip)
        if _is_after_cutoff(ev):
            filtered.append(ev)
    if not filtered:
        return
    known = {
        (h.get("name"), h.get("protocol"), h.get("start_time"),
         h.get("end_time"), h.get("source"), h.get("reporter"))
        for h in STATE["history"]
    }
    new_items = [ev for ev in filtered if (ev.get("name"), ev.get("protocol"), ev.get("start_time"),
                ev.get("end_time"), ev.get("source"), ev.get("reporter")) not in known]
    if new_items:
        STATE["history"][0:0] = new_items
        STATE["history"].sort(key=lambda h: h.get("end_time") or "", reverse=True)
        del STATE["history"][MAX_HISTORY:]
        _prune_stale_history()

async def poll_peer_status_task(name: str, base_url: str):
    """Legacy: poll by name and base_url (for ROUTE_PEERS)."""
    status_url, history_url = f"{base_url.rstrip('/')}/status", f"{base_url.rstrip('/')}/history"
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    connector = _http_connector()
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        while True:
            try:
                async with session.get(status_url) as resp:
                    resp.raise_for_status()
                    STATE["internal_status"][name] = await resp.json()
            except Exception as e:
                STATE["internal_status"][name] = {"error": str(e), "url": status_url}
            try:
                async with session.get(history_url) as resp:
                    resp.raise_for_status()
                    _merge_peer_history(await resp.json())
            except Exception:
                pass
            await asyncio.sleep(POLL_INTERVAL)

async def poll_node_status_task(url: str):
    """Poll one node endpoint (NodePort or Route); key internal_status by node_name from response."""
    status_url = f"{url.rstrip('/')}/status"
    history_url = f"{url.rstrip('/')}/history"
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    connector = _http_connector()
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        while True:
            try:
                async with session.get(status_url) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    self_info = data.get("self") or {}
                    key = self_info.get("node_name") or self_info.get("pod_name") or url
                    URL_NODE_KEYS[url] = key
                    STATE["internal_status"][key] = data
                    if self_info.get("pod_ip"):
                        _note_node_pod_ip(key, self_info["pod_ip"])
                    if url != key:
                        STATE["internal_status"].pop(url, None)
                    _prune_stale_history()
            except Exception as e:
                key = URL_NODE_KEYS.get(url, url)
                prev = STATE["internal_status"].get(key)
                prev_self = prev.get("self") if isinstance(prev, dict) and "error" not in prev else None
                STATE["internal_status"][key] = {
                    "error": str(e),
                    "url": status_url,
                    "last_update": now_iso(),
                    "self": prev_self,
                }
                if url != key:
                    STATE["internal_status"].pop(url, None)
            try:
                async with session.get(history_url) as resp:
                    resp.raise_for_status()
                    _merge_peer_history(await resp.json())
            except Exception:
                pass
            await asyncio.sleep(POLL_INTERVAL)

# ---------- Web UI ----------
HTML_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8"/><title>OVN Migration Dashboard</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;background:#f5f5f5;color:#333;margin:0;padding:20px}
h1{color:#d70000;border-bottom:2px solid #eee;padding-bottom:10px;margin-top:0;font-weight:400}
h2{color:#111;border-bottom:1px solid #ccc;padding-bottom:8px;margin-top:18px}
#timestamp{color:#555;font-size:.9em;margin-bottom:20px}
#container{display:flex;flex-wrap:wrap;gap:20px}
.section{flex:1;min-width:500px;background:#fff;border:1px solid #ddd;border-radius:8px;padding:20px;box-shadow:0 2px 8px rgba(0,0,0,.05)}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{border:1px solid #ddd;padding:10px 12px;text-align:center}
th{background:#f9f9f9;font-weight:600}
td:first-child{text-align:left;font-weight:bold;background:#f9f9f9}
.status-ok{background:#e6ffed;color:#1e7e34;font-weight:bold}
.status-error{background:#ffebee;color:#d70000;font-weight:bold}
.status-unknown{background:#fafafa;color:#777}
.internal-diagonal{background:#fafafa;border:1px solid #e8e8e8}
.mermaid-legend{font-size:13px;color:#555;margin-top:10px;line-height:1.5}
pre{background:#fff;border:1px solid #ddd;padding:15px;border-radius:5px;white-space:pre-wrap;word-wrap:break-word;font-family:SFMono-Regular,Consolas,Menlo,monospace;font-size:13px;line-height:1.6}
.history-item{border-bottom:1px solid #eee;padding:8px 2px;margin-bottom:5px}
.history-item:last-child{border-bottom:none}
.hist-down{color:#d70000;font-weight:bold}
.btn{border:1px solid #ccc;background:#fafafa;border-radius:6px;padding:6px 10px;cursor:pointer;font-size:13px}
.btn:hover{background:#f0f0f0}
.btn:disabled{opacity:.6;cursor:not-allowed}
.header-row{display:flex;align-items:center;justify-content:space-between}
#mermaid-container{min-height:160px;font-size:13px;overflow:auto}
</style>
<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
</head>
<body>
<h1>Live Migration Dashboard</h1>
<div id="timestamp">Loading...</div>

<div id="container">
  <div class="section">
    <h2>External Tests (Bastion → Cluster)</h2>
    <table id="metallb-matrix"><thead><tr><th>Target (LoadBalancer)</th><th>HTTP</th><th>WebSocket (WS)</th><th>Raw TCP</th></tr></thead><tbody id="metallb-matrix-body"></tbody></table><br/>
    <table id="external-matrix"><thead><tr><th>Target (NodePort)</th><th>HTTP</th><th>WebSocket (WS)</th><th>Raw TCP</th></tr></thead><tbody id="external-matrix-body"></tbody></table><br/>
    <table id="route-probe-matrix"><thead><tr><th>Target (OpenShift Route)</th><th>HTTP /ping</th><th>Host</th></tr></thead><tbody id="route-probe-matrix-body"></tbody></table>
  </div>

  <div class="section">
    <h2>Connectivity graph</h2>
    <div id="mermaid-container" class="mermaid"></div>
    <p class="mermaid-legend"><b>Bastion → node:</b> solid = NodePort <code>/ping</code> on that node&rsquo;s host IP (same order as <code>node_order</code>). Dotted = ingress check failing. <b>Route table:</b> one shared OpenShift Route to the NodePort service (tests the cluster router).<br/>
    <b>Between nodes:</b> two arrows per pair (one per direction). A rebooting node stays on the graph; red/dotted = down or unreachable (stale outbound is not shown as green). Solid green = <code>connected</code>.</p>
  </div>

  <div class="section">
    <h2>Internal Status – TCP (Pod ↔ Pod)</h2>
    <table id="internal-tcp-table"><thead><tr id="internal-tcp-head"><th>Source</th></tr></thead><tbody id="internal-tcp-body"></tbody></table>
  </div>
</div>

<div class="section" style="margin-top:20px;">
  <div class="header-row">
    <h2 style="margin:0;">Disconnection History</h2>
    <button id="btn-clear" class="btn" title="Clear history">Clear</button>
  </div>
  <pre id="history-log">No disconnections yet.</pre>
</div>

<script>
const get=(o,p,d=null)=>{try{return p.split('.').reduce((a,k)=>(a&&a[k]!==undefined)?a[k]:undefined,o)??d}catch(_){return d}};
function cell(s,t){let c='status-unknown';if(s==='connected')c='status-ok';else if(s==='error')c='status-error';return `<td class="${c}">${t}</td>`;}
function bases(ext, needle){const b=new Set();Object.keys(ext||{}).forEach(k=>{if(k.includes(needle))b.add(k.replace(' (HTTP)','').replace(' (WS)','').replace(' (TCP)',''));});return [...b].sort();}

function renderMetalLBMatrix(ext){const tb=document.getElementById('metallb-matrix-body');if(!tb)return;const rows=bases(ext,'-lb');let h='';rows.forEach(base=>{const httpS=get(ext,base+' (HTTP).status','unknown');const wsS=get(ext,base+' (WS).status','unknown');const tcpS=get(ext,base+' (TCP).status','unknown');h+='<tr>';h+=`<td><b>${base}</b></td>`;h+=cell(httpS,httpS)+cell(wsS,wsS)+cell(tcpS,tcpS);h+='</tr>';});tb.innerHTML=h||'<tr><td>No data</td></tr>';}
function renderExternalMatrix(ext){const tb=document.getElementById('external-matrix-body');if(!tb)return;const rows=bases(ext,'-np');let h='';rows.forEach(base=>{const httpS=get(ext,base+' (HTTP).status','unknown');const wsS=get(ext,base+' (WS).status','unknown');const tcpS=get(ext,base+' (TCP).status','unknown');h+='<tr>';h+=`<td><b>${base}</b></td>`;h+=cell(httpS,httpS)+cell(wsS,wsS)+cell(tcpS,tcpS);h+='</tr>';});tb.innerHTML=h||'<tr><td>No data</td></tr>';}
function renderRouteProbe(ext,routeProbe){const tb=document.getElementById('route-probe-matrix-body');if(!tb)return;if(!routeProbe){tb.innerHTML='<tr><td colspan="3"><i>Set ROUTE_PROBE_URL (deploy with APPLY_ROUTE_PROBE=yes)</i></td></tr>';return;}const k='route-probe (HTTP)';const pingS=get(ext,k+'.status','unknown');const host=routeProbe.replace(/^https?:\\/\\//,'').replace(/\\/+$/,'');tb.innerHTML='<tr><td><b>Shared route</b></td>'+cell(pingS,pingS)+`<td style="text-align:left;font-weight:normal">${host}</td></tr>`;}
function buildNodeToIp(internal, ipToNode){
  const nodeToIp={};
  for(const key of Object.keys(internal||{})){
    const d=internal[key]; if(!d||d.error)continue;
    const self=d.self; if(!self)continue;
    const n=self.node_name, p=self.pod_ip; if(n&&p) nodeToIp[n]=p;
  }
  return nodeToIp;
}

function renderInternalTcp(data){
  const internal=data.internal_status||{}; const nodeOrder=data.node_order||[]; const ipToNode=data.ip_to_node||{};
  const ingressReach=(data.mgraph||{}).ingress_nodeport||[];
  const thead=document.getElementById('internal-tcp-head'); const tbody=document.getElementById('internal-tcp-body');
  if(!thead||!tbody)return;
  const nodeToIp=buildNodeToIp(internal, ipToNode);
  if(nodeOrder.length===0){thead.innerHTML='<tr><th>Source</th></tr>';tbody.innerHTML='<tr><td>No nodes (set NODE_STATUS_ENDPOINTS)</td></tr>';return;}
  let headCells='<th>Source</th>'; nodeOrder.forEach(n=>{headCells+=`<th>→ ${n}</th>`;}); thead.innerHTML='<tr>'+headCells+'</tr>';
  let bodyRows='';
  nodeOrder.forEach((srcNode,si)=>{
    const d=internal[srcNode]; const srcUp=ingressReach[si]==='connected';
    bodyRows+=`<tr><td><b>${srcNode}</b></td>`;
    if(!d||d.error||!srcUp){nodeOrder.forEach((tgtNode,tj)=>{if(tgtNode===srcNode)bodyRows+='<td class="internal-diagonal"></td>';else bodyRows+=cell('error',d&&d.error?'err':'down');}); bodyRows+='</tr>'; return;}
    const conn=d.connections||{};
    nodeOrder.forEach((tgtNode,tj)=>{
      if(tgtNode===srcNode){bodyRows+='<td class="internal-diagonal"></td>'; return;}
      if(ingressReach[tj]!=='connected'){bodyRows+=cell('error','down'); return;}
      const tgtIp=nodeToIp[tgtNode]; const entry=tgtIp?(conn[tgtIp]||{}):null; const s=entry?(entry.tcp||'unknown'):'unknown';
      bodyRows+=cell(s,s);
    });
    bodyRows+='</tr>';
  });
  tbody.innerHTML=bodyRows;
}

function renderMermaid(data){
  const nodeOrder=data.node_order||[]; const internal=data.internal_status||{}; const ipToNode=data.ip_to_node||{};
  const mgraph=data.mgraph||{};
  const ingressRoute=mgraph.ingress_nodeport||[];
  const nodeToIp=buildNodeToIp(internal, ipToNode);
  const container=document.getElementById('mermaid-container'); if(!container)return;
  if(nodeOrder.length===0){container.textContent='No nodes'; container.removeAttribute('data-processed'); return;}
  const id=(s)=>String(s).replace(/[^a-zA-Z0-9]/g,'_').replace(/^_/,'')||'n';
  const esc=(s)=>String(s).replace(/"/g,'\\"').replace(/\\|/g,' ');
  let lines=['flowchart TB'];
  lines.push('  subgraph ingress["Ingress checks"]');
  lines.push('    direction LR');
  lines.push('    bastion((Bastion))');
  lines.push('  end');
  lines.push('  subgraph mesh["Cluster TCP mesh"]');
  lines.push('    direction LR');
  nodeOrder.forEach(n=>{lines.push('    '+id(n)+'["'+esc(n)+'"]');});
  lines.push('  end');
  lines.push('  classDef mBastion fill:#e3f2fd,stroke:#1565c0,stroke-width:2px');
  lines.push('  classDef mWorker fill:#fafafa,stroke:#424242,stroke-width:1px');
  lines.push('  class bastion mBastion');
  if(nodeOrder.length) lines.push('  class '+nodeOrder.map(n=>id(n)).join(',')+' mWorker');
  const linkOk=[];
  const nodeIds=nodeOrder.map(n=>id(n));
  for(let i=0;i<nodeIds.length-1;i++){lines.push('  '+nodeIds[i]+' ~~~ '+nodeIds[i+1]); linkOk.push(null);}
  nodeOrder.forEach((n,i)=>{
    const ok=(ingressRoute[i]==='connected');
    const lab=ok?'ingress OK':'ingress fail';
    if(ok) lines.push('  bastion-->|"'+lab+'"|'+id(n));
    else lines.push('  bastion-.->|"'+lab+'"|'+id(n));
    linkOk.push(ok);
  });
  for(let i=0;i<nodeOrder.length;i++){
    const srcUp=ingressRoute[i]==='connected';
    for(let j=0;j<nodeOrder.length;j++){
      if(i===j) continue;
      const tgtUp=ingressRoute[j]==='connected';
      const src=nodeOrder[i], tgt=nodeOrder[j];
      const d=internal[src]; const conn=(d&&!d.error&&srcUp&&d.connections)||{};
      const tgtIp=nodeToIp[tgt];
      const s=(srcUp&&tgtUp&&tgtIp)?(conn[tgtIp]&&conn[tgtIp].tcp)||'unknown':'unknown';
      const ok=(s==='connected');
      const lab='TCP '+(i+1)+'→'+(j+1)+(ok?'':' ×');
      if(ok) lines.push('  '+id(src)+'-->|"'+esc(lab)+'"|'+id(tgt));
      else lines.push('  '+id(src)+'-.->|"'+esc(lab)+'"|'+id(tgt));
      linkOk.push(ok);
    }
  }
  linkOk.forEach((ok,idx)=>{
    if(ok===null) return;
    if(ok) lines.push('  linkStyle '+idx+' stroke:#1e7e34,stroke-width:2px');
    else lines.push('  linkStyle '+idx+' stroke:#d70000,stroke-width:2px');
  });
  const diagram=lines.join(String.fromCharCode(10));
  container.innerHTML=''; container.textContent=diagram;
  container.removeAttribute('data-processed');
  if(window.mermaid){try{mermaid.run({nodes:[container], suppressErrors:true});}catch(e){}}
}

function renderHistory(hist){
  const el=document.getElementById('history-log');
  if(!el)return; if(!hist||hist.length===0){el.textContent='No disconnections yet.';return;}
  let h=''; for(const ev of hist){
    const label=ev.label||ev.name||'unknown';
    const range=ev.time_range||'';
    h+=`<div class="history-item"><b>${label}</b>\\n`+
       `  <span class="hist-down">down</span> <b>${ev.duration_sec}s</b>${range?` &nbsp;(${range})`:''}\\n`+
       `</div>`;
  }
  el.innerHTML=h;
}

async function loop(){
  try{
    const r=await fetch('/api/data'); const data=await r.json();
    document.getElementById('timestamp').innerText='Last Updated: '+new Date().toISOString();
    renderMetalLBMatrix(data.external_tests);
    renderExternalMatrix(data.external_tests);
    renderRouteProbe(data.external_tests, data.route_probe_url);
    renderInternalTcp(data);
    renderMermaid(data);
    renderHistory(data.history);
  }catch(e){
    document.getElementById('timestamp').innerText='Error fetching data: '+e;
  }
}

async function clearHistory(){
  const b=document.getElementById('btn-clear');
  b.disabled=true; b.textContent='Clearing…';
  try{
    const r=await fetch('/api/clear_history',{method:'POST'});
    if(!r.ok) throw new Error('HTTP '+r.status);
    await loop();
  }catch(e){
    alert('Failed to clear history: '+e);
  }finally{
    b.disabled=false; b.textContent='Clear';
  }
}

document.addEventListener('DOMContentLoaded',()=>{
  const b=document.getElementById('btn-clear'); if(b) b.addEventListener('click', clearHistory);
});
loop(); setInterval(loop,1000);
</script>
</body>
</html>
"""

# ---------- API helpers ----------
def _build_node_order_and_ip_map():
    """Stable node list aligned with NODE_STATUS_ENDPOINTS / ingress-{i} probes (includes nodes while down)."""
    ip_to_node = {}
    node_order: list[str] = []
    seen: set[str] = set()

    # Primary order: same indices as ingress-0..n and deploy.sh output (sorted node names at deploy time)
    for i, url in enumerate(NODE_STATUS_ENDPOINTS):
        node = URL_NODE_KEYS.get(url)
        if not node:
            data = STATE["internal_status"].get(url)
            if isinstance(data, dict) and data.get("self", {}).get("node_name"):
                node = data["self"]["node_name"]
        if not node:
            node = f"node-{i + 1}"
        if node in seen:
            continue
        node_order.append(node)
        seen.add(node)

    for _key, data in STATE["internal_status"].items():
        if _key.startswith("http://") or _key.startswith("https://"):
            continue
        if not isinstance(data, dict):
            continue
        node_name = _key if "error" in data else (data.get("self") or {}).get("node_name") or _key
        if node_name and node_name not in seen:
            node_order.append(node_name)
            seen.add(node_name)
        if "error" in data:
            continue
        self_info = data.get("self") or {}
        pod_ip = self_info.get("pod_ip")
        if pod_ip and node_name:
            ip_to_node[pod_ip] = node_name

    return node_order, ip_to_node


def _build_mgraph(node_order: list) -> dict:
    """ingress-{i} matches NODE_STATUS_ENDPOINTS index; node_order[i] is the node for that index when aligned."""
    ext = STATE["external_tests"]
    n = len(NODE_STATUS_ENDPOINTS) if NODE_STATUS_ENDPOINTS else len(node_order)
    ingress_nodeport = []
    for i in range(n):
        st = ext.get(f"ingress-{i} (HTTP)", {}).get("status", "unknown")
        ingress_nodeport.append(st or "unknown")
    return {"ingress_nodeport": ingress_nodeport}


# ---------- Routes ----------
async def handle_html(_): return web.Response(text=HTML_PAGE, content_type="text/html")

async def handle_api(_):
    """Return STATE plus computed node_order and ip_to_node for dynamic dashboard."""
    node_order, ip_to_node = _build_node_order_and_ip_map()
    payload = {
        **STATE,
        "history": _history_for_api(ip_to_node),
        "node_order": node_order,
        "ip_to_node": ip_to_node,
        "mgraph": _build_mgraph(node_order),
        "route_probe_url": ROUTE_PROBE_URL or None,
    }
    return web.json_response(payload)

async def handle_health(_): return web.Response(text="ok", content_type="text/plain")

async def _fanout_clear_to_peers():
    """POST /admin/clear_history to every node endpoint (NODE_STATUS_ENDPOINTS)."""
    urls = NODE_STATUS_ENDPOINTS if NODE_STATUS_ENDPOINTS else ROUTE_PEERS
    timeout = aiohttp.ClientTimeout(total=2.0)
    connector = _http_connector()
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        for url in urls:
            try:
                async with session.post(f"{url.rstrip('/')}/admin/clear_history") as resp:
                    await resp.text()
                    logging.info(f"[CLEAR] peer ok: {url}")
            except Exception as e:
                logging.warning(f"[CLEAR] peer failed: {url} -> {e}")

async def handle_clear_history(_):
    global HISTORY_IGNORE_BEFORE
    # 1) set cutoff and clear local memory
    HISTORY_IGNORE_BEFORE = now_iso()
    STATE["history"].clear()
    NODE_IP_HISTORY.clear()
    # 2) fan-out clear to peers (don't block response)
    asyncio.create_task(_fanout_clear_to_peers())
    return web.Response(text="history cleared", content_type="text/plain")

# ---------- Probe supervisor ----------
async def run_probes():
    tasks = []
    # Per-node status (primary): one poll task per NODE_STATUS_ENDPOINTS URL
    for i, url in enumerate(NODE_STATUS_ENDPOINTS):
        tasks.append(asyncio.create_task(poll_node_status_task(url)))
        tasks.append(asyncio.create_task(http_client_task(f"ingress-{i}", url, log_history=False)))
    # Legacy: ROUTE_PEERS (poll by name)
    for i, url in enumerate(ROUTE_PEERS, 1):
        rname = f"peer-{i}-route"
        tasks += [asyncio.create_task(http_client_task(rname, url)),
                  asyncio.create_task(poll_peer_status_task(rname, url))]
    # Optional: MetalLB / NodePort external tests
    for name, ip in METALLB_PEERS.items():
        tasks += [asyncio.create_task(ws_client_task(name, ip, 8080)),
                  asyncio.create_task(tcp_client_task(name, ip, 8081)),
                  asyncio.create_task(http_client_task(name, f"http://{ip}:8082"))]
    for name, cfg in NODEPORT_PEERS.items():
        tasks += [asyncio.create_task(ws_client_task(name, cfg["host"], cfg["ws_port"])),
                  asyncio.create_task(tcp_client_task(name, cfg["host"], cfg["tcp_port"])),
                  asyncio.create_task(http_client_task(name, f"http://{cfg['host']}:{cfg['http_port']}"))]
    if ROUTE_PROBE_URL:
        tasks.append(asyncio.create_task(http_client_task("route-probe", ROUTE_PROBE_URL)))
    await asyncio.gather(*tasks)

# ---------- Main ----------
async def main():
    app = web.Application()
    app.router.add_get("/", handle_html)
    app.router.add_get("/api/data", handle_api)
    app.router.add_get("/healthz", handle_health)
    app.router.add_post("/api/clear_history", handle_clear_history)

    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", DASHBOARD_PORT); await site.start()
    logging.info(f"Dashboard server started at http://0.0.0.0:{DASHBOARD_PORT}")

    asyncio.create_task(run_probes())
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except OSError as e:
        logging.error(f"Failed to bind port {DASHBOARD_PORT}: {e}")
    except KeyboardInterrupt:
        logging.info("Dashboard shutting down.")

