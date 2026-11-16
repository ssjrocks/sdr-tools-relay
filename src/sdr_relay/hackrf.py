"""The HackRF One, through libhackrf (libhackrf.so on Linux, hackrf.dll on Windows).

Same methods as rtlsdr.Dongle, so the server can serve either: rtl_tcp's commands are mapped onto
the HackRF (its gain onto the LNA and VGA, the bias tee onto antenna power, a frequency correction
in software), and its own controls have commands of their own (protocol.py). The calls follow
HackRF's own tools exactly, in their order: receiving as hackrf_transfer does (open, sample rate,
baseband filter if one is set, hardware sync off, frequency, amp and antenna power if set, VGA and
LNA gain, start), sweeping as hackrf_sweep does, transmitting as hackrf_transfer -t does. Nothing
else is sent to the device.

The HackRF sends signed 8-bit IQ; rtl_tcp's clients expect unsigned (offset by 128): flipping the
top bit of every byte turns one into the other. It samples at 2-20 MS/s; a client that wants less
asks for a whole multiple and filters it down itself (SDR Toolkit does).
"""

import ctypes
import ctypes.util
import logging
import os
import queue
import sys
import threading
from pathlib import Path

from .rtlsdr import RtlError

log = logging.getLogger(__name__)

TUNER = 100                     # in rtl_tcp's header: not an RTL tuner (0-6); clients look at the hello for what it is
MIN_RATE, MAX_RATE = 2_000_000, 20_000_000
MIN_FREQ, MAX_FREQ = 1_000_000, 6_000_000_000
LNA_STEPS = tuple(range(0, 41, 8))          # dB
VGA_STEPS = tuple(range(0, 63, 2))          # dB
TXVGA_MAX = 47
DEFAULT_LNA, DEFAULT_VGA = 16, 20           # hackrf_sweep's (hackrf_transfer: 8, 20); "automatic" gain
# rtl_tcp's gain list: every total LNA + VGA can make (0-102 dB in 2 dB steps), in tenths of a dB.
GAINS = tuple(range(0, 1021, 20))
BOARDS = {0: "Jellybean", 1: "Jawbreaker", 2: "HackRF One", 3: "rad1o", 4: "HackRF One (r9)", 0xFF: "unknown"}
BYTES_PER_BLOCK = 16384
SWEEP_STEP = 20_000_000         # hackrf_sweep's TUNE_STEP (20 MHz) and sample rate
SWEEP_OFFSET = 7_500_000
SWEEP_FILTER = 15_000_000
FLIP = bytes(range(128, 256)) + bytes(range(128))      # signed 8-bit <-> unsigned (xor 0x80)


class HackRFError(RtlError):
    """A HackRF problem: an RtlError, so the server reports it the same way."""


class Transfer(ctypes.Structure):
    _fields_ = [("device", ctypes.c_void_p), ("buffer", ctypes.POINTER(ctypes.c_ubyte)),
                ("buffer_length", ctypes.c_int), ("valid_length", ctypes.c_int),
                ("rx_ctx", ctypes.c_void_p), ("tx_ctx", ctypes.c_void_p)]


class DeviceList(ctypes.Structure):
    _fields_ = [("serial_numbers", ctypes.POINTER(ctypes.c_char_p)), ("usb_board_ids", ctypes.POINTER(ctypes.c_int)),
                ("usb_device_index", ctypes.POINTER(ctypes.c_int)), ("devicecount", ctypes.c_int),
                ("usb_devices", ctypes.POINTER(ctypes.c_void_p)), ("usb_devicecount", ctypes.c_int)]


class PartSerial(ctypes.Structure):
    _fields_ = [("part_id", ctypes.c_uint32 * 2), ("serial_no", ctypes.c_uint32 * 4)]


CALLBACK = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(Transfer))
_lib = None
_inited = False
_lock = threading.Lock()


