"""The RTL-SDR's Windows driver: librtlsdr needs WinUSB on the dongle's interface 0.

Windows gives a new dongle no driver (or a TV-tuner driver from Windows Update), so
librtlsdr can't see it. ``missing()`` finds plugged-in dongles whose interface 0 isn't on
WinUSB (from Windows' device list), and ``install()`` puts WinUSB on them with
wdi-simple from libwdi (github.com/pbatard/libwdi, what Zadig is built on), bundled with
the relay. It needs administrator rights, so Windows asks for permission first.

On Linux there's nothing to install: the distribution's rtl-sdr package has the udev rules
and keeps the kernel's TV driver off the dongle.
"""

import ctypes
import json
import logging
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)
IDS = ((0x0BDA, 0x2838), (0x0BDA, 0x2832))          # RTL2832U dongles (RTL-SDR Blog V3/V4, generic)


@dataclass
class Device:
    vid: int
    pid: int
    iid: int | None           # the interface (MI_00), or None for a non-composite device
    name: str
    service: str              # its driver now ("" for none)
    instance: str

    def describe(self) -> str:
        now = f"driver {self.service}" if self.service else "no driver"
        return f"{self.name or 'RTL-SDR'} ({self.vid:04X}:{self.pid:04X}, {now})"


def parse(entries) -> list[Device]:
    """Dongles needing WinUSB, from Win32_PnPEntity rows (PNPDeviceID, Name, Service)."""
    if isinstance(entries, dict):
        entries = [entries]
    found = {}
    for e in entries or []:
        pnp = (e.get("PNPDeviceID") or "").upper()
        for vid, pid in IDS:
            prefix = f"USB\\VID_{vid:04X}&PID_{pid:04X}"
            if not pnp.startswith(prefix):
                continue
            rest = pnp[len(prefix):]
            if rest.startswith("&MI_"):
                iid = int(rest[4:6], 16)
                if iid != 0:
                    continue                        # interface 1 is the remote-control receiver
            elif rest.startswith("\\"):
                iid = None
            else:
                continue
            key = (vid, pid, iid, pnp.split("\\")[-1])
            dev = Device(vid, pid, iid, e.get("Name") or "", e.get("Service") or "", pnp)
            found[key] = dev
    # A composite dongle lists its parent too (service usbccgp); only the interface matters.
    composite = {(d.vid, d.pid) for d in found.values() if d.iid is not None}
    return [d for d in found.values()
            if d.service.lower() != "winusb" and not (d.iid is None and (d.vid, d.pid) in composite)]


def _powershell(cmd: str, timeout=20) -> str:
    flags = 0x08000000 if sys.platform == "win32" else 0          # CREATE_NO_WINDOW
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                       text=True, timeout=timeout, creationflags=flags)
    return r.stdout


def missing() -> list[Device]:
    """Plugged-in dongles that need WinUSB (always [] off Windows)."""
    if sys.platform != "win32":
        return []
    query = ("Get-CimInstance Win32_PnPEntity -Filter \"PNPDeviceID LIKE 'USB\\\\VID_0BDA&PID_283%'\" | "
             "Select-Object PNPDeviceID, Name, Service | ConvertTo-Json -Compress")
    try:
        out = _powershell(query)
        return parse(json.loads(out)) if out.strip() else []
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log.info("looking for dongles without a driver: %s", e)
        return []


def wdi_simple() -> Path | None:
    here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for p in (here / "wdi" / "wdi-simple.exe", Path(sys.executable).parent / "wdi" / "wdi-simple.exe"):
        if p.is_file():
            return p
    return None


def command(dev: Device, dest: str) -> list[str]:
    args = ["--name", "RTL2832U (SDR Tools Relay)", "--manufacturer", "Realtek", "--vid", f"0x{dev.vid:04X}",
            "--pid", f"0x{dev.pid:04X}", "--type", "0", "--inf", "rtl-sdr-winusb.inf", "--dest", dest,
            "--timeout", "120000", "--silent"]
    if dev.iid is not None:
        args += ["--iid", str(dev.iid)]
    return args


def run_elevated(exe: str, args: list[str], timeout_ms=180000) -> int:
    """Run *exe* as administrator (Windows' permission prompt) and wait. Returns its exit code;
    raises OSError if it couldn't start (e.g. permission refused)."""
    from ctypes import wintypes

    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    info = SHELLEXECUTEINFO()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000400            # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_FLAG_NO_UI
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = subprocess.list2cmdline(args)
    info.nShow = 0                                   # SW_HIDE
    shell32, kernel32 = ctypes.windll.shell32, ctypes.windll.kernel32
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.GetLastError()
        if err == 1223:                              # ERROR_CANCELLED: "No" on the permission prompt
            raise OSError("Windows' permission prompt was declined")
        raise OSError(f"couldn't start the driver installer (error {err})")
    try:
        if kernel32.WaitForSingleObject(info.hProcess, timeout_ms) != 0:
            raise OSError("the driver installer didn't finish in time")
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return ctypes.c_int32(code.value).value
    finally:
        kernel32.CloseHandle(info.hProcess)


# libwdi's error codes (enum wdi_error in libwdi.h, v1.5.1), for messages.
ERRORS = {-1: "input/output error", -2: "invalid parameter", -3: "access denied", -4: "no such device",
          -5: "not found", -6: "busy", -7: "timed out", -8: "overflow",
          -9: "another driver installation is still pending", -10: "interrupted", -11: "out of resources",
          -12: "not supported", -13: "already exists", -14: "cancelled", -15: "needs administrator rights",
          -16: "a 32-bit installer can't install on 64-bit Windows", -17: "error in the driver's .inf file",
          -18: "the driver's catalogue is missing", -19: "the driver isn't signed", -99: "other error"}


def install(devices: list[Device]) -> str:
    """Put WinUSB on each device. Returns a message; raises OSError on failure."""
    exe = wdi_simple()
    if exe is None:
        raise OSError("this copy has no driver installer (wdi-simple.exe); use Zadig instead")
    dest = tempfile.mkdtemp(prefix="sdr-tools-relay-driver-")
    done = []
    for dev in devices:
        code = run_elevated(str(exe), command(dev, dest))
        log.info("wdi-simple for %s: exit %d", dev.describe(), code)
        if code != 0:
            raise OSError(f"installing the driver for {dev.describe()} failed: {ERRORS.get(code, f'error {code}')}")
        done.append(dev.describe())
    return "Driver installed for " + ", ".join(done)
