"""A dongle check a client with the password can ask for ({"diagnose": true}).

It reads samples for 2 seconds at 1090 MHz, 2.4 MS/s with the RTL-SDR Blog's own rtl_sdr
program (bundled on Windows), in its async and -S sync modes, then with librtlsdr's
rtlsdr_read_async through this app. If rtl_sdr gets samples and the app doesn't, the
problem is in the relay; if neither does, it's the dongle, its USB connection or driver.
rtl_sdr goes first, and is killed if it hangs, which frees the dongle; this app's sync read
isn't tried, because it can't be interrupted when no samples come.
"""

import logging
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .rtlsdr import Dongle, RtlError, devices

log = logging.getLogger(__name__)
FREQ, RATE, SECONDS = 1090000000, 2400000, 2.0


def rtl_sdr_exe() -> str | None:
    here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for p in (here / "rtlsdr" / "rtl_sdr.exe", here / "rtl_sdr.exe"):
        if p.is_file():
            return str(p)
    return shutil.which("rtl_sdr")


def through_librtlsdr(index, method) -> dict:
    try:
        d = Dongle(index)
    except RtlError as e:
        return {"opened": False, "error": str(e)}
    out = {"opened": True, "tuner": d.tuner}
    try:
        out["set_rate"] = d.call("set_sample_rate", RATE)
        out["set_freq"] = d.call("set_center_freq", FREQ)
        out["set_gain_mode"] = d.call("set_tuner_gain_mode", 0)
        got = []
        d.start(lambda data: got.append(len(data)), method=method)
        time.sleep(SECONDS)
        out.update(bytes=d.received, buffers=len(got), result=d.last_result, method_used=d.method,
                   callback_error=d.callback_error)
        d.stop()
    finally:
        try:
            d.close()
        except RtlError as e:
            out["close"] = str(e)
    return out


def through_rtl_sdr(index, sync) -> dict:
    exe = rtl_sdr_exe()
    if exe is None:
        return {"skipped": "rtl_sdr not available"}
    target = Path(tempfile.mkdtemp(prefix="sdr-relay-check-")) / "samples.bin"
    cmd = [exe, "-d", str(index), "-f", str(FREQ), "-s", str(RATE), "-n", str(int(RATE * SECONDS))]
    if sync:
        cmd.append("-S")
    flags = 0x08000000 if sys.platform == "win32" else 0         # CREATE_NO_WINDOW
    try:
        r = subprocess.run(cmd + [str(target)], capture_output=True, text=True, timeout=20, creationflags=flags)
        code, err = r.returncode, r.stderr
    except subprocess.TimeoutExpired as e:
        code, err = "timed out", (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
    size = target.stat().st_size if target.exists() else 0
    target.unlink(missing_ok=True)
    return {"exit": code, "bytes": size, "output": "\n".join(err.strip().splitlines()[-6:])}


def run(index=0) -> dict:
    report = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "platform": sys.platform}
    try:
        report["devices"] = devices()
    except RtlError as e:
        report["devices"] = str(e)
    for name, fn in (("rtl_sdr", lambda: through_rtl_sdr(index, False)),
                     ("rtl_sdr -S", lambda: through_rtl_sdr(index, True)),
                     ("librtlsdr async", lambda: through_librtlsdr(index, "async"))):
        try:
            report[name] = fn()
        except Exception as e:  # part of the report
            log.exception("dongle check %s", name)
            report[name] = {"error": repr(e)}
        log.info("dongle check %s: %s", name, report[name])
    return report
