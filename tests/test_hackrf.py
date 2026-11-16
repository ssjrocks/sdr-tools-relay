"""The HackRF backend against a stand-in libhackrf: the calls it makes (exactly hackrf_transfer's and
hackrf_sweep's, in their order), the samples it passes on, and the relay serving it."""

import ctypes
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src")]

from sdr_relay import hackrf as H  # noqa: E402
from sdr_relay import protocol as P  # noqa: E402
from sdr_relay.server import Config, Relay  # noqa: E402
from test_relay import free_port, read_exact, read_line  # noqa: E402


class FakeLib:
    """libhackrf, recording each call (name and arguments); start_rx and friends call the callback with
    transfers of `pattern` from a thread, as the library's own thread does."""

    def __init__(self, pattern=bytes([0x00, 0x7F, 0x80, 0xFF]) * 4096):
        self.calls: list[tuple] = []
        self.pattern = pattern
        self._halt = threading.Event()
        self.tx_out = bytearray()

    def __getattr__(self, name):
        if not name.startswith("hackrf_"):
            raise AttributeError(name)

        def fn(*args):
            given = args[1:] if args and isinstance(args[0], ctypes.c_void_p) else args    # not the device
            self.calls.append((name[7:],) + tuple(self._plain(a) for a in given))
            if name in ("hackrf_start_rx", "hackrf_start_rx_sweep"):
                self._run(args[1], False)
            elif name == "hackrf_start_tx":
                self._run(args[1], True)
            elif name in ("hackrf_stop_rx", "hackrf_stop_tx"):
                self._halt.set()
            elif name == "hackrf_compute_baseband_filter_bw":
                return min((b for b in (1750000, 2500000, 3500000, 5000000, 5500000, 6000000, 7000000, 8000000,
                                        9000000, 10000000, 12000000, 14000000, 15000000, 20000000, 24000000,
                                        28000000) if b >= args[0]), default=28000000)
            return 0
        return fn

    @staticmethod
    def _plain(a):
        if isinstance(a, (int, float, str, bytes)) or a is None:
            return a
        if isinstance(a, ctypes.Array):
            return list(a)
        return type(a).__name__

    def _run(self, cb, tx):
        self._halt.clear()

        def run():
            buf = (ctypes.c_ubyte * len(self.pattern)).from_buffer_copy(self.pattern)
            while not self._halt.is_set():
                t = H.Transfer(None, ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), len(self.pattern),
                               0 if tx else len(self.pattern), None, None)
                cb(ctypes.pointer(t))
                if tx:
                    self.tx_out += ctypes.string_at(t.buffer, t.valid_length)
                time.sleep(0.01)
        threading.Thread(target=run, daemon=True).start()


@pytest.fixture
def lib(monkeypatch):
    fake = FakeLib()
    monkeypatch.setattr(H, "_lib", fake)
    monkeypatch.setattr(H, "_inited", True)
    return fake


def names(calls, start=0):
    return [c[0] for c in calls[start:]]


def test_receiving_makes_hackrf_transfers_calls_in_its_order(lib):
    d = H.HackRF("abc")
    assert lib.calls[0][:2] == ("open_by_serial", b"abc")
    n = len(lib.calls)
    got = []
    d.start(got.append)                                        # nothing optional set: only what it always does
    assert names(lib.calls, n) == ["set_sample_rate", "set_hw_sync_mode", "set_freq", "set_vga_gain",
                                   "set_lna_gain", "start_rx"]
    assert lib.calls[n] == ("set_sample_rate", 2048000.0) and lib.calls[n + 1] == ("set_hw_sync_mode", 0)
    time.sleep(0.05)
    d.stop()
    assert got and got[0][:4] == bytes([0x80, 0xFF, 0x00, 0x7F])   # signed 8-bit IQ as rtl_tcp's unsigned
    # With the filter, amp and antenna power set: where hackrf_transfer puts them.
    d.call("set_baseband_filter", 5000000)
    d.call("set_amp_enable", 1)
    d.call("set_bias_tee", 1)
    n = len(lib.calls)
    d.start(got.append)
    assert names(lib.calls, n) == ["set_sample_rate", "set_baseband_filter_bandwidth", "set_hw_sync_mode", "set_freq",
                                   "set_amp_enable", "set_antenna_enable", "set_vga_gain", "set_lna_gain", "start_rx"]
    d.close()
    assert names(lib.calls)[-2:] == ["stop_rx", "close"]


