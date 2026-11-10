"""--self-test: checks a build works on this PC without a dongle or a window (used by the Windows CI).

Loads librtlsdr and lists dongles (none is fine), then serves a made-up recording on a
free port, connects to it as a client would, checks the handshake, a command and the
samples, and writes the results as JSON to the given file. Exit code 0 when all passed.
"""

import json
import socket
import sys
import tempfile
import time
import traceback
from pathlib import Path


def run(out_path, driver_test=False) -> int:
    results = {}
    try:
        from . import protocol as P
        from .rtlsdr import devices
        from .server import Config, Relay
        try:
            results["dongles"] = devices()
            results["librtlsdr"] = True
        except Exception as e:
            results["librtlsdr"] = f"failed: {e}"
        rec = Path(tempfile.mkdtemp()) / "test.cu8"
        rec.write_bytes(bytes(range(256)) * 1024)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        relay = Relay(Config(name="self-test", port=port, password="pw", file=str(rec)), version="test")
        relay.start()
        try:
            c = socket.create_connection(("127.0.0.1", port), timeout=10)
            buf = b""
            while not buf.endswith(b"\n"):
                buf += c.recv(1)
            hello = json.loads(buf)
            c.sendall(P.line({"client": "self-test", "auth": P.auth("pw", hello["nonce"])})
                      + P.command(P.SET_RATE, 250000) + P.command(P.SET_FREQ, 433920000))
            buf = b""
            while not buf.endswith(b"\n"):
                buf += c.recv(1)
            results["auth"] = json.loads(buf).get("ok") is True
            data = b""
            end = time.time() + 10
            while len(data) < 12 + 100000 and time.time() < end:
                data += c.recv(65536)
            results["header"] = data[:4] == b"RTL0"
            results["samples"] = bytes(range(256)) in data
            c.close()
        finally:
            relay.stop()
        if driver_test:
            # Pre-installs WinUSB for a dongle that isn't plugged in (GitHub's runner has none).
            from . import driver
            try:
                results["driver"] = driver.install([driver.Device(0x0BDA, 0x2838, 0, "test", "", "")])
            except OSError as e:
                results["driver"] = f"failed: {e}"
    except Exception:
        results["error"] = traceback.format_exc()
    ok = results.get("librtlsdr") is True and all(results.get(k) for k in ("auth", "header", "samples"))
    if driver_test:
        ok = ok and str(results.get("driver", "")).startswith("Driver installed")
    results["ok"] = ok
    Path(out_path).write_text(json.dumps(results, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1]))
