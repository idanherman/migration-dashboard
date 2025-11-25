# bastion-client.py — web-first dashboard, background probes, per-protocol internal tables, Clear History button
import asyncio
import aiohttp
import websockets
import logging
import socket
import os
import json
from datetime import datetime, timezone
from aiohttp import web

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
    
    # Route peers (comma-separated URLs)
    route_str = os.getenv("ROUTE_PEERS", "http://peer-1-route-migration-test-system.apps.ocp.lab,http://peer-2-route-migration-test-system.apps.ocp.lab,http://peer-3-route-migration-test-system.apps.ocp.lab")
    config["ROUTE_PEERS"] = [url.strip() for url in route_str.split(",") if url.strip()]
    
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
    
    return config

CONFIG = load_config()
METALLB_PEERS = CONFIG["METALLB_PEERS"]
NODEPORT_PEERS = CONFIG["NODEPORT_PEERS"]
ROUTE_PEERS = CONFIG["ROUTE_PEERS"]
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

now_iso = lambda: datetime.now(timezone.utc).isoformat()

STATE = {
    "external_tests": {},   # { "<name> (HTTP|TCP|WS)": {status, error, last_update} }
    "internal_status": {},  # { "peer-1-route": {.../status json...}, ... }
    "history": []           # merged outages (bastion + peers)
}

# cutoff to ignore peer events older than the last "clear"
HISTORY_IGNORE_BEFORE = None  # ISO string or None

# ---------- Helpers ----------
def set_state(section: str, name: str, status: str, error: Exception | str = ""):
    STATE[section][name] = {"status": status, "error": "" if not error else str(error), "last_update": now_iso()}
    if status == "error":
        logging.warning(f"[{section.upper()}] {name} -> error: {error}")

def log_disconnection_event(name: str, proto: str, start_time_str: str, *, source: str = "bastion"):
    try:
        start = datetime.fromisoformat(start_time_str)
        end = datetime.now(timezone.utc)
        dur = (end - start).total_seconds()
        STATE["history"].insert(0, {
            "name": name, "protocol": proto,
            "start_time": start.isoformat(), "end_time": end.isoformat(),
            "duration_sec": round(dur, 2), "source": source
        })
        del STATE["history"][MAX_HISTORY:]
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
async def http_client_task(name: str, base_url: str, ping_path="/ping"):
    url, label = f"{base_url}{ping_path}", f"{name} (HTTP)"
    conn = {"status": "unknown", "since": now_iso()}
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    if conn["status"] == "error":
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

