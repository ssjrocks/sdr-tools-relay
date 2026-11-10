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


def test_finding_dongles_without_winusb():
    from sdr_relay import driver
    rows = [  # Win32_PnPEntity, as Windows lists an RTL-SDR (composite: parent + two interfaces)
        {"PNPDeviceID": "USB\\VID_0BDA&PID_2838\\00000001", "Name": "USB Composite Device", "Service": "usbccgp"},
        {"PNPDeviceID": "USB\\VID_0BDA&PID_2838&MI_00\\6&1A2B3C&0&0000", "Name": "Bulk-In, Interface", "Service": None},
        {"PNPDeviceID": "USB\\VID_0BDA&PID_2838&MI_01\\6&1A2B3C&0&0001", "Name": "Bulk-In, Interface", "Service": None},
        {"PNPDeviceID": "USB\\VID_046D&PID_C52B\\5&2", "Name": "Logitech", "Service": "usbccgp"}]
    need = driver.parse(rows)
    assert len(need) == 1 and (need[0].vid, need[0].pid, need[0].iid) == (0x0BDA, 0x2838, 0)
    assert "no driver" in need[0].describe()
    cmd = driver.command(need[0], "C:\\tmp")
    assert cmd[cmd.index("--vid") + 1] == "0x0BDA" and cmd[cmd.index("--iid") + 1] == "0" and "--silent" in cmd
    # A TV driver from Windows Update also needs replacing; WinUSB doesn't.
    tv = driver.parse({"PNPDeviceID": "USB\\VID_0BDA&PID_2832&MI_00\\x", "Name": "RTL2832U", "Service": "RTL2832UBDA"})
    assert tv and tv[0].service == "RTL2832UBDA"
    assert driver.parse([{"PNPDeviceID": "USB\\VID_0BDA&PID_2838&MI_00\\x", "Service": "WinUSB"}]) == []
    # A non-composite one (no MI_ part) is installed without --iid.
    plain = driver.parse([{"PNPDeviceID": "USB\\VID_0BDA&PID_2838\\0001", "Name": "RTL", "Service": ""}])
    assert plain[0].iid is None and "--iid" not in driver.command(plain[0], "x")
    assert driver.missing() == []                 # off Windows


def test_rate_change_doesnt_reset_the_running_stream(relay):
    # rtl_tcp never calls rtlsdr_reset_buffer on a rate change: on a running async read it
    # stops the samples (seen on Windows: 0.0 MB in 61 s after SDR Toolkit set 2.4 MS/s).
    s, _, ok = connect(relay, "secret")
    read_exact(s, 12)
    s.sendall(P.command(P.SET_RATE, 2400000) + P.command(P.SET_FREQ, 1090000000))
    read_exact(s, 200000)
    calls = [c[0] for c in relay.dongles[-1].calls]
    assert "set_sample_rate" in calls and "reset_buffer" not in calls
    s.close()


def test_password_check_doesnt_open_the_dongle(relay):
    s = socket.create_connection(("127.0.0.1", relay.config.port), timeout=5)
    hello = read_line(s)
    s.sendall(P.line({"client": "test", "auth": P.auth("secret", hello["nonce"]), "check": True}))
    assert read_line(s) == {"ok": True, "ppm": -4, "checked": True} and s.recv(10) == b""
    assert relay.dongles == [] and relay.session is None
    s2 = socket.create_connection(("127.0.0.1", relay.config.port), timeout=5)
    hello = read_line(s2)
    s2.sendall(P.line({"client": "test", "auth": P.auth("nope", hello["nonce"]), "check": True}))
    assert read_line(s2)["error"] == "wrong password"


def test_next_client_waits_for_the_previous_dongle_to_close(relay, recording):
    slow = []

    class SlowClose(FileDongle):
        def close(self):
            time.sleep(1.5)                          # librtlsdr on Windows takes a while
            super().close()

    def opener():
        d = SlowClose(recording)
        slow.append(d)
        return d

    relay.open_dongle = opener
    s, _, ok = connect(relay, "secret")
    read_exact(s, 12 + 1000)
    s.close()
    time.sleep(0.2)                                  # gone, but its dongle is still closing
    s2, hello, ok2 = connect(relay, "secret")
    assert not hello["busy"] and ok2["ok"], (hello, ok2)
    assert read_exact(s2, 12)[:4] == b"RTL0" and len(slow) == 2
    s2.close()


