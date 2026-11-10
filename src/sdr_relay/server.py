"""The relay itself: accepts one client at a time and gives it the dongle over rtl_tcp."""

import collections
import json
import logging
import secrets
import socket
import threading
import time

from . import protocol as P
from .rtlsdr import FileDongle, RtlError

log = logging.getLogger(__name__)
CLOSING_WAIT = 5.0             # s a new client waits for the previous one's dongle to close
QUEUE_BUFFERS = 64            # ~1.5 s at 2.4 MS/s with 64 KB buffers; older data is dropped


class Config:
    def __init__(self, name="SDR relay", port=P.PORT, password="", device=0, ppm=0, allow_client_ppm=False,
                 file=None):
        self.name = name
        self.port = port
        self.password = password
        self.device = device
        self.ppm = ppm
        self.allow_client_ppm = allow_client_ppm
        self.file = file                  # play this recording instead of a dongle


class Session:
    """What the window shows about the connected client."""

    def __init__(self, address):
        self.address = address
        self.started = time.time()
        self.bytes = 0
        self.dropped = 0
        self.freq = 0
        self.rate = 0
        self.gain = "auto"
        self.rate_bps = 0.0          # recent bytes per second
        self.closing = False         # the client has gone; the dongle is being closed


class Relay:
    def __init__(self, config: Config, version="0", open_dongle=None):
        self.config = config
        self.version = version
        self.open_dongle = open_dongle or self._open
        # Called (from a connection's thread) when a client with the password asks for an update:
        # returns (True, version being installed) or (False, why not). None: updates not offered.
        self.on_update = None
        self.session: Session | None = None
        self.last_error = ""
        self.events: collections.deque = collections.deque(maxlen=200)    # (time, text) for the window
        self._sock = None
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._client_sock = None

    # ---- what clients see ---------------------------------------------

    def describe_devices(self) -> list[dict]:
        if self.config.file:
            return [{"index": 0, "name": f"Recording {self.config.file}"}]
        try:
            from .rtlsdr import devices
            return [{"index": d["index"], "name": d["name"], "serial": d["serial"]} for d in devices()]
        except RtlError:
            return []

    def hello(self, nonce="") -> dict:
        h = {"relay": "sdr-tools-relay", "protocol": P.PROTOCOL, "name": self.config.name, "version": self.version,
             "devices": self.describe_devices(), "password": bool(self.config.password),
             "busy": self.session is not None,
             "features": ["check", "diagnose"] + (["update"] if self.on_update is not None else [])}
        if nonce:
            h["nonce"] = nonce
        return h

    def note(self, text):
        log.info(text)
        self.events.append((time.time(), text))

    # ---- serving ------------------------------------------------------

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", self.config.port))
        s.listen(4)
        s.settimeout(0.5)
        self._sock = s
        self._halt.clear()
        threading.Thread(target=self._accept, daemon=True, name="relay-accept").start()
        self.note(f"Listening on port {self.config.port}")

    def stop(self):
        self._halt.set()
        self.disconnect()
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def disconnect(self):
        with self._lock:
            sock = self._client_sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _accept(self):
        while not self._halt.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn, addr), daemon=True, name="relay-client").start()

    def _handle(self, conn, addr):
        conn.settimeout(10)
        nonce = secrets.token_hex(16)
        try:
            # A client that has just left may still be closing the dongle (a decoder restarting,
            # or SDR Toolkit's password check): wait for it rather than turn the next one away.
            end = time.monotonic() + CLOSING_WAIT
            while self.session is not None and self.session.closing and time.monotonic() < end:
                time.sleep(0.05)
            busy = self.session is not None
            conn.sendall(P.line(self.hello(nonce)))
            if busy:
                return
            reply, rest = self._read_line(conn)
            if reply is None:
                return                                      # a probe (the directory checking we're reachable)
            if not P.check(self.config.password, nonce, reply.get("auth", "")):
                conn.sendall(P.line({"ok": False, "error": "wrong password"}))
                self.note(f"{addr[0]}: wrong password")
                return
            if reply.get("update"):
                conn.sendall(P.line(self._update_request(addr)))
                return
            if reply.get("diagnose"):
                conn.settimeout(120)
                conn.sendall(P.line(self._diagnose_request(addr)))
                return
            if reply.get("check"):
                # Only checking the password (SDR Toolkit's Connect): don't open the dongle.
                conn.sendall(P.line({"ok": True, "ppm": self.config.ppm, "checked": True}))
                return
            with self._lock:
                if self.session is not None:
                    conn.sendall(P.line({"ok": False, "error": "busy"}))
                    return
                self.session = Session(addr[0])
                self._client_sock = conn
            try:
                self._serve(conn, addr, reply, rest)
            finally:
                with self._lock:
                    self.session = None
                    self._client_sock = None
        except OSError as e:
            log.info("%s: %s", addr[0], e)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _update_request(self, addr) -> dict:
        if self.on_update is None:
            return {"ok": False, "error": "this relay can't update itself"}
        if not self.config.password:
            return {"ok": False, "error": "updating from SDR Toolkit needs a password set on the relay"}
        self.note(f"{addr[0]} asked for an update")
        try:
            ok, text = self.on_update(addr[0])
        except Exception as e:  # reported to the client
            log.exception("update request")
            ok, text = False, str(e)
        return {"ok": True, "updating": text} if ok else {"ok": False, "error": text}

    def _diagnose_request(self, addr) -> dict:
        if not self.config.password:
            return {"ok": False, "error": "the dongle check needs a password set on the relay"}
        with self._lock:
            if self.session is not None:
                return {"ok": False, "error": "busy"}
            self.session = Session(addr[0])         # nobody else opens the dongle meanwhile
        try:
            self.note(f"{addr[0]} asked for a dongle check")
            if self.config.file:
                return {"ok": True, "report": {"skipped": "playing a recording"}}
            from .diagnose import run
            report = run(self.config.device)
            self.note("Dongle check: " + ", ".join(
                f"{k} {v.get('bytes', 0) // 1000} kB" for k, v in report.items() if isinstance(v, dict)))
            return {"ok": True, "report": report}
        finally:
            with self._lock:
                self.session = None

    @staticmethod
    def _read_line(conn) -> tuple[dict | None, bytes]:
        """The client's JSON line, and whatever came after it (rtl_tcp commands sent straight away)."""
        buf = b""
        while b"\n" not in buf and len(buf) < 4096:
            try:
                chunk = conn.recv(1024)
            except socket.timeout:
                return None, b""
            if not chunk:
                return None, b""
            buf += chunk
        head, _, rest = buf.partition(b"\n")
        try:
            return json.loads(head), rest
        except ValueError:
            return None, b""

    def _open(self):
        if self.config.file:
            return FileDongle(self.config.file)
        from .rtlsdr import Dongle
        return Dongle(self.config.device)

    def _serve(self, conn, addr, reply, rest=b""):
        session = self.session
        try:
            dongle = self.open_dongle()
        except RtlError as e:
            self.last_error = str(e)
            conn.sendall(P.line({"ok": False, "error": str(e)}))
            self.note(f"{addr[0]}: couldn't open the dongle: {e}")
            return
        try:
            self.last_error = ""
            conn.sendall(P.line({"ok": True, "ppm": self.config.ppm}))
            client = " ".join(str(x) for x in (reply.get("client") or "a client", reply.get("version")) if x)
            self.note(f"{addr[0]} connected ({client})")
            gains = dongle.gains()
            # rtl_tcp's defaults, until the client sets its own.
            dongle.call("set_sample_rate", 2048000)
            dongle.call("set_center_freq", 100000000)
            dongle.call("set_freq_correction", int(self.config.ppm))
            dongle.call("set_tuner_gain_mode", 0)
            session.rate, session.freq = 2048000, 100000000
            conn.settimeout(None)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.sendall(P.header(dongle.tuner, len(gains)))
            queue: collections.deque = collections.deque()
            ready = threading.Condition()
            done = threading.Event()

            def on_data(data):
                with ready:
                    if len(queue) >= QUEUE_BUFFERS:
                        queue.popleft()
                        session.dropped += 1
                    queue.append(data)
                    ready.notify()

            def sender():
                window = collections.deque()
                try:
                    while not done.is_set():
                        with ready:
                            while not queue and not done.is_set():
                                ready.wait(0.5)
                            if done.is_set():
                                break
                            data = queue.popleft()
                        conn.sendall(data)
                        now = time.monotonic()
                        session.bytes += len(data)
                        window.append((now, len(data)))
                        while window and now - window[0][0] > 2:
                            window.popleft()
                        span = max(now - window[0][0], 0.5)
                        session.rate_bps = sum(n for _, n in window) / span
                except OSError:
                    pass
                done.set()

            dongle.start(on_data)
            t = threading.Thread(target=sender, daemon=True, name="relay-send")
            t.start()
            buf = rest
            while len(buf) >= 5:
                cmd, value = P.parse_command(buf[:5])
                buf = buf[5:]
                self._command(dongle, session, gains, cmd, value)
            while not done.is_set():
                try:
                    chunk = conn.recv(256)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 5:
                    cmd, value = P.parse_command(buf[:5])
                    buf = buf[5:]
                    self._command(dongle, session, gains, cmd, value)
            done.set()
            session.closing = True
            with ready:
                ready.notify_all()
            dongle.stop()
            t.join(2)
            mb = session.bytes / 1e6
            self.note(f"{addr[0]} disconnected after {time.time() - session.started:.0f} s, {mb:.1f} MB"
                      + (f", {session.dropped} buffers dropped (network too slow)" if session.dropped else ""))
        finally:
            try:
                dongle.close()
            except RtlError as e:
                self.last_error = str(e)
                self.note(str(e))

    def _command(self, dongle, session, gains, cmd, value):
        if cmd == P.SET_FREQ:
            dongle.call("set_center_freq", value)
            session.freq = value
        elif cmd == P.SET_RATE:
            # Not reset_buffer, as rtl_tcp: resetting a running async read stalls the stream.
            dongle.call("set_sample_rate", value)
            session.rate = value
        elif cmd == P.SET_GAIN_MODE:
            dongle.call("set_tuner_gain_mode", value)
            if not value:
                session.gain = "auto"
        elif cmd == P.SET_GAIN:
            dongle.call("set_tuner_gain", P.signed(value))
            session.gain = f"{P.signed(value) / 10:.1f} dB"
        elif cmd == P.SET_PPM:
            if self.config.allow_client_ppm:
                dongle.call("set_freq_correction", P.signed(value))
        elif cmd == P.SET_IF_GAIN:
            gain = value & 0xFFFF                       # stage in the top 16 bits, a signed 16-bit gain below
            dongle.call("set_tuner_if_gain", (value >> 16) & 0xFFFF, gain - 0x10000 if gain & 0x8000 else gain)
        elif cmd == P.SET_TEST_MODE:
            dongle.call("set_testmode", value)
        elif cmd == P.SET_AGC:
            dongle.call("set_agc_mode", value)
        elif cmd == P.SET_DIRECT_SAMPLING:
            dongle.call("set_direct_sampling", value)
        elif cmd == P.SET_OFFSET_TUNING:
            dongle.call("set_offset_tuning", value)
        elif cmd == P.SET_RTL_XTAL:
            dongle.call("set_xtal_freq", value, 0)
        elif cmd == P.SET_TUNER_XTAL:
            dongle.call("set_xtal_freq", 0, value)
        elif cmd == P.SET_GAIN_INDEX:
            if 0 <= value < len(gains):
                dongle.call("set_tuner_gain", gains[value])
                session.gain = f"{gains[value] / 10:.1f} dB"
        elif cmd == P.SET_BIAS_TEE:
            dongle.call("set_bias_tee", value)
