"""Answers SDR Toolkit's LAN scans (UDP broadcast on port 47321)."""

import json
import logging
import socket
import threading

from . import protocol as P

log = logging.getLogger(__name__)


class Responder:
    def __init__(self, relay, port=P.DISCOVERY_PORT):
        self.relay = relay
        self.port = port
        self._halt = threading.Event()
        self._sock = None

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", self.port))
        s.settimeout(0.5)
        self._sock = s
        threading.Thread(target=self._run, daemon=True, name="relay-discovery").start()

    def stop(self):
        self._halt.set()
        if self._sock is not None:
            self._sock.close()

    def _run(self):
        while not self._halt.is_set():
            try:
                data, addr = self._sock.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.strip() != P.DISCOVER:
                continue
            answer = dict(self.relay.hello(), port=self.relay.config.port)
            try:
                self._sock.sendto(json.dumps(answer).encode(), addr)
            except OSError as e:
                log.info("discovery reply to %s: %s", addr, e)
