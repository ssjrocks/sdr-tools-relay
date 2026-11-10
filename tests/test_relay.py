"""The relay against a recording instead of a dongle, a client written from the protocol, and fakes."""

import json
import socket
import struct
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "directory")]

from sdr_relay import protocol as P  # noqa: E402
from sdr_relay import upnp  # noqa: E402
from sdr_relay.discovery import Responder  # noqa: E402
from sdr_relay.rtlsdr import FileDongle  # noqa: E402
from sdr_relay.server import Config, Relay  # noqa: E402


def free_port(kind=socket.SOCK_STREAM):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def recording(tmp_path):
    p = tmp_path / "test.cu8"
    p.write_bytes(bytes(range(256)) * 4096)          # 1 MB with a known pattern
    return p


@pytest.fixture
def relay(recording):
    dongles = []

    def open_dongle():
        d = FileDongle(recording)
        dongles.append(d)
        return d

    r = Relay(Config(name="Test relay", port=free_port(), password="secret", file=str(recording), ppm=-4),
              version="9.9", open_dongle=open_dongle)
    r.dongles = dongles
    r.start()
    yield r
    r.stop()


def read_line(s):
    buf = b""
    while not buf.endswith(b"\n"):
        c = s.recv(1)
        if not c:
            break
        buf += c
    return json.loads(buf) if buf else None


def connect(relay, password):
    s = socket.create_connection(("127.0.0.1", relay.config.port), timeout=5)
    hello = read_line(s)
    s.sendall(P.line({"client": "test", "version": "1", "auth": P.auth(password, hello.get("nonce", ""))}))
    return s, hello, read_line(s)


def read_exact(s, n):
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        assert c, "closed"
        buf += c
    return buf


def test_hello_auth_and_rtl_tcp_stream(relay):
    s, hello, ok = connect(relay, "secret")
    assert hello["relay"] == "sdr-tools-relay" and hello["name"] == "Test relay" and hello["password"]
    assert hello["version"] == "9.9" and len(hello["nonce"]) == 32 and not hello["busy"]
    assert ok == {"ok": True, "ppm": -4}
    head = read_exact(s, 12)
    assert head[:4] == b"RTL0" and struct.unpack(">II", head[4:]) == (5, 29)
    s.sendall(P.command(P.SET_RATE, 1000000) + P.command(P.SET_FREQ, 161975000)
              + P.command(P.SET_GAIN_MODE, 1) + P.command(P.SET_GAIN, 372) + P.command(P.SET_PPM, 57))
    data = read_exact(s, 200000)
    i = data.index(bytes(range(256)))                # the recording's pattern comes through intact
    assert data[i:i + 1024] == bytes(range(256)) * 4
    time.sleep(0.2)
    calls = relay.dongles[0].calls
    assert ("set_sample_rate", 1000000) in calls and ("set_center_freq", 161975000) in calls
    assert ("set_tuner_gain", 372) in calls and ("set_freq_correction", -4) in calls
    assert ("set_freq_correction", 57) not in calls                   # the relay's own correction wins
    sess = relay.session
    assert sess.freq == 161975000 and sess.rate == 1000000 and sess.gain == "37.2 dB" and sess.bytes > 0
    # A second client is told it's busy.
    t = socket.create_connection(("127.0.0.1", relay.config.port), timeout=5)
    assert read_line(t)["busy"] and t.recv(10) == b""
    s.close()
    for _ in range(50):
        if relay.session is None:
            break
        time.sleep(0.1)
    assert relay.session is None and any("disconnected" in text for _, text in relay.events)


def test_wrong_password_and_probe(relay):
    s, hello, reply = connect(relay, "nope")
    assert reply == {"ok": False, "error": "wrong password"} and s.recv(10) == b""
    # The directory's reachability probe: read the hello and hang up.
    p = socket.create_connection(("127.0.0.1", relay.config.port), timeout=5)
    assert read_line(p)["relay"] == "sdr-tools-relay"
    p.close()
    time.sleep(0.2)
    assert relay.session is None
    s2, _, ok = connect(relay, "secret")          # still free afterwards
    assert ok["ok"]
    s2.close()