async def poll_peer_status_task(name: str, base_url: str):
    status_url, history_url = f"{base_url}/status", f"{base_url}/history"
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            # /status
            try:
                async with session.get(status_url) as resp:
                    resp.raise_for_status()
                    STATE["internal_status"][name] = await resp.json()
            except Exception as e:
                STATE["internal_status"][name] = {"error": str(e), "url": status_url}
            # /history
            try:
                async with session.get(history_url) as resp:
                    resp.raise_for_status()
                    peer_hist = await resp.json()
                    # mark source and filter by cutoff
                    filtered = []
                    for ev in peer_hist:
                        ev.setdefault("source", "pod")
                        if _is_after_cutoff(ev):
                            filtered.append(ev)
                    if filtered:
                        known = {
                            (h.get("name"), h.get("protocol"), h.get("start_time"),
                             h.get("end_time"), h.get("source"), h.get("reporter"))
                            for h in STATE["history"]
                        }
                        new_items = []
                        for ev in filtered:
                            key = (ev.get("name"), ev.get("protocol"), ev.get("start_time"),
                                   ev.get("end_time"), ev.get("source"), ev.get("reporter"))
                            if key not in known:
                                new_items.append(ev)
                        if new_items:
                            STATE["history"][0:0] = new_items
                            STATE["history"].sort(key=lambda h: h.get("end_time") or "", reverse=True)
                            del STATE["history"][MAX_HISTORY:]
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
pre{background:#fff;border:1px solid #ddd;padding:15px;border-radius:5px;white-space:pre-wrap;word-wrap:break-word;font-family:SFMono-Regular,Consolas,Menlo,monospace;font-size:13px;line-height:1.6}
.history-item{border-bottom:1px solid #eee;padding:8px 2px;margin-bottom:5px}
.history-item:last-child{border-bottom:none}
.hist-down{color:#d70000;font-weight:bold}
.btn{border:1px solid #ccc;background:#fafafa;border-radius:6px;padding:6px 10px;cursor:pointer;font-size:13px}
.btn:hover{background:#f0f0f0}
.btn:disabled{opacity:.6;cursor:not-allowed}
.header-row{display:flex;align-items:center;justify-content:space-between}
</style>
</head>
<body>
<h1>Live Migration Dashboard</h1>
<div id="timestamp">Loading...</div>

<div id="container">
  <div class="section">
    <h2>External Tests (Bastion → Cluster)</h2>
    <table id="metallb-matrix"><thead><tr><th>Target (LoadBalancer)</th><th>HTTP</th><th>WebSocket (WS)</th><th>Raw TCP</th></tr></thead><tbody id="metallb-matrix-body"></tbody></table><br/>
    <table id="external-matrix"><thead><tr><th>Target (NodePort)</th><th>HTTP</th><th>WebSocket (WS)</th><th>Raw TCP</th></tr></thead><tbody id="external-matrix-body"></tbody></table><br/>
    <table id="route-matrix"><thead><tr><th>Target (Route)</th><th>HTTP /ping</th></tr></thead><tbody id="route-matrix-body"></tbody></table>
  </div>

  <div class="section">
    <h2>Internal Status – WebSocket (Pod ↔ Pod)</h2>
    <table><thead><tr><th>Source Pod</th><th>→ peer-1-svc</th><th>→ peer-2-svc</th><th>→ peer-3-svc</th></tr></thead><tbody id="internal-ws-body"></tbody></table>
    <h2>Internal Status – TCP (Pod ↔ Pod)</h2>
    <table><thead><tr><th>Source Pod</th><th>→ peer-1-svc</th><th>→ peer-2-svc</th><th>→ peer-3-svc</th></tr></thead><tbody id="internal-tcp-body"></tbody></table>
    <h2>Internal Status – HTTP (Pod ↔ Pod)</h2>
    <table><thead><tr><th>Source Pod</th><th>→ peer-1-svc</th><th>→ peer-2-svc</th><th>→ peer-3-svc</th></tr></thead><tbody id="internal-http-body"></tbody></table>
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

function renderMetalLBMatrix(ext){const tb=document.getElementById('metallb-matrix-body');let h='';for(let i=1;i<=3;i++){const httpS=get(ext,`peer-${i}-lb (HTTP).status`,'unknown');const wsS=get(ext,`peer-${i}-lb (WS).status`,'unknown');const tcpS=get(ext,`peer-${i}-lb (TCP).status`,'unknown');h+='<tr>';h+=`<td><b>Peer-${i} (LoadBalancer)</b></td>`;h+=cell(httpS,httpS)+cell(wsS,wsS)+cell(tcpS,tcpS);h+='</tr>';}tb.innerHTML=h;}
function renderExternalMatrix(ext){const tb=document.getElementById('external-matrix-body');let h='';for(let i=1;i<=3;i++){const httpS=get(ext,`peer-${i}-np (HTTP).status`,'unknown');const wsS=get(ext,`peer-${i}-np (WS).status`,'unknown');const tcpS=get(ext,`peer-${i}-np (TCP).status`,'unknown');h+='<tr>';h+=`<td><b>Peer-${i} (NodePort)</b></td>`;h+=cell(httpS,httpS)+cell(wsS,wsS)+cell(tcpS,tcpS);h+='</tr>';}tb.innerHTML=h;}
function renderRouteMatrix(ext){const tb=document.getElementById('route-matrix-body');let h='';for(let i=1;i<=3;i++){const name=`peer-${i}-route`;const pingS=get(ext,`${name} (HTTP).status`,'unknown');h+='<tr>';h+=`<td><b>${name}</b></td>`;h+=cell(pingS,pingS);h+='</tr>';}tb.innerHTML=h;}

function renderInternalProto(internal, proto, targetId){
  const tb=document.getElementById(targetId);let h='';
  for(let i=1;i<=3;i++){
    const src=`peer-${i}-route`; const data=internal[src];
    h+='<tr>'; h+=`<td><b>From Peer-${i}</b></td>`;
    if(!data||data.error){h+=cell('error','POLL FAILED')+cell('error','POLL FAILED')+cell('error','POLL FAILED');}
    else{
      for(let j=1;j<=3;j++){
        const dest=`peer-${j}-svc`;
        const s=get(data,`connections.${dest}.${proto}`,'unknown');
        h+=cell(s,s);
      }
    }
    h+='</tr>';
  }
  tb.innerHTML=h;
}

function renderHistory(hist){
  const el=document.getElementById('history-log');
  if(!hist||hist.length===0){el.textContent='No disconnections yet.';return;}
  let h=''; for(const ev of hist){
    const src=ev.source||''; let label='';
    if(src==='bastion'){label=`Bastion → ${ev.name||'unknown'}`;}
    else if(src==='pod'&&ev.reporter){label=`${ev.reporter} → ${ev.name||'unknown'}`;}
    else{label=`${ev.name||'unknown'}`;}
    h+=`<div class="history-item"><b>[${ev.protocol}] ${label}</b>\\n`+
       `  <span class="hist-down">DISCONNECTED</span> for <b>${ev.duration_sec}s</b>\\n`+
       `  Started: ${ev.start_time}\\n`+
       `  Ended:   ${ev.end_time}\\n`+
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
    renderRouteMatrix(data.external_tests);
    renderInternalProto(data.internal_status,'ws','internal-ws-body');
    renderInternalProto(data.internal_status,'tcp','internal-tcp-body');
    renderInternalProto(data.internal_status,'http','internal-http-body');
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

# ---------- Routes ----------
async def handle_html(_): return web.Response(text=HTML_PAGE, content_type="text/html")
async def handle_api(_):  return web.json_response(STATE)
async def handle_health(_): return web.Response(text="ok", content_type="text/plain")

async def _fanout_clear_to_peers():
    timeout = aiohttp.ClientTimeout(total=2.0)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url in ROUTE_PEERS:
            try:
                async with session.post(f"{url}/admin/clear_history") as resp:
                    await resp.text()
                    logging.info(f"[CLEAR] peer ok: {url}")
            except Exception as e:
                logging.warning(f"[CLEAR] peer failed: {url} -> {e}")

async def handle_clear_history(_):
    global HISTORY_IGNORE_BEFORE
    # 1) set cutoff and clear local memory
    HISTORY_IGNORE_BEFORE = now_iso()
    STATE["history"].clear()
    # 2) fan-out clear to peers (don't block response)
    asyncio.create_task(_fanout_clear_to_peers())
    return web.Response(text="history cleared", content_type="text/plain")

# ---------- Probe supervisor ----------
async def run_probes():
    tasks = []
    for name, ip in METALLB_PEERS.items():
        tasks += [asyncio.create_task(ws_client_task(name, ip, 8080)),
                  asyncio.create_task(tcp_client_task(name, ip, 8081)),
                  asyncio.create_task(http_client_task(name, f"http://{ip}:8082"))]
    for name, cfg in NODEPORT_PEERS.items():
        tasks += [asyncio.create_task(ws_client_task(name, cfg["host"], cfg["ws_port"])),
                  asyncio.create_task(tcp_client_task(name, cfg["host"], cfg["tcp_port"])),
                  asyncio.create_task(http_client_task(name, f"http://{cfg['host']}:{cfg['http_port']}"))]
    for i, url in enumerate(ROUTE_PEERS, 1):
        rname = f"peer-{i}-route"
        tasks += [asyncio.create_task(http_client_task(rname, url)),
                  asyncio.create_task(poll_peer_status_task(rname, url))]
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