def _load():
    global _lib
    if _lib is not None:
        return _lib
    if sys.platform == "win32":
        here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        for d in (here, here / "hackrf", Path(sys.executable).parent):
            if d.is_dir():
                os.add_dll_directory(str(d))
        names = ["hackrf.dll", "libhackrf.dll"]
    else:
        names = [ctypes.util.find_library("hackrf") or "", "libhackrf.so.0", "libhackrf.so"]
    errors = []
    for name in filter(None, names):
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError as e:
            errors.append(f"{name}: {e}")
    else:
        raise HackRFError("The HackRF library (libhackrf) isn't installed: " + "; ".join(errors))
    p, i, u8, u32 = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint8, ctypes.c_uint32
    sig = {
        "init": ([], i), "exit": ([], i), "device_list": ([], ctypes.POINTER(DeviceList)),
        "device_list_free": ([ctypes.POINTER(DeviceList)], None),
        "open_by_serial": ([ctypes.c_char_p, ctypes.POINTER(p)], i), "close": ([p], i),
        "start_rx": ([p, CALLBACK, p], i), "stop_rx": ([p], i), "start_tx": ([p, CALLBACK, p], i),
        "stop_tx": ([p], i), "is_streaming": ([p], i), "set_freq": ([p, ctypes.c_uint64], i),
        "set_sample_rate": ([p, ctypes.c_double], i), "set_sample_rate_manual": ([p, u32, u32], i),
        "set_baseband_filter_bandwidth": ([p, u32], i), "compute_baseband_filter_bw": ([u32], u32),
        "set_lna_gain": ([p, u32], i), "set_vga_gain": ([p, u32], i), "set_txvga_gain": ([p, u32], i),
        "set_amp_enable": ([p, u8], i), "set_antenna_enable": ([p, u8], i), "set_hw_sync_mode": ([p, u8], i),
        "set_clkout_enable": ([p, u8], i), "get_clkin_status": ([p, ctypes.POINTER(u8)], i),
        "board_id_read": ([p, ctypes.POINTER(u8)], i),
        "version_string_read": ([p, ctypes.c_char_p, u8], i),
        "usb_api_version_read": ([p, ctypes.POINTER(ctypes.c_uint16)], i),
        "board_partid_serialno_read": ([p, ctypes.POINTER(PartSerial)], i),
        "init_sweep": ([p, ctypes.POINTER(ctypes.c_uint16), i, u32, u32, u32, i], i),
        "start_rx_sweep": ([p, CALLBACK, p], i), "error_name": ([i], ctypes.c_char_p),
    }
    for name, (args, res) in sig.items():
        fn = getattr(lib, f"hackrf_{name}", None)
        if fn is None:
            continue                        # an older libhackrf (no clkout or sweep, say): that call says so
        fn.argtypes = args
        fn.restype = res
    _lib = lib
    return lib


def _init(lib):
    global _inited
    with _lock:
        if not _inited:
            r = lib.hackrf_init()
            if r != 0:
                raise HackRFError(f"libhackrf wouldn't start: {_err(lib, r)}")
            _inited = True


def _err(lib, code) -> str:
    try:
        name = lib.hackrf_error_name(code)
        return f"{name.decode(errors='replace')} ({code})" if name else str(code)
    except (AttributeError, OSError):
        return str(code)


def devices() -> list[dict]:
    """The HackRFs plugged in: index, serial, board."""
    lib = _load()
    _init(lib)
    lst = lib.hackrf_device_list()
    if not lst:
        return []
    out = []
    try:
        n = lst.contents.devicecount
        for k in range(n):
            serial = lst.contents.serial_numbers[k]
            board = lst.contents.usb_board_ids[k]
            out.append({"index": k, "serial": serial.decode(errors="replace") if serial else "",
                        "name": {0x604B: "Jawbreaker", 0x6089: "HackRF One", 0xCC15: "rad1o"}.get(board, "HackRF")})
    finally:
        lib.hackrf_device_list_free(lst)
    return out


def explain_open_error(text) -> str:
    if sys.platform == "win32":
        return (f"Couldn't open the HackRF ({text}). Close any other program using it (SDR#, SDR++, SDR Angel); "
                "Windows needs its WinUSB driver (a HackRF One installs it by itself; if not, Zadig does).")
    return (f"Couldn't open the HackRF ({text}). Close any other program using it; check its udev rule "
            "(the hackrf package installs one) and that it's plugged in.")


