import asyncio
import aiohttp
import websockets
import json
import logging
from datetime import datetime, timezone
from aiohttp import web

# --- CONFIGURATION: SET YOUR ENDPOINTS HERE ---

# 1. MetalLB IPs (for WS and TCP tests)
#    (Get these from 'oc get svc')
METALLB_PEERS = {
    "peer-1-lb": "172.17.95.200", # <-- EXAMPLE: REPLACE
    "peer-2-lb": "172.17.95.201", # <-- EXAMPLE: REPLACE
    "peer-3-lb": "172.17.95.202", # <-- EXAMPLE: REPLACE
}

# 2. HTTP Routes (for HTTP tests AND fetching /status)
#    (Get these from 'oc get route')
ROUTE_PEERS = [
    "http://peer-1-route.migration-test-system.apps.ocp.lab", # <-- EXAMPLE: REPLACE
    "http://peer-2-route.migration-test-system.apps.ocp.lab", # <-- EXAMPLE: REPLACE
    "http://peer-3-route.migration-test-system.apps.ocp.lab", # <-- EXAMPLE: REPLACE
]

# 3. Test Intervals
HTTP_INTERVAL = 1.0  # Test HTTP Route
WS_INTERVAL = 0.5    # Test WS MetalLB
TCP_INTERVAL = 0.5    # Test TCP MetalLB
POLL_INTERVAL = 1.0  # Fetch /status from peers
RECONNECT_DELAY = 1.0

# -------------------------------------------------

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')

# Global state dictionaries to hold all data
STATE = {
    "external_tests": {}, # Status of bastion -> cluster
    "internal_status": {}  # Status of pod <-> pod
}

def now_iso():
    return datetime.now(timezone.utc).isoformat()

# --- STATE MANAGEMENT ---
def set_state(test_type, name, status, error=""):
    STATE[test_type][name] = {
        "status": status,
        "error": str(error),
        "last_update": now_iso()
    }
    if status == "error":
        logging.warning(f"[{test_type.upper()}] {name} -> {status}: {error}")

# --- EXTERNAL CLIENTS (Bastion -> Cluster) ---

async def http_client_task(name, base_url):
    """Tests HTTP GET /ping via Route"""
    url = f"{base_url}/ping"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url, timeout=1.0) as resp:
                    resp.raise_for_status()
                    set_state("external_tests", f"{name} (HTTP)", "connected")
            except Exception as e:
                set_state("external_tests", f"{name} (HTTP)", "error", e)
            await asyncio.sleep(HTTP_INTERVAL)

async def ws_client_task(name, host, port=8080):
    """Tests WebSocket connection via MetalLB"""
    uri = f"ws://{host}:{port}"
    while True:
        try:
            async with websockets.connect(uri, open_timeout=1.0) as ws:
                set_state("external_tests", f"{name} (WS)", "connected")
                while True:
                    await ws.send(f"ping from bastion {now_iso()}")
                    await asyncio.sleep(WS_INTERVAL)
        except Exception as e:
            set_state("external_tests", f"{name} (WS)", "error", e)
            await asyncio.sleep(RECONNECT_DELAY)

async def tcp_client_task(name, host, port=8081):
    """Tests TCP connection via MetalLB"""
    writer = None  # <-- 1. Initialize writer to None
    while True:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=1.0
            )
            set_state("external_tests", f"{name} (TCP)", "connected")
            while True:
                writer.write(f"ping from bastion {now_iso()}\n".encode())
                await writer.drain()
                await asyncio.sleep(TCP_INTERVAL)
        except Exception as e:
            set_state("external_tests", f"{name} (TCP)", "error", e)
            if writer:  # <-- 2. Check if writer exists
                try:
                    writer.close()
                except:
                    pass  # Ignore errors on close
            writer = None  # <-- 3. Reset writer
            await asyncio.sleep(RECONNECT_DELAY)

# --- INTERNAL POLLER (Polls peer /status) ---

