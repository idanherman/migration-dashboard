# app.py — peer: TCP server + clients (mesh), HTTP server for /status, /history, /ping, /admin/clear
import asyncio
import socket
import logging
import os
from datetime import datetime, timezone
import json

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')

# ---------- Config ----------
TCP_PORT = 8081
HTTP_PORT = 8082

PEER_SERVICE = os.getenv("PEER_SERVICE", "migration-peer-svc")
NAMESPACE = os.getenv("NAMESPACE", "migration-test-system")
POD_IP = os.getenv("POD_IP", "")
POD_NAME = os.getenv("HOSTNAME", socket.gethostname())
NODE_NAME = os.getenv("NODE_NAME", "")

CHECK_INTERVAL = float(os.getenv("CHECK_INTERVAL", "10.0"))
RECONNECT_DELAY = float(os.getenv("RECONNECT_DELAY", "1.0"))
PEER_RESOLVE_INTERVAL = float(os.getenv("PEER_RESOLVE_INTERVAL", "60.0"))

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
HISTORY = []

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

async def handle_http(reader, writer):
    try:
        data = await reader.read(4096)
        line0 = data.decode(errors="ignore").splitlines()[0] if data else ""
        method, path = ("GET", "/") if not line0 else (line0.split()[0], line0.split()[1])
        body = ""

        if method == "POST" and path == "/admin/clear_history":
            HISTORY.clear()
            body = json.dumps({"ok": True, "pod": POD_NAME, "ts": now_iso()})
        elif method == "GET" and path == "/status":
            status_body = {
                "self": {
                    "pod_name": POD_NAME,
                    "pod_ip": POD_IP,
                    "node_name": NODE_NAME,
                },
                "timestamp": now_iso(),
                "connections": connection_state,
            }
            body = json.dumps(status_body)
        elif method == "GET" and path == "/history":
            body = json.dumps(HISTORY[-MAX_HISTORY:])
        else:
            body = json.dumps({"status": "ok", "pod": POD_NAME, "ts": now_iso()})

        resp = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n{body}"
        )
        writer.write(resp.encode())
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
                logging.info(f"[TCP Client] Disconnected from {peer_ip}: {e}")
                connection_state[peer_ip]["tcp"] = "disconnected"
                connection_state[peer_ip]["last_change"] = now_iso()
                record_outage(peer_ip, "TCP", start_time)
            try:
                if writer:
                    writer.close()
                    await writer.wait_closed()
            except Exception:
                pass
            await asyncio.sleep(RECONNECT_DELAY)

# ---------- Peer discovery loop: re-resolve DNS and add/remove client tasks ----------
async def peer_discovery_loop():
    global connection_state, peer_tasks
    while True:
        try:
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

            added = new_set - current_set
            for ip in added:
                connection_state[ip] = {"tcp": "unknown", "last_change": now_iso()}
                peer_tasks[ip] = asyncio.create_task(tcp_client_task(ip))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning(f"[Discovery] Error: {e}")
        await asyncio.sleep(PEER_RESOLVE_INTERVAL)

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

    await asyncio.gather(tcp_srv, http_srv, discovery_task)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Peer app shutting down.")