class HackRF:
    """One HackRF: receive (rtl_tcp style), sweep, or transmit."""

    is_hackrf = True

    def __init__(self, serial=""):
        self.lib = _load()
        _init(self.lib)
        self.dev = ctypes.c_void_p()
        r = self.lib.hackrf_open_by_serial(serial.encode() if serial else None, ctypes.byref(self.dev))
        if r != 0:
            raise HackRFError(explain_open_error(_err(self.lib, r)))
        self.serial = serial
        # What the next start uses (and the running stream has).
        self.rate = 2_048_000
        self.freq = 100_000_000
        self.freq_hi = 0                     # the upper 32 bits for the next frequency (protocol SET_FREQ_HI)
        self.ppm = 0
        self.lna, self.vga = DEFAULT_LNA, DEFAULT_VGA
        self.txvga = 0
        self.amp = None                      # None: never set (hackrf_transfer only sets it when asked)
        self.antenna = None
        self.filter_bw = 0                   # 0: the library's own (about 0.75 x the sample rate)
        self.clkout = None
        self.mode = None                     # "rx", "sweep", "tx" while streaming
        self.received = 0
        self.no_samples = False
        self.callback_error = ""
        self._cb = None
        self._on_data = None
        self._tx_queue: queue.Queue = queue.Queue(maxsize=64)
        self._tx_left = b""
        self.underruns = 0

    # ---- what clients see ---------------------------------------------------------------------

    @property
    def tuner(self) -> int:
        return TUNER

    def gains(self) -> list[int]:
        return list(GAINS)

    def info(self) -> dict:
        """Board, firmware, USB API, part ID and serial, clock input: for the hello and the window."""
        lib, d = self.lib, self.dev
        out = {"type": "hackrf", "min_rate": MIN_RATE, "max_rate": MAX_RATE, "min_freq": MIN_FREQ,
               "max_freq": MAX_FREQ, "lna_steps": list(LNA_STEPS), "vga_steps": list(VGA_STEPS),
               "txvga_max": TXVGA_MAX}
        b = ctypes.c_uint8()
        if lib.hackrf_board_id_read(d, ctypes.byref(b)) == 0:
            out["board"] = BOARDS.get(b.value, f"board {b.value}")
        buf = ctypes.create_string_buffer(255)
        if lib.hackrf_version_string_read(d, buf, 255) == 0:
            out["firmware"] = buf.value.decode(errors="replace")
        api = ctypes.c_uint16()
        if lib.hackrf_usb_api_version_read(d, ctypes.byref(api)) == 0:
            out["usb_api"] = f"{api.value >> 8:x}.{api.value & 0xFF:02x}"
        ps = PartSerial()
        if lib.hackrf_board_partid_serialno_read(d, ctypes.byref(ps)) == 0:
            out["part_id"] = "".join(f"{v:08x}" for v in ps.part_id)
            out["serial"] = "".join(f"{v:08x}" for v in ps.serial_no)
        if getattr(lib, "hackrf_get_clkin_status", None) is not None:
            st = ctypes.c_uint8()
            if lib.hackrf_get_clkin_status(d, ctypes.byref(st)) == 0:
                out["clkin"] = bool(st.value)
        return out

    # ---- settings (rtl_tcp's names, as the server calls them, and the HackRF's own) --------------

    def _tuned(self) -> int:
        return int(round(self.freq * (1 + self.ppm / 1e6)))      # the frequency correction, in software

    def call(self, name, *args) -> int:
        lib, d = self.lib, self.dev
        live = self.mode in ("rx", "tx")
        if name == "set_center_freq":
            f = (self.freq_hi << 32) | int(args[0])
            self.freq_hi = 0
            if not MIN_FREQ <= f <= MAX_FREQ:
                log.info("HackRF: %d Hz is outside 1 MHz-6 GHz, not tuned", f)
                return -1
            self.freq = f
            return lib.hackrf_set_freq(d, self._tuned()) if live else 0
        if name == "set_freq_hi":
            self.freq_hi = int(args[0])
            return 0
        if name == "set_sample_rate":
            rate = int(args[0])
            if not MIN_RATE <= rate <= MAX_RATE:
                log.info("HackRF: %d S/s is outside 2-20 MS/s, not set", rate)
                return -1
            self.rate = rate
            if self.mode == "rx":                # a new rate: stop, and start again as hackrf_transfer does
                self.stop()
                self.start(self._on_data)
            return 0
        if name == "set_freq_correction":
            self.ppm = int(args[0])
            return lib.hackrf_set_freq(d, self._tuned()) if live else 0
        if name == "set_tuner_gain_mode":
            if not args[0]:                      # "automatic": the HackRF has no AGC; hackrf_sweep's gains
                self.lna, self.vga = DEFAULT_LNA, DEFAULT_VGA
                return self._gains()
            return 0
        if name in ("set_tuner_gain", "set_gain_tenths"):
            self.lna, self.vga = split_gain(int(args[0]))
            return self._gains()
        if name == "set_lna_gain":
            self.lna = min(LNA_STEPS, key=lambda g: abs(g - int(args[0])))
            return self._gains()
        if name == "set_vga_gain":
            self.vga = min(VGA_STEPS, key=lambda g: abs(g - int(args[0])))
            return self._gains()
        if name == "set_txvga_gain":
            self.txvga = max(0, min(TXVGA_MAX, int(args[0])))
            return lib.hackrf_set_txvga_gain(d, self.txvga) if self.mode == "tx" else 0
        if name == "set_amp_enable":
            self.amp = 1 if args[0] else 0
            return lib.hackrf_set_amp_enable(d, self.amp) if self.mode else 0
        if name in ("set_bias_tee", "set_antenna_enable"):
            self.antenna = 1 if args[0] else 0
            return lib.hackrf_set_antenna_enable(d, self.antenna) if self.mode else 0
        if name == "set_baseband_filter":
            self.filter_bw = int(lib.hackrf_compute_baseband_filter_bw(int(args[0]))) if args[0] else 0
            if self.mode == "rx" and self.filter_bw:
                return lib.hackrf_set_baseband_filter_bandwidth(d, self.filter_bw)
            return 0
        if name == "set_clkout_enable":
            self.clkout = 1 if args[0] else 0
            fn = getattr(lib, "hackrf_set_clkout_enable", None)
            return fn(d, self.clkout) if fn is not None else -1
        # rtl_tcp's RTL-only settings (AGC, direct sampling, offset tuning, crystals, test mode, IF gain):
        # nothing on a HackRF.
        return 0

    def _gains(self) -> int:
        if self.mode not in ("rx", "sweep"):
            return 0
        r = self.lib.hackrf_set_vga_gain(self.dev, self.vga)
        r |= self.lib.hackrf_set_lna_gain(self.dev, self.lna)
        return r

    # ---- receiving -----------------------------------------------------------------------------

    def _callback(self, deliver):
        def cb(t):
            try:
                tr = t.contents
                n = tr.valid_length
                if n > 0:
                    self.received += n
                    deliver(ctypes.string_at(tr.buffer, n))
            except Exception as e:  # a windowed app has no stderr: keep it for the log
                if not self.callback_error:
                    self.callback_error = repr(e)
                    log.exception("HackRF callback")
            return 0
        return CALLBACK(cb)

    def start(self, on_data, buf_len=None, method=None):
        """Receive into on_data(bytes) as rtl_tcp's unsigned 8-bit IQ, as hackrf_transfer -r starts."""
        lib, d = self.lib, self.dev
        self._on_data = on_data
        self.no_samples = False
        self.received = 0
        self._check(lib.hackrf_set_sample_rate(d, float(self.rate)), "set the sample rate")
        if self.filter_bw:
            self._check(lib.hackrf_set_baseband_filter_bandwidth(d, self.filter_bw), "set the filter")
        self._check(lib.hackrf_set_hw_sync_mode(d, 0), "turn hardware sync off")
        self._check(lib.hackrf_set_freq(d, self._tuned()), "tune")
        if self.amp is not None:
            self._check(lib.hackrf_set_amp_enable(d, self.amp), "set the RF amp")
        if self.antenna is not None:
            self._check(lib.hackrf_set_antenna_enable(d, self.antenna), "set antenna power")
        r = lib.hackrf_set_vga_gain(d, self.vga)
        r |= lib.hackrf_set_lna_gain(d, self.lna)
        self._cb = self._callback(lambda data: on_data(data.translate(FLIP)))
        r |= lib.hackrf_start_rx(d, self._cb, None)
        self._check(r, "start receiving")
        self.mode = "rx"
        threading.Thread(target=self._watch, daemon=True, name="hackrf-watchdog").start()

    def _watch(self):
        start = self.received
        threading.Event().wait(3.0)
        if self.mode == "rx" and self.received == start:
            self.no_samples = True
            log.warning("no samples from the HackRF in 3 s (callback error: %s)", self.callback_error or "none")

    def start_sweep(self, on_block, ranges_mhz, lna=DEFAULT_LNA, vga=DEFAULT_VGA):
        """hackrf_sweep: the HackRF hops 20 MHz at a time across ranges_mhz ([(lo, hi), ...], up to 10) and
        sends 16384-byte blocks, each 0x7F 0x7F, the frequency (u64, little-endian), then signed 8-bit IQ.
        on_block gets the transfers as they are (the client does the FFTs)."""
        lib, d = self.lib, self.dev
        if getattr(lib, "hackrf_init_sweep", None) is None:
            raise HackRFError("this libhackrf can't sweep")
        freqs = []
        for lo, hi in ranges_mhz[:10]:
            lo = int(lo)
            steps = 1 + (int(hi) - lo - 1) // (SWEEP_STEP // 1_000_000)
            freqs += [lo, lo + steps * (SWEEP_STEP // 1_000_000)]
        arr = (ctypes.c_uint16 * len(freqs))(*freqs)
        self.lna, self.vga = lna, vga
        self._check(lib.hackrf_set_sample_rate_manual(d, SWEEP_STEP, 1), "set the sample rate")
        self._check(lib.hackrf_set_baseband_filter_bandwidth(d, SWEEP_FILTER), "set the filter")
        r = lib.hackrf_set_vga_gain(d, self.vga)
        r |= lib.hackrf_set_lna_gain(d, self.lna)
        self._check(lib.hackrf_init_sweep(d, arr, len(freqs) // 2, BYTES_PER_BLOCK, SWEEP_STEP, SWEEP_OFFSET, 1),
                    "set up the sweep")
        self._cb = self._callback(on_block)
        self._check(lib.hackrf_start_rx_sweep(d, self._cb, None), "start sweeping")
        if self.amp is not None:
            self._check(lib.hackrf_set_amp_enable(d, self.amp), "set the RF amp")
        if self.antenna is not None:
            self._check(lib.hackrf_set_antenna_enable(d, self.antenna), "set antenna power")
        self.mode = "sweep"
        return freqs

    # ---- transmitting -----------------------------------------------------------------------------

    def start_tx(self):
        """Transmit what send() is given (signed 8-bit IQ), as hackrf_transfer -t starts: sample rate,
        filter, hardware sync off, frequency, amp and antenna power if set, TX VGA gain, start."""
        lib, d = self.lib, self.dev
        self._check(lib.hackrf_set_sample_rate(d, float(self.rate)), "set the sample rate")
        if self.filter_bw:
            self._check(lib.hackrf_set_baseband_filter_bandwidth(d, self.filter_bw), "set the filter")
        self._check(lib.hackrf_set_hw_sync_mode(d, 0), "turn hardware sync off")
        self._check(lib.hackrf_set_freq(d, self._tuned()), "tune")
        if self.amp is not None:
            self._check(lib.hackrf_set_amp_enable(d, self.amp), "set the RF amp")
        if self.antenna is not None:
            self._check(lib.hackrf_set_antenna_enable(d, self.antenna), "set antenna power")
        r = lib.hackrf_set_txvga_gain(d, self.txvga)

        def cb(t):
            try:
                tr = t.contents
                n = tr.buffer_length
                data = self._tx_left
                while len(data) < n:
                    try:
                        data += self._tx_queue.get_nowait()
                    except queue.Empty:
                        break
                if len(data) < n:                 # nothing more yet: send silence rather than stop
                    self.underruns += 1
                    data += bytes(n - len(data))
                ctypes.memmove(tr.buffer, data[:n], n)
                tr.valid_length = n
                self._tx_left = data[n:]
            except Exception as e:  # keep it for the log
                if not self.callback_error:
                    self.callback_error = repr(e)
                    log.exception("HackRF TX callback")
            return 0
        self._cb = CALLBACK(cb)
        r |= lib.hackrf_start_tx(d, self._cb, None)
        self._check(r, "start transmitting")
        self.mode = "tx"

    def send(self, data: bytes, timeout=1.0) -> bool:
        """Queue signed 8-bit IQ to transmit; False if the queue stayed full."""
        try:
            self._tx_queue.put(data, timeout=timeout)
            return True
        except queue.Full:
            return False

    # ---- stopping ---------------------------------------------------------------------------------

    def _check(self, r, what):
        if r != 0:
            raise HackRFError(f"The HackRF couldn't {what}: {_err(self.lib, r)}")

    def stop(self):
        mode, self.mode = self.mode, None
        if mode in ("rx", "sweep"):
            self.lib.hackrf_stop_rx(self.dev)
        elif mode == "tx":
            self.lib.hackrf_stop_tx(self.dev)

    def close(self):
        self.stop()
        self.lib.hackrf_close(self.dev)


def split_gain(tenths: int) -> tuple[int, int]:
    """rtl_tcp's total gain (tenths of a dB) as the HackRF's LNA (0-40, 8 dB steps) and VGA (0-62, 2 dB):
    the LNA first, as much as fits below the total, so the front end has the gain (less noise)."""
    total = max(0, min(102, round(tenths / 10)))
    lna = max(g for g in LNA_STEPS if g <= total)
    vga = min(VGA_STEPS, key=lambda g: abs(g - (total - lna)))
    return lna, vga
