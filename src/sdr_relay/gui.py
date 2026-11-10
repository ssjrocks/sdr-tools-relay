"""The relay's window and tray icon."""

import html
import logging
import sys
import time
import uuid
from pathlib import Path

from PyQt6.QtCore import QPointF, QSettings, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, __version__, appreports, autostart, driver, updater
from . import protocol as P
from .discovery import Responder
from .internet import Internet
from .rtlsdr import RtlError, devices
from .server import Config, Relay

log = logging.getLogger(__name__)


def make_icon(size=64, colour=QColor(30, 110, 200)) -> QIcon:
    """An antenna with waves, drawn here so no image files are needed."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 64
    p.setBrush(colour)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(int(4 * s), int(4 * s), int(56 * s), int(56 * s), 12 * s, 12 * s)
    pen = QPen(QColor("white"), 4 * s)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.drawLine(QPointF(32 * s, 30 * s), QPointF(32 * s, 52 * s))
    p.drawLine(QPointF(24 * s, 52 * s), QPointF(40 * s, 52 * s))
    p.setBrush(QColor("white"))
    p.drawEllipse(QPointF(32 * s, 27 * s), 3.5 * s, 3.5 * s)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for r in (10, 18):
        p.drawArc(int((32 - r) * s), int((27 - r) * s), int(2 * r * s), int(2 * r * s), 30 * 16, 120 * 16)
    p.end()
    return QIcon(pix)


class Task(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as e:  # shown to the user
            log.exception("background task")
            self.failed.emit(str(e))


class Window(QMainWindow):
    def __init__(self, start_hidden=False, demo_file=None):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_icon())
        self.settings = QSettings("ssjrocks", "sdr-tools-relay")
        self._tasks = set()
        self._update = None
        relay_id = self.settings.value("id", "")
        if not relay_id:
            relay_id = str(uuid.uuid4())
            self.settings.setValue("id", relay_id)
        self.relay = Relay(self._config(demo_file), version=__version__)
        self.internet = Internet(self.relay, relay_id, f"{APP_NAME}/{__version__}")
        self.responder = Responder(self.relay)

        # Status
        self.state = QLabel()
        self.state.setWordWrap(True)
        f = self.state.font()
        f.setPointSizeF(f.pointSizeF() * 1.2)
        f.setBold(True)
        self.state.setFont(f)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self.relay.disconnect)
        top = QHBoxLayout()
        col = QVBoxLayout()
        col.addWidget(self.state)
        col.addWidget(self.detail)
        top.addLayout(col, 1)
        top.addWidget(self.disconnect_btn, 0, Qt.AlignmentFlag.AlignTop)

        # Dongle
        self.device = QComboBox()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.fill_devices)
        self.device_note = QLabel()
        self.device_note.setWordWrap(True)
        self.device_note.setOpenExternalLinks(True)
        self.driver_btn = QPushButton("Install driver")
        self.driver_btn.setToolTip("Put Windows' WinUSB driver on the dongle, so the relay can use it")
        self.driver_btn.clicked.connect(lambda: self.install_driver(ask=False))
        self.driver_btn.setVisible(False)
        self._need_driver: list = []
        self._offered_driver = False
        drow = QHBoxLayout()
        drow.addWidget(self.device, 1)
        drow.addWidget(self.driver_btn)
        drow.addWidget(refresh)
        dbox = QGroupBox("Dongle")
        dl = QVBoxLayout(dbox)
        dl.addLayout(drow)
        dl.addWidget(self.device_note)

        # Settings
        self.name = QLineEdit(self.relay.config.name)
        self.password = QLineEdit(self.relay.config.password)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("none: anyone who finds the relay can use it")
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(self.relay.config.port)
        self.ppm = QSpinBox()
        self.ppm.setRange(-200, 200)
        self.ppm.setSuffix(" ppm")
        self.ppm.setValue(int(self.relay.config.ppm))
        self.client_ppm = QCheckBox("Let users change it")
        self.client_ppm.setChecked(self.relay.config.allow_client_ppm)
        prow = QHBoxLayout()
        prow.addWidget(self.ppm)
        prow.addWidget(self.client_ppm)
        prow.addStretch(1)
        self.location = QLineEdit(str(self.settings.value("location", "")))
        self.location.setPlaceholderText("optional, e.g. Brisbane, Australia")
        self.open_port = QCheckBox("Reachable from the internet (open the port on my router with UPnP)")
        self.open_port.setChecked(self._bool("open_port", False))
        self.listed = QCheckBox("List it on ssjrocks.com (anyone with SDR Toolkit can see and use it)")
        self.listed.setChecked(self._bool("listed", False))
        self.autostart = QCheckBox("Start when I log in (in the tray)")
        self.autostart.setChecked(autostart.enabled())
        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Password", self.password)
        form.addRow("Port", self.port)
        form.addRow("Frequency correction", prow)
        form.addRow("Location", self.location)
        sbox = QGroupBox("Settings")
        sl = QVBoxLayout(sbox)
        sl.addLayout(form)
        for w in (self.open_port, self.listed, self.autostart):
            sl.addWidget(w)
        self.port_status = QLabel()
        self.port_status.setWordWrap(True)
        self.list_status = QLabel()
        self.list_status.setWordWrap(True)
        sl.addWidget(self.port_status)
        sl.addWidget(self.list_status)
        for w in (self.name, self.password, self.location):
            w.editingFinished.connect(self.apply)
        for w in (self.port, self.ppm):
            w.editingFinished.connect(self.apply)
        for w in (self.client_ppm, self.open_port, self.listed):
            w.toggled.connect(self.apply)
        self.autostart.toggled.connect(self._set_autostart)
        self.device.currentIndexChanged.connect(self.apply)

        # Activity
        self.events = QListWidget()
        self.update_btn = QPushButton()
        self.update_btn.setVisible(False)
        self.update_btn.clicked.connect(self.install_update)
        check_btn = QPushButton("Check for updates")
        check_btn.clicked.connect(lambda: self.check_updates(quiet=False))
        report_btn = QPushButton("Report a problem…")
        report_btn.clicked.connect(self.report_problem)
        brow = QHBoxLayout()
        brow.addWidget(QLabel(f"Version {__version__}"))
        brow.addStretch(1)
        for b in (self.update_btn, check_btn, report_btn):
            brow.addWidget(b)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addLayout(top)
        lay.addWidget(dbox)
        lay.addWidget(sbox)
        lay.addWidget(QLabel("Activity"))
        lay.addWidget(self.events, 1)
        lay.addLayout(brow)
        self.setCentralWidget(central)
        self.resize(620, 720)

        # Tray
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(make_icon(), self)
            menu = QMenu()
            show = QAction("Show", self)
            show.triggered.connect(self.show_window)
            disc = QAction("Disconnect the user", self)
            disc.triggered.connect(self.relay.disconnect)
            quit_ = QAction("Quit", self)
            quit_.triggered.connect(self.quit)
            for a in (show, disc, quit_):
                menu.addAction(a)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(lambda reason: self.show_window()
                                        if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
            self.tray.show()
        self._quitting = False
        self._told_tray = False

        self.fill_devices()
        self.start_serving()
        self.internet.start()
        self.internet.configure(self.open_port.isChecked(), self.listed.isChecked(), self.location.text())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()
        if not (start_hidden and self.tray):
            self.show()
        QTimer.singleShot(3000, lambda: self.check_updates(quiet=True) if updater.due() else None)

    # ---- settings -----------------------------------------------------

    def _bool(self, key, default):
        return str(self.settings.value(key, default)).lower() in ("true", "1")

    def _config(self, demo_file) -> Config:
        s = self.settings
        return Config(name=str(s.value("name", "") or _default_name()), port=int(s.value("port", P.PORT)),
                      password=str(s.value("password", "")), device=int(s.value("device", 0)),
                      ppm=int(s.value("ppm", 0)), allow_client_ppm=self._bool("client_ppm", False),
                      file=demo_file)

    def apply(self):
        c = self.relay.config
        old_port = c.port
        c.name = self.name.text().strip() or _default_name()
        c.password = self.password.text()
        c.ppm = self.ppm.value()
        c.allow_client_ppm = self.client_ppm.isChecked()
        if self.device.currentData() is not None:
            c.device = int(self.device.currentData())
        for key, value in (("name", c.name), ("password", c.password), ("ppm", c.ppm),
                           ("client_ppm", c.allow_client_ppm), ("device", c.device),
                           ("location", self.location.text().strip()), ("open_port", self.open_port.isChecked()),
                           ("listed", self.listed.isChecked()), ("port", self.port.value())):
            self.settings.setValue(key, value)
        if self.port.value() != old_port:
            self.relay.stop()
            c.port = self.port.value()
            self.start_serving()
            self.internet.port_changed()
        self.internet.configure(self.open_port.isChecked(), self.listed.isChecked(), self.location.text().strip())

    def _set_autostart(self, on):
        try:
            autostart.set_enabled(on)
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't change starting at login: {e}")

    def start_serving(self):
        try:
            self.relay.start()
            self.serve_error = ""
        except OSError as e:
            self.serve_error = f"Can't listen on port {self.relay.config.port}: {e}. Choose another port."
            log.warning(self.serve_error)
        try:
            self.responder.start()
        except OSError as e:
            log.info("LAN discovery: %s", e)

    def fill_devices(self):
        self.device.blockSignals(True)
        self.device.clear()
        if self.relay.config.file:
            self.device.addItem(f"Recording: {Path(self.relay.config.file).name}", 0)
            self.device_note.setText("Demonstration: playing a recording instead of a dongle.")
        else:
            try:
                found = devices()
                for d in found:
                    label = d["name"] + (f" (serial {d['serial']})" if d["serial"] else "")
                    self.device.addItem(label, d["index"])
                self.device_note.setText("" if found else "No RTL-SDR found. Plug it in and press Refresh.")
            except RtlError as e:
                self.device_note.setText(html.escape(str(e)))
            i = self.device.findData(self.relay.config.device)
            if i >= 0:
                self.device.setCurrentIndex(i)
            if sys.platform == "win32":
                self._run(driver.missing, self._driver_checked, lambda msg: None)
        self.device.blockSignals(False)

    # ---- the Windows driver -------------------------------------------

    def _driver_checked(self, need):
        self._need_driver = need
        self.driver_btn.setVisible(bool(need))
        if not need:
            return
        names = ", ".join(d.describe() for d in need)
        self.device_note.setText(f"{html.escape(names)} needs Windows' WinUSB driver before the relay can use it. "
                                 "Press Install driver (Windows asks for permission).")
        if not self._offered_driver:
            self._offered_driver = True
            QTimer.singleShot(300, lambda: self.install_driver(ask=True))

    def install_driver(self, ask=True):
        need = self._need_driver
        if not need:
            return
        if ask:
            self.show_window()
            names = "\n".join(d.describe() for d in need)
            if QMessageBox.question(self, APP_NAME, f"This dongle needs Windows' WinUSB driver:\n\n{names}\n\n"
                                    "Install it now? Windows will ask for permission. Programs that use the "
                                    "dongle's old driver (TV software) won't see it any more.") \
                    != QMessageBox.StandardButton.Yes:
                return
        self.driver_btn.setEnabled(False)
        self.driver_btn.setText("Installing…")

        def done(message):
            self.relay.note(message)
            self.driver_btn.setEnabled(True)
            self.driver_btn.setText("Install driver")
            QTimer.singleShot(2000, self.fill_devices)       # give Windows a moment to start the driver

        def failed(msg):
            self.relay.note(f"Driver: {msg}")
            self.driver_btn.setEnabled(True)
            self.driver_btn.setText("Install driver")
            QMessageBox.warning(self, APP_NAME, f"The driver wasn't installed: {msg}\n\nZadig (zadig.akeo.ie) "
                                "does the same by hand: Options → List All Devices → Bulk-In, Interface "
                                "(Interface 0) → WinUSB.")

        self._run(lambda: driver.install(need), done, failed)

    # ---- status -------------------------------------------------------

    def status_text(self) -> tuple[str, str]:
        if getattr(self, "serve_error", ""):
            return "Not running", self.serve_error
        s = self.relay.session
        if s is None:
            extra = self.relay.last_error or ("Password set." if self.relay.config.password else "No password.")
            return "Ready, waiting for SDR Toolkit", f"Port {self.relay.config.port}. {extra}"
        mins = int(time.time() - s.started)
        return (f"In use by {s.address}",
                f"{s.freq / 1e6:.4f} MHz · {s.rate / 1e6:.3f} MS/s · gain {s.gain} · "
                f"{s.rate_bps / 1e6:.2f} MB/s · {mins // 60}:{mins % 60:02d}"
                + (f" · {s.dropped} buffers dropped (network too slow for this sample rate)" if s.dropped else ""))

    def refresh(self):
        state, detail = self.status_text()
        self.state.setText(state)
        self.detail.setText(detail)
        self.disconnect_btn.setEnabled(self.relay.session is not None)
        self.port_status.setText(self.internet.port_status)
        self.list_status.setText(self.internet.list_status)
        if self.tray:
            self.tray.setToolTip(f"{APP_NAME}: {state}")
        events = list(self.relay.events)
        if self.events.count() != len(events):
            self.events.clear()
            for t, text in reversed(events):
                self.events.addItem(f"{time.strftime('%H:%M:%S', time.localtime(t))}  {text}")

    # ---- window and tray ----------------------------------------------

    def show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, ev):
        if self.tray and not self._quitting:
            ev.ignore()
            self.hide()
            if not self._told_tray:
                self._told_tray = True
                self.tray.showMessage(APP_NAME, "Still running in the tray. Right-click it to quit.",
                                      QSystemTrayIcon.MessageIcon.Information, 4000)
            return
        self.shutdown()
        super().closeEvent(ev)

    def shutdown(self):
        self.timer.stop()
        self.relay.stop()
        self.responder.stop()
        self.internet.stop()

    def quit(self):
        self._quitting = True
        self.close()
        QApplication.instance().quit()

    # ---- updates and reports ------------------------------------------

    def _run(self, fn, done, failed):
        t = Task(fn)
        t.done.connect(done)
        t.failed.connect(failed)
        t.finished.connect(lambda: self._tasks.discard(t))
        self._tasks.add(t)
        t.start()

    def check_updates(self, quiet=True):
        updater.mark_checked()

        def done(update):
            self._update = update
            if update:
                self.update_btn.setText(f"Update to {update.version}")
                self.update_btn.setVisible(True)
            elif not quiet:
                QMessageBox.information(self, APP_NAME, f"{__version__} is the latest version.")

        self._run(updater.check, done, lambda msg: None if quiet else
                  QMessageBox.warning(self, APP_NAME, f"Couldn't check for updates: {msg}"))

    def install_update(self):
        u = self._update
        if u is None:
            return
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Updating…")

        def done(_):
            if sys.platform == "win32":
                self.quit()                 # the installer replaces the files and starts the new version
            else:
                self.shutdown()
                updater.restart()

        def failed(msg):
            self.update_btn.setEnabled(True)
            self.update_btn.setText(f"Update to {u.version}")
            QMessageBox.warning(self, APP_NAME, msg)

        self._run(lambda: updater.install(u), done, failed)

    def report_problem(self):
        note, ok = QInputDialog.getMultiLineText(self, "Report a problem",
                                                 "What went wrong? The relay's log and settings (not the "
                                                 "password) are sent with it.")
        if ok:
            appreports.send_report(note, extra={"settings": self._settings_summary()})
            QMessageBox.information(self, "Report a problem", "Thanks, it's on its way.")

    def _settings_summary(self) -> str:
        c = self.relay.config
        return (f"name={c.name} port={c.port} password={'set' if c.password else 'none'} device={c.device} "
                f"ppm={c.ppm} client_ppm={c.allow_client_ppm} open_port={self.open_port.isChecked()} "
                f"listed={self.listed.isChecked()} port_status={self.internet.port_status!r} "
                f"list_status={self.internet.list_status!r}")


def _default_name() -> str:
    import socket
    return f"{socket.gethostname()} RTL-SDR"


def run(start_hidden=False, demo_file=None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(make_icon())
    win = Window(start_hidden, demo_file)
    app.aboutToQuit.connect(lambda: None if win._quitting else win.shutdown())
    return app.exec()
