# app.py — peer: TCP server + clients (mesh), HTTP server for /status, /history, /ping, /admin/clear, WebSocket :8080 for bastion probes
import asyncio
import socket
import logging
import os
from datetime import datetime, timezone
import json

import websockets

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')

# ---------- Config ----------
WS_PORT = 8080
TCP_PORT = 8081
HTTP_PORT = 8082

PEER_SERVICE = os.getenv("PEER_SERVICE", "migration-peer-svc")
NAMESPACE = os.getenv("NAMESPACE", "migration-test-system")
POD_IP = os.getenv("POD_IP", "")
POD_NAME = os.getenv("HOSTNAME", socket.gethostname())
NODE_NAME = os.getenv("NODE_NAME", "")

CHECK_INTERVAL = float(os.getenv("CHECK_INTERVAL", "10.0"))
RECONNECT_DELAY = float(os.getenv("RECONNECT_DELAY", "1.0"))
PEER_RESOLVE_INTERVAL = float(os.getenv("PEER_RESOLVE_INTERVAL", "10.0"))
discovery_wakeup = None

MAX_HISTORY = 200

def now_iso():
    return datetime.now(timezone.utc).isoformat()

# ---------- Peer discovery (DNS) ----------
def _resolve_peer_ips_sync():
    fqdn = f"{PEER_SERVICE}.{NAMESPACE}.svc.cluster.local"
    try:
        infos = socket.getaddrinfo(fqdn, None, socket.AF_INET)
        return [info[4][0] for info in infos]
    except Exception as e:
        logging.warning(f"DNS resolve failed for {fqdn}: {e}")
        return []

async def resolve_peer_ips():
    loop = asyncio.get_event_loop()
    all_ips = await loop.run_in_executor(None, _resolve_peer_ips_sync)
    peers = [ip for ip in all_ips if ip != POD_IP]
    return peers

# ---------- State ----------
# connection_state keyed by peer IP; value: {"tcp": "connected"|"disconnected"|"unknown", "last_change": "<iso>"}
connection_state = {}
# peer_tasks: peer_ip -> asyncio.Task (tcp_client_task)
peer_tasks = {}
# peer_ip -> node_name (from peer /status), best-effort
peer_nodes = {}
# peer_ip -> disconnect count (flaps after a successful connect)
disconnect_totals = {}
HISTORY = []