def test_settings_while_receiving(lib):
    d = H.HackRF()
    d.start(lambda data: None)
    n = len(lib.calls)
    d.call("set_freq_hi", 1)                                   # 1 << 32 + 1 GHz = 5.29 GHz
    d.call("set_center_freq", 1_000_000_000)
    assert lib.calls[n:] == [("set_freq", 5_294_967_296)]
    d.call("set_tuner_gain", 400)                              # rtl_tcp's 40.0 dB: all LNA
    assert lib.calls[-2:] == [("set_vga_gain", 0), ("set_lna_gain", 40)]
    d.call("set_tuner_gain", 570)                              # 57 dB: LNA 40 + VGA 17 -> 16 or 18
    assert (d.lna, d.vga) in ((40, 16), (40, 18))
    d.call("set_tuner_gain_mode", 0)                           # "automatic": hackrf_sweep's gains
    assert (d.lna, d.vga) == (16, 20)
    d.call("set_freq_correction", 10)                          # 10 ppm, in software
    assert lib.calls[-1] == ("set_freq", round(5_294_967_296 * (1 + 10e-6)))
    d.call("set_center_freq", 7_000_000_000 & 0xFFFFFFFF)      # out of range: refused, nothing sent
    n = len(lib.calls)
    d.call("set_freq_hi", 1)
    assert d.call("set_center_freq", 3_000_000_000) == -1 and len(lib.calls) == n
    n = len(lib.calls)
    d.call("set_sample_rate", 10_000_000)                      # a new rate: stop, then start again in order
    assert names(lib.calls, n) == ["stop_rx", "set_sample_rate", "set_hw_sync_mode", "set_freq", "set_vga_gain",
                                   "set_lna_gain", "start_rx"]
    assert d.call("set_sample_rate", 1_000_000) == -1          # below 2 MS/s: the client filters down
    for rtl_only in ("set_agc_mode", "set_direct_sampling", "set_offset_tuning", "set_testmode"):
        n = len(lib.calls)
        assert d.call(rtl_only, 1) == 0 and len(lib.calls) == n
    d.close()


def test_sweep_makes_hackrf_sweeps_calls_in_its_order(lib):
    d = H.HackRF()
    blocks = []
    freqs = d.start_sweep(blocks.append, [(100, 105), (400, 480)])
    assert freqs == [100, 120, 400, 480]                       # rounded up to 20 MHz steps, as hackrf_sweep
    assert names(lib.calls)[1:] == ["set_sample_rate_manual", "set_baseband_filter_bandwidth", "set_vga_gain",
                                    "set_lna_gain", "init_sweep", "start_rx_sweep"]
    assert lib.calls[1] == ("set_sample_rate_manual", 20_000_000, 1)
    assert lib.calls[2] == ("set_baseband_filter_bandwidth", 15_000_000)
    assert lib.calls[5] == ("init_sweep", [100, 120, 400, 480], 2, 16384, 20_000_000, 7_500_000, 1)
    time.sleep(0.05)
    d.close()
    assert blocks and blocks[0][:4] == bytes([0x00, 0x7F, 0x80, 0xFF])      # raw, as the library gives them


def test_transmit_makes_hackrf_transfer_t_calls_and_sends_what_it_gets(lib):
    d = H.HackRF()
    d.call("set_sample_rate", 4_000_000)
    d.call("set_center_freq", 433_920_000)
    d.call("set_txvga_gain", 20)
    d.start_tx()
    assert names(lib.calls)[1:] == ["set_sample_rate", "set_hw_sync_mode", "set_freq", "set_txvga_gain", "start_tx"]
    d.send(bytes([1, 2, 3, 4]) * 8192)
    time.sleep(0.1)
    d.close()
    out = bytes(lib.tx_out)
    assert bytes([1, 2, 3, 4]) * 2 in out and d.underruns > 0          # silence while nothing was queued


