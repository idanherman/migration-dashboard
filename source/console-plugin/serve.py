#!/usr/bin/env python3
"""Serve console-plugin static assets over HTTPS (disconnected-friendly)."""
import http.server
import os
import ssl

ROOT = os.environ.get("PLUGIN_ROOT", "/opt/plugin")
PORT = int(os.environ.get("PLUGIN_PORT", "9443"))
CERT = os.environ.get("TLS_CERT", "/var/cert/tls.crt")
KEY = os.environ.get("TLS_KEY", "/var/cert/tls.key")

os.chdir(ROOT)


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "application/javascript",
        ".json": "application/json",
    }

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args), flush=True)


httpd = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(CERT, KEY)
httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
print("poc-plugin serving %s on :%s" % (ROOT, PORT), flush=True)
httpd.serve_forever()