def test_owner_can_disconnect(relay):
    s, _, ok = connect(relay, "secret")
    read_exact(s, 12)
    time.sleep(0.2)
    relay.disconnect()
    s.settimeout(3)
    got = b""
    try:
        while True:
            c = s.recv(65536)
            if not c:
                break
            got += c
    except OSError:
        pass
    for _ in range(30):
        if relay.session is None:
            break
        time.sleep(0.1)
    assert relay.session is None


def test_lan_discovery(relay):
    port = free_port(socket.SOCK_DGRAM)
    resp = Responder(relay, port)
    resp.start()
    try:
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.settimeout(3)
        c.sendto(P.DISCOVER, ("127.0.0.1", port))
        answer = json.loads(c.recvfrom(4096)[0])
        assert answer["name"] == "Test relay" and answer["port"] == relay.config.port and "nonce" not in answer
        c.sendto(b"something else", ("127.0.0.1", port))
        with pytest.raises(socket.timeout):
            c.recvfrom(4096)
    finally:
        resp.stop()


def test_directory(relay, monkeypatch):
    import relay_directory as D
    D.relays.clear()
    D.hits.clear()
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), D.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}{D.PREFIX}"
    from sdr_relay import directory
    try:
        got = directory.register(relay, "abc-123", "Brisbane", "test", base=base)
        assert got["listed"] and got["address"] == "127.0.0.1"
        lst = json.loads(urllib.request.urlopen(f"{base}/relays").read())["relays"]
        assert len(lst) == 1 and lst[0]["name"] == "Test relay" and lst[0]["port"] == relay.config.port
        assert lst[0]["location"] == "Brisbane" and lst[0]["password"] and lst[0]["host"] == "127.0.0.1"
        # A port nothing answers on isn't listed.
        relay2 = Relay(Config(name="Closed", port=free_port()), version="1")
        got = directory.register(relay2, "def-456", "", "test", base=base)
        assert not got["listed"]
        assert len(json.loads(urllib.request.urlopen(f"{base}/relays").read())["relays"]) == 1
        directory.unregister("abc-123", "test", base=base)
        assert json.loads(urllib.request.urlopen(f"{base}/relays").read())["relays"] == []
        # Silly input is refused or trimmed.
        req = urllib.request.Request(f"{base}/register", data=json.dumps({"id": "x", "port": 80}).encode(),
                                     method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 400
    finally:
        server.shutdown()


def test_upnp_against_a_fake_router():
    calls = []

    class Router(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            xml = ('<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device><deviceList><device>'
                   '<serviceList><service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>'
                   '<controlURL>/ctl/IPConn</controlURL></service></serviceList></device></deviceList></device>'
                   '</root>').encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(xml)

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            action = self.headers["SOAPAction"].split("#")[1].strip('"')
            calls.append((action, body))
            out = "<NewExternalIPAddress>203.0.113.7</NewExternalIPAddress>" if action == "GetExternalIPAddress" else ""
            self.send_response(200)
            self.end_headers()
            self.wfile.write(f'<s:Envelope xmlns:s="x"><s:Body><u:{action}Response xmlns:u="y">{out}'
                             f'</u:{action}Response></s:Body></s:Envelope>'.encode())

    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Router)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        m = upnp.PortMapping(47320)
        m.open(f"http://127.0.0.1:{port}/desc.xml")
        assert m.external_ip == "203.0.113.7" and m.url == f"http://127.0.0.1:{port}/ctl/IPConn"
        add = next(b for a, b in calls if a == "AddPortMapping")
        assert "<NewExternalPort>47320</NewExternalPort>" in add and "<NewInternalClient>127.0.0.1<" in add
        m.close()
        assert calls[-1][0] == "DeletePortMapping"
    finally:
        server.shutdown()


def test_commands_sent_with_the_handshake_are_kept(relay):
    s = socket.create_connection(("127.0.0.1", relay.config.port), timeout=5)
    hello = read_line(s)
    s.sendall(P.line({"client": "test", "auth": P.auth("secret", hello["nonce"])})
              + P.command(P.SET_FREQ, 1090000000) + P.command(P.SET_RATE, 2400000))
    assert read_line(s)["ok"]
    read_exact(s, 12 + 100000)
    calls = relay.dongles[0].calls
    assert ("set_center_freq", 1090000000) in calls and ("set_sample_rate", 2400000) in calls
    s.close()