def test_gain_split():
    assert H.split_gain(0) == (0, 0) and H.split_gain(1020) == (40, 62)
    assert H.split_gain(250) == (24, 0) or H.split_gain(250) == (24, 2)
    assert all(lna in H.LNA_STEPS and vga in H.VGA_STEPS for lna, vga in map(H.split_gain, H.GAINS))


# ---- the relay serving it ----------------------------------------------------------------------------

@pytest.fixture
def hackrf_relay(lib):
    opened = []

    def open_dongle(device=None):
        d = H.HackRF((device or "hackrf:abc").partition(":")[2])
        opened.append(d)
        return d
    r = Relay(Config(name="HackRF relay", port=free_port(), password="secret", device="hackrf:abc"),
              version="9.9", open_dongle=open_dongle)
    r.opened = opened
    r.start()
    yield r
    r.stop()


def handshake(relay, **extra):
    s = socket.create_connection(("127.0.0.1", relay.config.port), timeout=5)
    hello = read_line(s)
    s.sendall(P.line(dict({"client": "test", "version": "1", "auth": P.auth("secret", hello["nonce"])}, **extra)))
    return s, hello, read_line(s)


def test_relay_streams_a_hackrf_like_an_rtl_sdr(hackrf_relay, lib):
    s, hello, ok = handshake(hackrf_relay, device="hackrf:abc")
    assert "hackrf" in hello["features"] and "sweep" in hello["features"] and "tx" not in hello["features"]
    assert ok["ok"] and ok["device"]["type"] == "hackrf" and ok["device"]["max_freq"] == 6_000_000_000
    head = read_exact(s, 12)
    assert head[:4] == b"RTL0" and struct.unpack(">II", head[4:]) == (100, 52)
    s.sendall(P.command(P.SET_RATE, 10_000_000) + P.command(P.SET_FREQ_HI, 1) + P.command(P.SET_FREQ, 1_000_000_000)
              + P.command(P.SET_LNA_GAIN, 24) + P.command(P.SET_VGA_GAIN, 30) + P.command(P.SET_AMP, 1)
              + P.command(P.SET_ANTENNA_POWER, 1) + P.command(P.SET_BASEBAND_FILTER, 8_000_000))
    data = read_exact(s, 20000)
    assert bytes([0x80, 0xFF, 0x00, 0x7F]) * 4 in data
    time.sleep(0.3)
    calls = lib.calls
    assert ("set_freq", 5_294_967_296) in calls and ("set_lna_gain", 24) in calls and ("set_vga_gain", 30) in calls
    assert ("set_amp_enable", 1) in calls and ("set_antenna_enable", 1) in calls
    assert ("set_baseband_filter_bandwidth", 8_000_000) in calls and ("set_sample_rate", 10_000_000.0) in calls
    sess = hackrf_relay.session
    assert sess.freq == 5_294_967_296 and sess.gain == "LNA 24 dB · VGA 30 dB"
    s.close()


def test_relay_sweeps(hackrf_relay, lib):
    s, _, ok = handshake(hackrf_relay, stream="sweep", ranges=[[2400, 2500]], lna=24, vga=16)
    assert ok["ok"] and ok["stream"] == "sweep" and ok["ranges"] == [2400, 2500]
    assert read_exact(s, 16384)[:4] == bytes([0x00, 0x7F, 0x80, 0xFF])
    s.close()


def test_relay_transmits_only_when_allowed(hackrf_relay, lib):
    s, hello, ok = handshake(hackrf_relay, stream="tx", freq=433_920_000, rate=4_000_000, txvga=10)
    assert not ok["ok"] and "doesn't allow transmitting" in ok["error"]
    assert "start_tx" not in names(lib.calls)
    hackrf_relay.config.allow_tx = True
    s, hello, ok = handshake(hackrf_relay, stream="tx", freq=433_920_000, rate=4_000_000, txvga=10)
    assert "tx" in hello["features"] and ok["ok"] and ok["stream"] == "tx"
    s.sendall(bytes([9, 8, 7, 6]) * 16384)
    time.sleep(0.3)
    s.close()
    time.sleep(0.3)
    assert ("set_freq", 433_920_000) in lib.calls and ("set_txvga_gain", 10) in lib.calls
    assert bytes([9, 8, 7, 6]) * 2 in bytes(lib.tx_out)
    assert any("TRANSMITTING" in text for _, text in hackrf_relay.events)