def ask_update(relay, password):
    s = socket.create_connection(("127.0.0.1", relay.config.port), timeout=5)
    hello = read_line(s)
    s.sendall(P.line({"client": "test", "auth": P.auth(password, hello["nonce"]), "update": True}))
    return hello, read_line(s)


def test_update_on_request_needs_the_password(relay):
    asked = []
    relay.on_update = lambda who: (asked.append(who), (True, "0.9.0"))[1]
    hello, answer = ask_update(relay, "secret")
    assert "update" in hello["features"] and answer == {"ok": True, "updating": "0.9.0"} and asked == ["127.0.0.1"]
    assert relay.dongles == []                               # nothing streamed
    _, answer = ask_update(relay, "nope")
    assert answer["error"] == "wrong password" and len(asked) == 1
    relay.on_update = lambda who: (False, "0.1.2 is already the latest version")
    assert ask_update(relay, "secret")[1] == {"ok": False, "error": "0.1.2 is already the latest version"}
    relay.config.password = ""                               # no password: nobody may
    relay.on_update = lambda who: (asked.append(who), (True, "0.9.0"))[1]
    _, answer = ask_update(relay, "")
    assert "needs a password" in answer["error"] and len(asked) == 1
    relay.on_update = None
    relay.config.password = "secret"
    hello, answer = ask_update(relay, "secret")
    assert "update" not in hello["features"] and not answer["ok"]


class FakeLib:
    """librtlsdr as on the Windows PC in the problem report: async reads deliver nothing."""

    def __init__(self, async_works=False):
        self.async_works = async_works
        self.cancelled = False
        self.sync_reads = 0

    def rtlsdr_reset_buffer(self, dev):
        return 0

    def rtlsdr_read_async(self, dev, cb, ctx, num, length):
        import ctypes
        buf = (ctypes.c_ubyte * length)()
        while not self.cancelled:
            if self.async_works:
                cb(buf, length, None)
            time.sleep(0.01)
        return 0

    def rtlsdr_cancel_async(self, dev):
        self.cancelled = True
        return 0

    def rtlsdr_read_sync(self, dev, buf, length, n):
        import ctypes
        self.sync_reads += 1
        ctypes.memset(buf, 0x55, length)
        n._obj.value = length
        time.sleep(0.005)
        return 0

    def rtlsdr_close(self, dev):
        return 0


def fake_dongle(lib):
    from sdr_relay.rtlsdr import Dongle
    d = Dongle.__new__(Dongle)
    d.lib, d.dev, d.index, d._thread, d._cb = lib, None, 0, None, None
    return d


def test_dongle_that_sends_nothing_is_flagged_not_hung():
    # Seen on a Windows PC: neither async nor sync reads got samples, and the sync read then
    # never returned, holding the dongle. So no sync fallback: async, and a warning.
    lib = FakeLib(async_works=False)
    d = fake_dongle(lib)
    got = []
    d.start(got.append, buf_len=4096)
    time.sleep(3.5)
    assert d.no_samples and d.method == "async" and lib.sync_reads == 0 and not got
    d.close()
    assert lib.cancelled
    ok = FakeLib(async_works=True)
    d2 = fake_dongle(ok)
    got2 = []
    d2.start(got2.append, buf_len=4096)
    time.sleep(3.5)
    assert not d2.no_samples and got2 and ok.sync_reads == 0
    d2.close()


def test_dongle_check_on_request(relay):
    s = socket.create_connection(("127.0.0.1", relay.config.port), timeout=5)
    hello = read_line(s)
    assert "diagnose" in hello["features"]
    s.sendall(P.line({"client": "test", "auth": P.auth("secret", hello["nonce"]), "diagnose": True}))
    assert read_line(s) == {"ok": True, "report": {"skipped": "playing a recording"}}
    assert relay.session is None
