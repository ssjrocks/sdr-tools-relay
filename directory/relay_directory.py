#!/usr/bin/env python3
"""The public list of SDR Tools Relays, served at https://ssjrocks.com/api/sdr-relay/v1/ (nginx -> 127.0.0.1:8793).

POST /register    {"id", "name", "port", "location", "version", "password", "devices"}
                  The relay's address is the one the request came from (X-Real-IP from nginx), so a
                  relay can only list itself. The directory connects to that address and port and
                  reads the relay's hello; only relays that answer are listed.
POST /unregister  {"id"}
GET  /relays      {"relays": [{"name", "host", "port", "location", "version", "password", "devices",
                               "busy", "seen"}]}: relays heard in the last 3 minutes that were reachable
GET  /health

Everything is kept in memory: relays register again every minute.
"""

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "/api/sdr-relay/v1"
STALE = 180
MAX_TOTAL = 5000
MAX_PER_IP = 4
REQUESTS_PER_MIN = 12

relays: dict[str, dict] = {}
hits: dict[str, list] = {}
lock = threading.Lock()


def text(value, limit) -> str:
    return "".join(c for c in str(value or "") if c.isprintable())[:limit].strip()


def probe(host, port, timeout=4.0) -> dict | None:
    """The relay's hello, if it answers on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            buf = b""
            while b"\n" not in buf and len(buf) < 8192:
                chunk = s.recv(2048)
                if not chunk:
                    break
                buf += chunk
        hello = json.loads(buf.split(b"\n", 1)[0])
        return hello if hello.get("relay") == "sdr-tools-relay" else None
    except (OSError, ValueError):
        return None


def limited(ip) -> bool:
    now = time.time()
    with lock:
        recent = [t for t in hits.get(ip, []) if now - t < 60]
        recent.append(now)
        hits[ip] = recent
        if len(hits) > 20000:
            hits.clear()
        return len(recent) > REQUESTS_PER_MIN


def prune():
    now = time.time()
    with lock:
        for k in [k for k, r in relays.items() if now - r["seen"] > STALE]:
            del relays[k]


class Handler(BaseHTTPRequestHandler):
    server_version = "sdr-relay-directory/1"

    def log_message(self, fmt, *args):
        pass                                   # no request log: addresses aren't kept beyond the list

    def ip(self) -> str:
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 8192:
            raise ValueError("too big")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data

    def do_GET(self):
        if self.path == f"{PREFIX}/health":
            return self.send(200, {"ok": True, "relays": len(relays)})
        if self.path.split("?")[0] == f"{PREFIX}/relays":
            prune()
            with lock:
                out = [{k: r[k] for k in ("name", "host", "port", "location", "version", "password", "devices",
                                          "busy", "seen")} for r in relays.values() if r["reachable"]]
            out.sort(key=lambda r: r["name"].lower())
            return self.send(200, {"relays": out})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        ip = self.ip()
        if limited(ip):
            return self.send(429, {"error": "too many requests"})
        try:
            data = self.body()
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        rid = text(data.get("id"), 64)
        if not rid:
            return self.send(400, {"error": "id missing"})
        if self.path == f"{PREFIX}/unregister":
            with lock:
                if relays.get(rid, {}).get("host") == ip:
                    del relays[rid]
            return self.send(200, {"ok": True})
        if self.path != f"{PREFIX}/register":
            return self.send(404, {"error": "not found"})
        try:
            port = int(data.get("port"))
        except (TypeError, ValueError):
            port = 0
        if not 1024 <= port <= 65535:
            return self.send(400, {"error": "port must be 1024-65535"})
        prune()
        with lock:
            mine = [k for k, r in relays.items() if r["host"] == ip and k != rid]
            if len(mine) >= MAX_PER_IP or (rid not in relays and len(relays) >= MAX_TOTAL):
                return self.send(429, {"error": "too many relays"})
        hello = probe(ip, port)
        entry = {"name": text(data.get("name"), 40) or "SDR relay", "host": ip, "port": port,
                 "location": text(data.get("location"), 60), "version": text(data.get("version"), 20),
                 "password": bool(data.get("password")),
                 "devices": [text(d, 60) for d in (data.get("devices") or [])[:4] if isinstance(d, str)],
                 "busy": bool(hello and hello.get("busy")), "reachable": hello is not None, "seen": int(time.time())}
        with lock:
            relays[rid] = entry
        self.send(200, {"ok": True, "listed": entry["reachable"], "reachable": entry["reachable"], "address": ip})


def main():
    port = int(os.environ.get("PORT", "8793"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
