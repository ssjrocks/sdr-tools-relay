"""The relay itself: accepts one client at a time and gives it the dongle over rtl_tcp (an RTL-SDR or a
HackRF; a HackRF can also sweep, and transmit when its owner allows it)."""

import collections
import json
import logging
import secrets
import socket
import threading
import time

from . import dsp
from . import protocol as P
from .rtlsdr import FileDongle, RtlError

log = logging.getLogger(__name__)
CLOSING_WAIT = 5.0             # s a new client waits for the previous one's dongle to close
QUEUE_BUFFERS = 64            # ~1.5 s at 2.4 MS/s with 64 KB buffers; older data is dropped


def device_id(device) -> str:
    """"rtl:0", "hackrf:<serial>"; a bare number (what older relays saved) is an RTL-SDR's index."""
    text = str(device).strip()
    return f"rtl:{int(text)}" if text.lstrip("-").isdigit() else (text or "rtl:0")


class Config:
    def __init__(self, name="SDR relay", port=P.PORT, password="", device="rtl:0", ppm=0, allow_client_ppm=False,
                 file=None, allow_tx=False, host="0.0.0.0"):
        self.name = name
        self.host = host                  # where it listens: every interface, or 127.0.0.1 (SDR Toolkit's built-in one)
        self.port = port
        self.password = password
        self.device = device_id(device)
        self.ppm = ppm
        self.allow_client_ppm = allow_client_ppm
        self.file = file                  # play this recording instead of a dongle
        self.allow_tx = allow_tx          # a HackRF may transmit (and only for a client with the password)


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
        self.rate_bps = 0.0          # recent bytes per second (to the client)
        self.closing = False         # the client has gone; the dongle is being closed
        self.device = "rtl"
        self.device_bytes = 0        # from the device (its USB link)
        self._device_window: collections.deque = collections.deque()
        # A HackRF's samples on the way out (dsp.py): DC spike removal, narrowing, the level.
        self.device_rate = 0         # what the device samples at (rate x narrow)
        self.dc = None               # a dsp.DCBlock while it's on
        self.decimate = None         # a dsp.Decimator for a rate below the HackRF's 2 MS/s
        self.level = None            # a dsp.Level, measured as the samples go out
        self.level_last = None
        self.dongle = None

    def device_arrived(self, n):
        now = time.monotonic()
        self.device_bytes += n
        w = self._device_window
        w.append((now, n))
        while w and now - w[0][0] > 2:
            w.popleft()

    @property
    def device_bps(self) -> float:
        w = self._device_window
        if not w:
            return 0.0
        return sum(n for _, n in w) / max(time.monotonic() - w[0][0], 0.5)

    def stats(self) -> dict:
        """The links, for a client's stats request: the device's (USB) and the network's, each against what
        the sample rate needs (2 bytes a sample), and what's been dropped."""
        out = {"device": self.device, "freq": self.freq, "rate": self.rate, "gain": self.gain,
               "needed_bps": self.rate * 2, "device_bps": round(self.device_bps), "net_bps": round(self.rate_bps),
               "device_bytes": self.device_bytes, "net_bytes": self.bytes, "dropped": self.dropped,
               "seconds": round(time.time() - self.started, 1)}
        if self.device == "hackrf":
            d = self.decimate
            out.update(device_rate=self.device_rate or self.rate, narrow=d.k if d else 1, dc_block=self.dc is not None,
                       level=self.level_last, usb_lost=getattr(self.dongle, "shortfalls", None))
        return out


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
            return [{"index": 0, "id": "rtl:0", "type": "rtl", "name": f"Recording {self.config.file}"}]
        out = []
        try:
            from .rtlsdr import devices
            out += [{"index": d["index"], "id": f"rtl:{d['index']}", "type": "rtl", "name": d["name"],
                     "serial": d["serial"]} for d in devices()]
        except RtlError:
            pass
        try:
            from .hackrf import devices as hackrfs
            out += [{"index": d["index"], "id": f"hackrf:{d['serial']}", "type": "hackrf", "name": d["name"],
                     "serial": d["serial"]} for d in hackrfs()]
        except RtlError:
            pass
        return out

    def features(self) -> list[str]:
        f = ["check", "diagnose", "hackrf", "sweep", "narrow", "dc_block", "levels", "sweep_trim"]
        if self.config.allow_tx and self.config.password:
            f.append("tx")
        return f + (["update"] if self.on_update is not None else [])

    def hello(self, nonce="") -> dict:
        h = {"relay": "sdr-tools-relay", "protocol": P.PROTOCOL, "name": self.config.name, "version": self.version,
             "devices": self.describe_devices(), "password": bool(self.config.password),
             "busy": self.session is not None,
             "features": self.features(), "device": self.config.device}
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
        s.bind((self.config.host, self.config.port))
        self.config.port = s.getsockname()[1]           # port 0: whichever was free
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
        # Wait for the client's thread to close the SDR: a program quitting while it's still inside libhackrf
        # or librtlsdr crashed Python on its way out (a segfault in the "relay-client" thread).
        for t in list(getattr(self, "_threads", [])):
            if t is not threading.current_thread():
                t.join(5)

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
            t = threading.Thread(target=self._handle, args=(conn, addr), daemon=True, name="relay-client")
            self._threads = [x for x in getattr(self, "_threads", []) if x.is_alive()] + [t]
            t.start()

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
                # Only a stats request (with the password) is answered while someone is connected.
                conn.settimeout(2)
                reply, _ = self._read_line(conn)
                session = self.session
                if reply and reply.get("stats") and P.check(self.config.password, nonce, reply.get("auth", "")) \
                        and session is not None:
                    conn.sendall(P.line({"ok": True, "stats": session.stats()}))
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
            if reply.get("stats"):
                conn.sendall(P.line({"ok": True, "stats": None}))          # nobody is connected
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
            if device_id(self.config.device).startswith("hackrf:"):
                return {"ok": True, "report": {"skipped": "the dongle check is for RTL-SDRs; this relay serves a HackRF"}}
            from .diagnose import run
            report = run(int(device_id(self.config.device).partition(":")[2] or 0))
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

    def _open(self, device=None):
        if self.config.file:
            return FileDongle(self.config.file)
        kind, _, which = device_id(device or self.config.device).partition(":")
        if kind == "hackrf":
            from .hackrf import HackRF
            return HackRF(which)
        from .rtlsdr import Dongle
        return Dongle(int(which or 0))

    def _serve(self, conn, addr, reply, rest=b""):
        session = self.session
        stream = reply.get("stream") or "iq"
        if stream == "tx" and not (self.config.allow_tx and self.config.password):
            conn.sendall(P.line({"ok": False, "error": "this relay doesn't allow transmitting (its owner can turn it "
                                                      "on, with a password set)"}))
            self.note(f"{addr[0]} asked to transmit: not allowed")
            return
        try:
            wanted = reply.get("device")
            dongle = self.open_dongle(wanted) if wanted else self.open_dongle()
        except TypeError:                                  # an opener that takes no device (tests)
            dongle = self.open_dongle()
        except RtlError as e:
            self.last_error = str(e)
            conn.sendall(P.line({"ok": False, "error": str(e)}))
            self.note(f"{addr[0]}: couldn't open the dongle: {e}")
            return
        hackrf = getattr(dongle, "is_hackrf", False)
        if stream in ("sweep", "tx") and not hackrf:
            dongle.close()
            conn.sendall(P.line({"ok": False, "error": f"only a HackRF can {'sweep' if stream == 'sweep' else 'transmit'}"}))
            return
        try:
            self.last_error = ""
            info = dongle.info() if hackrf else {"type": "rtl"}
            client = " ".join(str(x) for x in (reply.get("client") or "a client", reply.get("version")) if x)
            if stream == "sweep":
                return self._sweep(conn, addr, reply, dongle, info, session, client)
            if stream == "tx":
                return self._transmit(conn, addr, reply, dongle, info, session, client)
            conn.sendall(P.line({"ok": True, "ppm": self.config.ppm, "device": info}))
            self.note(f"{addr[0]} connected ({client}){' to the HackRF' if hackrf else ''}")
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

            session.device = info.get("type", "rtl")
            if hackrf:
                session.dongle = dongle
                session.dc = dsp.DCBlock()
                session.level = dsp.Level()
                session.device_rate = session.rate

            def on_data(data):
                session.device_arrived(len(data))
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
                        if session.level is not None:
                            data = self._process(session, data)
                            if not data:
                                continue
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

            try:
                dongle.start(on_data)
            except RtlError as e:
                self.last_error = str(e)
                self.note(f"{addr[0]}: {e}")
                return

            def no_samples_note():
                if getattr(dongle, "no_samples", False) and not done.is_set():
                    self.note("The dongle sends no samples. Try another USB port (a rear one, no hub or "
                              "extension cable) or replug it; the dongle check in SDR Toolkit's relay menu "
                              "says more.")
            threading.Timer(4.0, no_samples_note).start()
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

    @staticmethod
    def _process(session, data: bytes) -> bytes:
        """A HackRF buffer on its way out (in the sending thread, so the USB callback never waits): measured,
        the DC spike taken out, narrowed."""
        session.level(data)
        now = time.monotonic()
        if now - getattr(session, "_level_t", 0) >= 1.0:
            session._level_t = now
            session.level_last = session.level.take()
        dc, dec = session.dc, session.decimate
        if dc is not None:
            data = dc(data, session.device_rate or session.rate)
        if dec is not None:
            data = dec(data)
        return data

    def _sweep(self, conn, addr, reply, dongle, info, session, client):
        """hackrf_sweep's raw transfers to the client, until it disconnects."""
        ranges = [(int(a), int(b)) for a, b in (reply.get("ranges") or [[0, 6000]])][:10]
        fft_size = max(0, min(int(reply.get("fft_size") or 0), dsp.SWEEP_BLOCK // 2 - 5))
        window: collections.deque = collections.deque()          # what went out, the last 2 s (the stats' rate)
        for key, name in (("amp", "set_amp_enable"), ("antenna", "set_antenna_enable")):
            if reply.get(key) is not None:
                dongle.call(name, int(bool(reply[key])))
        sent = threading.Event()
        q: collections.deque = collections.deque()
        ready = threading.Condition()

        session.device = "hackrf"

        def on_block(data):
            session.device_arrived(len(data))
            with ready:
                if len(q) >= QUEUE_BUFFERS:
                    q.popleft()
                    session.dropped += 1
                q.append(data)
                ready.notify()
        try:
            freqs = dongle.start_sweep(on_block, ranges, int(reply.get("lna", 16)), int(reply.get("vga", 20)))
        except RtlError as e:
            conn.sendall(P.line({"ok": False, "error": str(e)}))
            self.note(f"{addr[0]}: couldn't sweep: {e}")
            return
        conn.sendall(P.line({"ok": True, "stream": "sweep", "ranges": freqs, "device": info}))
        self.note(f"{addr[0]} sweeping {', '.join(f'{a}-{b}' for a, b in zip(freqs[::2], freqs[1::2]))} MHz ({client})")
        session.freq, session.rate, session.gain = freqs[0] * 1_000_000, 20_000_000, "sweep"
        conn.settimeout(None)

        def watch():                                    # the client hanging up ends it
            try:
                while conn.recv(256):
                    pass
            except OSError:
                pass
            sent.set()
            with ready:
                ready.notify_all()
        threading.Thread(target=watch, daemon=True, name="relay-sweep-watch").start()
        try:
            while not sent.is_set():
                with ready:
                    while not q and not sent.is_set():
                        ready.wait(0.5)
                    if sent.is_set():
                        break
                    data = q.popleft()
                if fft_size:
                    data = dsp.trim_sweep(data, fft_size)
                conn.sendall(data)
                session.bytes += len(data)
                now = time.monotonic()
                window.append((now, len(data)))
                while window and now - window[0][0] > 2:
                    window.popleft()
                session.rate_bps = sum(n for _, n in window) / max(now - window[0][0], 0.5)
        except OSError:
            pass
        session.closing = True
        dongle.stop()
        self.note(f"{addr[0]} stopped sweeping after {time.time() - session.started:.0f} s")

    def _transmit(self, conn, addr, reply, dongle, info, session, client):
        """The client's signed 8-bit IQ out of the HackRF, until it disconnects."""
        for key, name in (("rate", "set_sample_rate"), ("freq_hi", "set_freq_hi"), ("freq", "set_center_freq"),
                          ("txvga", "set_txvga_gain"), ("filter", "set_baseband_filter")):
            if reply.get(key) is not None:
                dongle.call(name, int(reply[key]))
        for key, name in (("amp", "set_amp_enable"), ("antenna", "set_antenna_enable")):
            if reply.get(key) is not None:
                dongle.call(name, int(bool(reply[key])))
        try:
            dongle.start_tx()
        except RtlError as e:
            conn.sendall(P.line({"ok": False, "error": str(e)}))
            self.note(f"{addr[0]}: couldn't transmit: {e}")
            return
        conn.sendall(P.line({"ok": True, "stream": "tx", "device": info}))
        session.freq, session.rate, session.gain = dongle.freq, dongle.rate, f"TX {dongle.txvga} dB"
        self.note(f"{addr[0]} TRANSMITTING on {dongle.freq / 1e6:.4f} MHz at {dongle.rate / 1e6:g} MS/s ({client})")
        conn.settimeout(None)
        try:
            while True:
                chunk = conn.recv(1 << 16)
                if not chunk:
                    break
                session.bytes += len(chunk)
                if not dongle.send(chunk, timeout=5):
                    break
        except OSError:
            pass
        session.closing = True
        dongle.stop()
        self.note(f"{addr[0]} stopped transmitting after {time.time() - session.started:.0f} s"
                  + (f" ({dongle.underruns} gaps: samples came too slowly)" if dongle.underruns else ""))

    def _command(self, dongle, session, gains, cmd, value):
        if cmd == P.SET_FREQ_HI:
            dongle.call("set_freq_hi", value)
            session.freq_hi = value
        elif cmd in (P.SET_LNA_GAIN, P.SET_VGA_GAIN):
            dongle.call("set_lna_gain" if cmd == P.SET_LNA_GAIN else "set_vga_gain", value)
            session.gain = f"LNA {dongle.lna} dB · VGA {dongle.vga} dB"
        elif cmd == P.SET_AMP:
            dongle.call("set_amp_enable", value)
        elif cmd == P.SET_ANTENNA_POWER:
            dongle.call("set_antenna_enable", value)
        elif cmd == P.SET_BASEBAND_FILTER:
            dongle.call("set_baseband_filter", value)
        elif cmd == P.SET_CLKOUT:
            dongle.call("set_clkout_enable", value)
        elif cmd == P.SET_TXVGA_GAIN:
            dongle.call("set_txvga_gain", value)
        elif cmd == P.SET_FREQ:
            dongle.call("set_center_freq", value)
            session.freq = (getattr(session, "freq_hi", 0) << 32) | value
            session.freq_hi = 0
        elif cmd == P.SET_RATE and getattr(dongle, "is_hackrf", False):
            # Below the HackRF's 2 MS/s: sample at a whole multiple and filter it down here.
            from .hackrf import MIN_RATE
            k = dsp.narrowing(value, MIN_RATE)
            if dongle.call("set_sample_rate", value * k) == 0:
                session.decimate = dsp.Decimator(k) if k > 1 else None
                session.rate, session.device_rate = value, value * k
        elif cmd == P.SET_DC_BLOCK:
            session.dc = dsp.DCBlock() if value and getattr(dongle, "is_hackrf", False) else None
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
