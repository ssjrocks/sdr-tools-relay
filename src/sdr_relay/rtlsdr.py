"""The RTL-SDR, through librtlsdr (librtlsdr.so on Linux, rtlsdr.dll on Windows), and a stand-in.

Only what the rtl_tcp protocol needs. ``Dongle`` and ``FileDongle`` have the same
methods, so the server doesn't care which it has: ``FileDongle`` plays an IQ
recording (unsigned 8-bit, as rtl_sdr writes) for tests and demonstrations.
"""

import ctypes
import ctypes.util
import logging
import os
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

TUNERS = {0: "unknown", 1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580", 5: "R820T", 6: "R828D"}
_lib = None


class RtlError(RuntimeError):
    pass


def _load():
    global _lib
    if _lib is not None:
        return _lib
    names = []
    if sys.platform == "win32":
        here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        for d in (here, here / "rtlsdr", Path(sys.executable).parent):
            if d.is_dir():
                os.add_dll_directory(str(d))
        names = ["rtlsdr.dll", "librtlsdr.dll"]
    else:
        names = [ctypes.util.find_library("rtlsdr") or "", "librtlsdr.so.2", "librtlsdr.so.0", "librtlsdr.so"]
    errors = []
    for name in filter(None, names):
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError as e:
            errors.append(f"{name}: {e}")
    else:
        raise RtlError("The RTL-SDR library (librtlsdr) isn't installed: " + "; ".join(errors))
    p = ctypes.c_void_p
    lib.rtlsdr_get_device_count.restype = ctypes.c_uint32
    lib.rtlsdr_get_device_name.restype = ctypes.c_char_p
    lib.rtlsdr_get_device_name.argtypes = [ctypes.c_uint32]
    lib.rtlsdr_get_device_usb_strings.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.rtlsdr_open.argtypes = [ctypes.POINTER(p), ctypes.c_uint32]
    for name, args in (("close", []), ("reset_buffer", []), ("cancel_async", []), ("get_tuner_type", []),
                       ("set_center_freq", [ctypes.c_uint32]), ("set_sample_rate", [ctypes.c_uint32]),
                       ("set_freq_correction", [ctypes.c_int]), ("set_tuner_gain_mode", [ctypes.c_int]),
                       ("set_tuner_gain", [ctypes.c_int]), ("set_tuner_if_gain", [ctypes.c_int, ctypes.c_int]),
                       ("set_testmode", [ctypes.c_int]), ("set_agc_mode", [ctypes.c_int]),
                       ("set_direct_sampling", [ctypes.c_int]), ("set_offset_tuning", [ctypes.c_int]),
                       ("set_xtal_freq", [ctypes.c_uint32, ctypes.c_uint32]),
                       ("get_tuner_gains", [ctypes.POINTER(ctypes.c_int)])):
        fn = getattr(lib, f"rtlsdr_{name}")
        fn.argtypes = [p] + args
        fn.restype = ctypes.c_int
    if hasattr(lib, "rtlsdr_set_bias_tee"):            # not in every librtlsdr
        lib.rtlsdr_set_bias_tee.argtypes = [p, ctypes.c_int]
    _lib = lib
    return lib


READ_CB = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint32, ctypes.c_void_p)


def devices() -> list[dict]:
    """The RTL-SDR dongles plugged in: index, name, manufacturer, product, serial."""
    lib = _load()
    out = []
    for i in range(lib.rtlsdr_get_device_count()):
        m, pr, s = (ctypes.create_string_buffer(256) for _ in range(3))
        lib.rtlsdr_get_device_usb_strings(i, m, pr, s)
        out.append({"index": i, "name": (lib.rtlsdr_get_device_name(i) or b"").decode(errors="replace"),
                    "manufacturer": m.value.decode(errors="replace"), "product": pr.value.decode(errors="replace"),
                    "serial": s.value.decode(errors="replace")})
    return out


def explain_open_error(code: int) -> str:
    if sys.platform == "win32":
        return (f"Couldn't open the RTL-SDR (error {code}). Close any other program using it (SDR#, SDR++). "
                "If it's newly plugged in, press Refresh in the relay's window: it offers to install the "
                "WinUSB driver it needs.")
    return (f"Couldn't open the RTL-SDR (error {code}). Close any other program using it (SDR++, GQRX, a "
            "readsb or rtl_tcp service). If the kernel's DVB driver holds it, blacklist dvb_usb_rtl28xxu.")


class Dongle:
    def __init__(self, index=0):
        self.lib = _load()
        self.dev = ctypes.c_void_p()
        r = self.lib.rtlsdr_open(ctypes.byref(self.dev), index)
        if r < 0:
            raise RtlError(explain_open_error(r))
        self.index = index
        self._thread = None
        self._cb = None

    @property
    def tuner(self) -> int:
        return self.lib.rtlsdr_get_tuner_type(self.dev)

    def gains(self) -> list[int]:
        n = self.lib.rtlsdr_get_tuner_gains(self.dev, None)
        if n <= 0:
            return []
        arr = (ctypes.c_int * n)()
        self.lib.rtlsdr_get_tuner_gains(self.dev, arr)
        return list(arr)

    def call(self, name, *args) -> int:
        """Run rtlsdr_<name>(dev, *args); bias tee only if this librtlsdr has it."""
        fn = getattr(self.lib, f"rtlsdr_{name}", None)
        if fn is None:
            return -1
        return fn(self.dev, *args)

    def start(self, on_data, buf_len=16384 * 4):
        """Stream into *on_data(bytes)* from a thread until stop()."""
        self.call("reset_buffer")

        def cb(buf, n, _ctx):
            on_data(ctypes.string_at(buf, n))

        self._cb = READ_CB(cb)
        self._thread = threading.Thread(target=self.lib.rtlsdr_read_async,
                                        args=(self.dev, self._cb, None, 0, buf_len), daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread is not None:
            self.lib.rtlsdr_cancel_async(self.dev)
            self._thread.join(3)
            self._thread = None

    def close(self):
        self.stop()
        self.lib.rtlsdr_close(self.dev)


class FileDongle:
    """Plays a .cu8 recording in real time at the sample rate the client sets, looping."""

    def __init__(self, path, tuner=5):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        if len(self.data) < 2:
            raise RtlError("the recording is empty")
        self.tuner = tuner
        self.rate = 2048000
        self.calls: list[tuple] = []           # every setting made, for tests
        self._halt = threading.Event()
        self._thread = None

    def gains(self):
        return [0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254, 280, 297, 328, 338, 364,
                372, 386, 402, 421, 434, 439, 445, 480, 496]

    def call(self, name, *args):
        self.calls.append((name,) + args)
        if name == "set_sample_rate":
            self.rate = args[0]
        return 0

    def start(self, on_data, buf_len=16384 * 4):
        self._halt.clear()

        def run():
            pos, t0, sent = 0, time.monotonic(), 0
            while not self._halt.is_set():
                chunk = self.data[pos:pos + buf_len]
                if len(chunk) < buf_len:
                    chunk += self.data[: buf_len - len(chunk)]
                pos = (pos + buf_len) % len(self.data)
                on_data(chunk)
                sent += buf_len // 2
                wait = t0 + sent / max(self.rate, 1) - time.monotonic()
                if wait > 0:
                    self._halt.wait(wait)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self):
        self._halt.set()
        if self._thread is not None:
            self._thread.join(3)
            self._thread = None

    def close(self):
        self.stop()
