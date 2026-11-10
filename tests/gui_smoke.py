"""Headless window check: QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py [screenshot.png]"""

import os
import socket
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
errors = []
sys.excepthook = lambda *a: (errors.append(a), traceback.print_exception(*a))
home = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = os.path.join(home, "config")       # QSettings and autostart go here
os.environ["XDG_STATE_HOME"] = os.path.join(home, "state")

from PyQt6.QtCore import QSettings  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from sdr_relay import appreports, gui, protocol as P, updater  # noqa: E402

appreports.send_report = lambda *a, **k: None
updater.due = lambda: False                      # no update check from the test
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
s = socket.socket()
s.bind(("127.0.0.1", 0))
port = s.getsockname()[1]
s.close()
pre = QSettings("ssjrocks", "sdr-tools-relay")
pre.setValue("port", port)
pre.setValue("password", "pw")
pre.sync()
rec = Path(home) / "demo.cu8"
rec.write_bytes(bytes(range(256)) * 2048)

app = QApplication([])
win = gui.Window(demo_file=str(rec))
app.processEvents()
results = {"ready": win.state.text().startswith("Ready") and win.device.count() == 1 and "Password set" in win.detail.text()}

c = socket.create_connection(("127.0.0.1", port), timeout=5)
hello = b""
while not hello.endswith(b"\n"):
    hello += c.recv(1)
import json  # noqa: E402
c.sendall(P.line({"client": "smoke", "auth": P.auth("pw", json.loads(hello)["nonce"])}))
c.sendall(P.command(P.SET_FREQ, 1090000000) + P.command(P.SET_RATE, 2400000))
c.settimeout(5)
got = 0
end = time.time() + 3
while time.time() < end:
    got += len(c.recv(65536))
    app.processEvents()
win.refresh()
results["in_use"] = win.state.text() == "In use by 127.0.0.1" and "1090.0000 MHz" in win.detail.text() \
    and win.disconnect_btn.isEnabled() and got > 1_000_000
win.disconnect_btn.click()
for _ in range(30):
    app.processEvents()
    time.sleep(0.1)
    if win.relay.session is None:
        break
win.refresh()
results["disconnected"] = win.relay.session is None and any("disconnected" in win.events.item(i).text()
                                                           for i in range(win.events.count()))
# Settings are kept; a new port moves the server.
win.name.setText("Shed dongle")
win.apply()
results["saved"] = QSettings("ssjrocks", "sdr-tools-relay").value("name") == "Shed dongle" and \
    win.relay.hello()["name"] == "Shed dongle"
win.autostart.setChecked(True)
results["autostart"] = (Path(home) / "config/autostart/sdr-tools-relay.desktop").exists()
win.autostart.setChecked(False)
# Update asked for by a client with the password: the window installs it and restarts.
installed, restarted = [], []
updater.check = lambda: updater.Update("9.9.9", "https://api.github.com/repos/ssjrocks/sdr-tools-relay/releases/assets/1",
                                      "x.whl", "0" * 64)
updater.can_install = lambda: True
updater.install = lambda u: installed.append(u.version)
updater.restart = lambda: restarted.append(1)
c = socket.create_connection(("127.0.0.1", win.relay.config.port), timeout=5)
hello = b""
while not hello.endswith(b"\n"):
    hello += c.recv(1)
h = json.loads(hello)
c.sendall(P.line({"client": "smoke", "auth": P.auth("pw", h["nonce"]), "update": True}))
answer = b""
while not answer.endswith(b"\n"):
    answer += c.recv(1)
for _ in range(40):
    app.processEvents()
    time.sleep(0.05)
results["remote_update"] = ("update" in h["features"] and json.loads(answer) == {"ok": True, "updating": "9.9.9"}
                            and installed == ["9.9.9"] and restarted == [1])
if len(sys.argv) > 1:
    win.grab().save(sys.argv[1])
win.quit()
print(results, "errors:", len(errors))
ok = all(results.values()) and not errors
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
