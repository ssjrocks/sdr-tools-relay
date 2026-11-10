"""Starting the relay at login: the Run key on Windows, an autostart .desktop file on Linux."""

import os
import shlex
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "SDRToolsRelay"


def command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--minimized"]
    return [sys.executable, "-m", "sdr_relay", "--minimized"]


def _desktop_file() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / "sdr-tools-relay.desktop"


def enabled() -> bool:
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, VALUE)
            return True
        except OSError:
            return False
    return _desktop_file().exists()


def set_enabled(on: bool):
    if sys.platform == "win32":
        import subprocess
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, subprocess.list2cmdline(command()))
            else:
                try:
                    winreg.DeleteValue(k, VALUE)
                except FileNotFoundError:
                    pass
        return
    f = _desktop_file()
    if on:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("[Desktop Entry]\nType=Application\nName=SDR Tools Relay\n"
                     f"Exec={shlex.join(command())}\nIcon=sdr-tools-relay\nX-GNOME-Autostart-enabled=true\n")
    else:
        f.unlink(missing_ok=True)
