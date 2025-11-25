# app.py — peer: WS/TCP/HTTP servers + clients + status/history + admin clear endpoint
import asyncio
import websockets
import aiohttp
import socket
import logging
import os
from datetime import datetime, timezone
import json

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')

# ---------- Config ----------
PEERS = os.getenv("PEERS", "peer-1-svc,peer-2-svc,peer-3-svc").split(',')
WS_PORT = 8080
TCP_PORT = 8081
HTTP_PORT = 8082

WS_INTERVAL = 0.1
WS_PONG_TIMEOUT = WS_INTERVAL + 0.3
TCP_INTERVAL = 0.1
HTTP_INTERVAL = 0.5
RECONNECT_DELAY = 1.0

POD_NAME = os.getenv("HOSTNAME", socket.gethostname())
def now_iso(): return datetime.now(timezone.utc).isoformat()

# ---------- State ----------
connection_state = {
    peer: {"ws": "unknown", "tcp": "unknown", "http": "unknown", "last_change": ""} for peer in PEERS
}
HISTORY = []
MAX_HISTORY = 200

# ---------- Servers ----------
async def ws_server(websocket):
    try:
        async for _ in websocket:
            pass
    except Exception as e:
        logging.info(f"[WS Server] Disconnected: {e}")

async def handle_tcp(reader, writer):
    try:
        while True:
            data = await reader.readline()
            if not data: break
            writer.write(data)  # echo for TCP probe validation
            await writer.drain()
    finally:
        try: writer.close(); await writer.wait_closed()
        except Exception: pass

async def handle_http(reader, writer):
    try:
        data = await reader.read(4096)
        line0 = data.decode(errors="ignore").splitlines()[0] if data else ""
        method, path = ("GET","/") if not line0 else (line0.split()[0], line0.split()[1])
        status = 200
        body = ""

        if method == "POST" and path == "/admin/clear_history":
            HISTORY.clear()
            body = json.dumps({"ok": True, "pod": POD_NAME, "ts": now_iso()})
        elif method == "GET" and path == "/status":
            body = json.dumps({"self": POD_NAME, "timestamp": now_iso(), "connections": connection_state})
        elif method == "GET" and path == "/history":
            body = json.dumps(HISTORY[-MAX_HISTORY:])
        else:
            body = json.dumps({"status": "ok", "pod": POD_NAME, "ts": now_iso()})

        resp = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n{body}"
        )
        writer.write(resp.encode()); await writer.drain()
    finally:
        try: writer.close()
        except Exception: pass

# ---------- History helper ----------
def record_outage(target: str, proto: str, start_time_str: str):
    try:
        start = datetime.fromisoformat(start_time_str)
        end = datetime.now(timezone.utc)
        dur = (end - start).total_seconds()
        HISTORY.insert(0, {
            "name": target, "protocol": proto,
            "start_time": start.isoformat(), "end_time": end.isoformat(),
            "duration_sec": round(dur, 2),
            "source": "pod", "reporter": POD_NAME
        })
        del HISTORY[MAX_HISTORY:]
        logging.info(f"--- OUTAGE ENDED: {target} ({proto}) {dur:.2f}s ---")
    except Exception as e:
        logging.warning(f"[HISTORY] Failed to record outage: {e}")

# ---------- Clients ----------
async def ws_client_task(target: str):
    uri = f"ws://{'localhost' if target == POD_NAME else target}:{WS_PORT}"
    while True:
        start_time = now_iso()
        try:
            async with websockets.connect(uri) as ws:
                logging.info(f"[WS Client] Connected to {target}")
                connection_state[target]["ws"] = "connected"; connection_state[target]["last_change"] = now_iso()
                while True:
                    await ws.send(f'{{"from":"{POD_NAME}","ts":"{now_iso()}"}}')
                    pong_waiter = ws.ping()
                    await asyncio.wait_for(pong_waiter, timeout=WS_PONG_TIMEOUT)
                    await asyncio.sleep(WS_INTERVAL)
        except Exception as e:
            if connection_state[target]["ws"] != "disconnected":
                logging.info(f"[WS Client] Disconnected from {target}: {e}")
                connection_state[target]["ws"] = "disconnected"; connection_state[target]["last_change"] = now_iso()
                record_outage(target, "WS", start_time)
            await asyncio.sleep(RECONNECT_DELAY)

async def tcp_client_task(target: str):
    host = "localhost" if target == POD_NAME else target
    while True:
        start_time = now_iso()
        writer = None
        try:
            reader, writer = await asyncio.open_connection(host, TCP_PORT)
            logging.info(f"[TCP Client] Connected to {target}")
            connection_state[target]["tcp"] = "connected"; connection_state[target]["last_change"] = now_iso()
            while True:
                line = f"ping from {POD_NAME} at {now_iso()}\n".encode()
                writer.write(line); await writer.drain()
                echo = await asyncio.wait_for(reader.readline(), timeout=TCP_INTERVAL + 0.3)
                if echo != line: raise RuntimeError("TCP echo mismatch")
                await asyncio.sleep(TCP_INTERVAL)
        except Exception as e:
            if connection_state[target]["tcp"] != "disconnected":
                logging.info(f"[TCP Client] Disconnected from {target}: {e}")
                connection_state[target]["tcp"] = "disconnected"; connection_state[target]["last_change"] = now_iso()
                record_outage(target, "TCP", start_time)
            try:
                if writer: writer.close(); await writer.wait_closed()
            except Exception: pass
            await asyncio.sleep(RECONNECT_DELAY)

async def http_client_task(target: str):
    base = f"http://{'localhost' if target == POD_NAME else target}:{HTTP_PORT}"
    timeout = aiohttp.ClientTimeout(total=1.0)
    conn = {"status": "unknown", "since": now_iso()}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with session.get(f"{base}/ping") as resp:
                    resp.raise_for_status()
                    if conn["status"] == "error":
                        record_outage(target, "HTTP", conn["since"])
                    conn["status"] = "connected"; conn["since"] = now_iso()
                    connection_state[target]["http"] = "connected"; connection_state[target]["last_change"] = now_iso()
            except Exception:
                if conn["status"] != "error":
                    conn["status"] = "error"; conn["since"] = now_iso()
                connection_state[target]["http"] = "disconnected"; connection_state[target]["last_change"] = now_iso()
            await asyncio.sleep(HTTP_INTERVAL)

# ---------- Main ----------
async def main():
    ws_srv = websockets.serve(ws_server, "0.0.0.0", WS_PORT)
    tcp_srv = asyncio.start_server(handle_tcp, "0.0.0.0", TCP_PORT)
    http_srv = asyncio.start_server(handle_http, "0.0.0.0", HTTP_PORT)

    tasks = []
    for p in connection_state:
        tasks += [asyncio.create_task(ws_client_task(p)),
                  asyncio.create_task(tcp_client_task(p)),
                  asyncio.create_task(http_client_task(p))]

    await asyncio.gather(ws_srv, tcp_srv, http_srv, *tasks, return_exceptions=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Peer app shutting down.")