async def poll_peer_status_task(name, base_url):
    """Fetches /status from a peer via its HTTP Route"""
    url = f"{base_url}/status"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url, timeout=1.0) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    STATE["internal_status"][name] = data
            except Exception as e:
                STATE["internal_status"][name] = {"error": str(e), "url": url}
            await asyncio.sleep(POLL_INTERVAL)

# --- WEB SERVER ---

async def handle_html(request):
    """Serves the main HTML dashboard page"""
    html_content = """
    <html>
    <head>
        <title>OVN Migration Dashboard</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; 
                   background-color: #f4f4f4; margin: 20px; }
            h1, h2 { color: #333; border-bottom: 2px solid #ccc; padding-bottom: 5px; }
            pre { background-color: #fff; border: 1px solid #ddd; padding: 10px; 
                  border-radius: 5px; white-space: pre-wrap; word-wrap: break-word; }
            #container { display: flex; flex-wrap: wrap; gap: 20px; }
            .section { flex: 1; min-width: 400px; background: #fff; 
                       border: 1px solid #ddd; border-radius: 5px; padding: 15px; }
            .status-ok { color: green; }
            .status-error { color: red; font-weight: bold; }
        </style>
    </head>
    <body>
        <h1>Live Migration Dashboard</h1>
        <div id="timestamp">Loading...</div>
        
        <div id="container">
            <div class="section">
                <h2>External Tests (Bastion &rarr; Cluster)</h2>
                <pre id="external-status">Loading...</pre>
            </div>
            <div class="section">
                <h2>Internal Status (Pod &harr; Pod)</h2>
                <pre id="internal-status">Loading...</pre>
            </div>
        </div>

        <script>
            function formatStatus(data) {
                let html = "";
                for (const [key, value] of Object.entries(data)) {
                    const statusClass = value.status === 'connected' ? 'status-ok' : 'status-error';
                    html += `<b>${key}</b>: <span class="${statusClass}">${value.status}</span>`;
                    if (value.status === 'error') {
                        html += ` (${value.error})`;
                    }
                    html += `\\n  (Last Update: ${value.last_update})\\n\\n`;
                }
                return html;
            }

            async function fetchData() {
                try {
                    const response = await fetch('/api/data');
                    const data = await response.json();
                    
                    document.getElementById('timestamp').innerText = "Last Updated: " + new Date().toISOString();
                    
                    // Format external status
                    document.getElementById('external-status').textContent = 
                        formatStatus(data.external_tests);
                    
                    // Format internal status (just pretty-print JSON)
                    document.getElementById('internal-status').textContent = 
                        JSON.stringify(data.internal_status, null, 2);
                    
                } catch (e) {
                    document.getElementById('timestamp').innerText = "Error fetching data: " + e;
                }
            }
            
            fetchData();
            setInterval(fetchData, 1000); // Auto-refresh every second
        </script>
    </body>
    </html>
    """
    return web.Response(text=html_content, content_type='text/html')

async def handle_api(request):
    """Serves the raw status data as JSON"""
    return web.json_response(STATE)

async def main():
    # 1. Create list of all client tasks
    tasks = []
    
    # Add MetalLB tasks
    for name, ip in METALLB_PEERS.items():
        tasks.append(ws_client_task(name, ip))
        tasks.append(tcp_client_task(name, ip))

    # Add Route tasks (HTTP test + /status poller)
    for i, url in enumerate(ROUTE_PEERS, 1):
        name = f"peer-{i}-route"
        tasks.append(http_client_task(name, url))
        tasks.append(poll_peer_status_task(name, url))

    # 2. Create the web server
    app = web.Application()
    app.router.add_get("/", handle_html)
    app.router.add_get("/api/data", handle_api)
    
    runner = web.AppRunner(app)
    await runner.setup()
    # Serve on port 8080 inside the container
    site = web.TCPSite(runner, '0.0.0.0', 9091) 
    await site.start()
    logging.info("Dashboard server started at http://0.0.0.0:9091")

    # 3. Run all tasks concurrently
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Dashboard shutting down.")