def _prom_escape(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _dst_label(peer_ip: str) -> str:
    return peer_nodes.get(peer_ip) or peer_ip


def render_metrics() -> str:
    """Prometheus text exposition (no extra dependency; airgap-friendly)."""
    lines = [
        "# HELP migration_peer_info Identity of this peer (always 1).",
        "# TYPE migration_peer_info gauge",
        (
            f'migration_peer_info{{node="{_prom_escape(NODE_NAME)}",'
            f'pod="{_prom_escape(POD_NAME)}",pod_ip="{_prom_escape(POD_IP)}"}} 1'
        ),
        "# HELP migration_peer_tcp_up 1 if TCP mesh client to peer is connected.",
        "# TYPE migration_peer_tcp_up gauge",
        "# HELP migration_peer_tcp_disconnects_total TCP disconnects after a successful connect.",
        "# TYPE migration_peer_tcp_disconnects_total counter",
        "# HELP migration_peer_edge Edge sample for Grafana Node graph "
        "(value=up; mainstat/secondarystat labels for panel mapping).",
        "# TYPE migration_peer_edge gauge",
    ]
    src = _prom_escape(NODE_NAME or POD_NAME or POD_IP or "unknown")
    for peer_ip, st in sorted(connection_state.items()):
        dst = _prom_escape(_dst_label(peer_ip))
        up = 1 if st.get("tcp") == "connected" else 0
        disc = int(disconnect_totals.get(peer_ip, 0))
        ip_l = _prom_escape(peer_ip)
        lines.append(
            f'migration_peer_tcp_up{{src_node="{src}",dst_node="{dst}",dst_ip="{ip_l}"}} {up}'
        )
        lines.append(
            f'migration_peer_tcp_disconnects_total{{src_node="{src}",dst_node="{dst}",dst_ip="{ip_l}"}} {disc}'
        )
        edge_id = _prom_escape(f"{NODE_NAME or POD_NAME}-{_dst_label(peer_ip)}")
        lines.append(
            f'migration_peer_edge{{id="{edge_id}",source="{src}",target="{dst}",'
            f'mainstat="{up}",secondarystat="{disc}"}} {up}'
        )
    lines.append("")
    return "\n".join(lines)


async def fetch_peer_node_name(peer_ip: str) -> None:
    """Best-effort: learn peer node name via HTTP /status for nicer metric labels."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(peer_ip, HTTP_PORT), timeout=2.0
        )
        try:
            writer.write(b"GET /status HTTP/1.0\r\nHost: peer\r\n\r\n")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(65536), timeout=2.0)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        text = data.decode(errors="ignore")
        body = text.split("\r\n\r\n", 1)[-1]
        payload = json.loads(body)
        node = (payload.get("self") or {}).get("node_name") or ""
        if node:
            peer_nodes[peer_ip] = node
    except Exception:
        pass

# ---------- Servers ----------
async def handle_tcp(reader, writer):
    try:
        while True:
            data = await reader.readline()
            if not data:
                break
            writer.write(data)
            await writer.drain()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

async def ws_handler(websocket):
    """Minimal handler; protocol ping/pong is answered by the library (bastion uses ws.ping())."""
    try:
        async for _ in websocket:
            pass
    except Exception:
        pass


async def ws_serve_forever():
    async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
        await asyncio.Future()


async def handle_http(reader, writer):
    try:
        data = await reader.read(4096)
        line0 = data.decode(errors="ignore").splitlines()[0] if data else ""
        method, path = ("GET", "/") if not line0 else (line0.split()[0], line0.split()[1])
        body = ""

        path = path.split("?", 1)[0]
        content_type = "application/json"

        if method == "POST" and path == "/admin/clear_history":
            HISTORY.clear()
            body = json.dumps({"ok": True, "pod": POD_NAME, "ts": now_iso()})
        elif method == "GET" and path == "/metrics":
            body = render_metrics()
            content_type = "text/plain; version=0.0.4; charset=utf-8"
        elif method == "GET" and path == "/status":
            status_body = {
                "self": {
                    "pod_name": POD_NAME,
                    "pod_ip": POD_IP,
                    "node_name": NODE_NAME,
                },
                "timestamp": now_iso(),
                "connections": connection_state,
                "peer_nodes": dict(peer_nodes),
                "disconnect_totals": dict(disconnect_totals),
            }
            body = json.dumps(status_body)
        elif method == "GET" and path == "/history":
            body = json.dumps(HISTORY[-MAX_HISTORY:])
        else:
            body = json.dumps({"status": "ok", "pod": POD_NAME, "ts": now_iso()})

        payload = body.encode()
        resp = (
            "HTTP/1.1 200 OK\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(payload)}\r\n\r\n"
        ).encode() + payload
        writer.write(resp)
        await writer.drain()
    finally:
        try:
            writer.close()
        except Exception:
            pass

# ---------- History helper ----------
def record_outage(target: str, proto: str, start_time_str: str):
    try:
        start = datetime.fromisoformat(start_time_str)
        end = datetime.now(timezone.utc)
        dur = (end - start).total_seconds()
        HISTORY.insert(
            0,
            {
                "name": target,
                "protocol": proto,
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "duration_sec": round(dur, 2),
                "source": "pod",
                "reporter": POD_NAME,
                "reporter_node": NODE_NAME,
            },
        )
        del HISTORY[MAX_HISTORY:]
        logging.info(f"--- OUTAGE ENDED: {target} ({proto}) {dur:.2f}s ---")
    except Exception as e:
        logging.warning(f"[HISTORY] Failed to record outage: {e}")

# ---------- TCP client (one per peer) ----------
async def tcp_client_task(peer_ip: str):
    while True:
        start_time = now_iso()
        writer = None
        try:
            reader, writer = await asyncio.open_connection(peer_ip, TCP_PORT)
            logging.info(f"[TCP Client] Connected to {peer_ip}")
            connection_state[peer_ip]["tcp"] = "connected"
            connection_state[peer_ip]["last_change"] = now_iso()
            if peer_ip not in peer_nodes:
                asyncio.create_task(fetch_peer_node_name(peer_ip))
            while True:
                line = f"ping from {POD_NAME} at {now_iso()}\n".encode()
                writer.write(line)
                await writer.drain()
                echo = await asyncio.wait_for(reader.readline(), timeout=CHECK_INTERVAL + 0.3)
                if echo != line:
                    raise RuntimeError("TCP echo mismatch")
                await asyncio.sleep(CHECK_INTERVAL)
        except asyncio.CancelledError:
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
            raise
        except Exception as e:
            if peer_ip in connection_state and connection_state[peer_ip]["tcp"] != "disconnected":
                was = connection_state[peer_ip]["tcp"]
                logging.info(f"[TCP Client] Disconnected from {peer_ip}: {e}")
                connection_state[peer_ip]["tcp"] = "disconnected"
                connection_state[peer_ip]["last_change"] = now_iso()
                if was == "connected":
                    disconnect_totals[peer_ip] = disconnect_totals.get(peer_ip, 0) + 1
                record_outage(peer_ip, "TCP", start_time)
                nudge_peer_discovery()
            try:
                if writer:
                    writer.close()
                    await writer.wait_closed()
            except Exception:
                pass
            await asyncio.sleep(RECONNECT_DELAY)


def nudge_peer_discovery():
    if discovery_wakeup is not None:
        discovery_wakeup.set()


# ---------- Peer discovery loop: re-resolve DNS and add/remove client tasks ----------
async def sync_peers_from_dns():
    global connection_state, peer_tasks
    new_peers = await resolve_peer_ips()
    new_set = set(new_peers)
    current_set = set(connection_state.keys())

    removed = current_set - new_set
    for ip in removed:
        if ip in peer_tasks:
            peer_tasks[ip].cancel()
            try:
                await peer_tasks[ip]
            except asyncio.CancelledError:
                pass
            del peer_tasks[ip]
        connection_state.pop(ip, None)
        peer_nodes.pop(ip, None)
        # keep disconnect_totals for historical counters across brief DNS flaps

    added = new_set - current_set
    for ip in added:
        connection_state[ip] = {"tcp": "unknown", "last_change": now_iso()}
        peer_tasks[ip] = asyncio.create_task(tcp_client_task(ip))
        asyncio.create_task(fetch_peer_node_name(ip))


async def peer_discovery_loop():
    global discovery_wakeup
    discovery_wakeup = asyncio.Event()
    while True:
        try:
            await sync_peers_from_dns()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning(f"[Discovery] Error: {e}")
        try:
            await asyncio.wait_for(discovery_wakeup.wait(), timeout=PEER_RESOLVE_INTERVAL)
        except asyncio.TimeoutError:
            pass
        discovery_wakeup.clear()

# ---------- Main ----------
async def main():
    # Initial resolve and start TCP clients
    initial_peers = await resolve_peer_ips()
    for ip in initial_peers:
        connection_state[ip] = {"tcp": "unknown", "last_change": now_iso()}
        peer_tasks[ip] = asyncio.create_task(tcp_client_task(ip))

    tcp_srv = asyncio.start_server(handle_tcp, "0.0.0.0", TCP_PORT)
    http_srv = asyncio.start_server(handle_http, "0.0.0.0", HTTP_PORT)
    discovery_task = asyncio.create_task(peer_discovery_loop())
    ws_task = asyncio.create_task(ws_serve_forever())

    await asyncio.gather(tcp_srv, http_srv, discovery_task, ws_task)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Peer app shutting down.")
