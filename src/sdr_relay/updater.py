"""Updates from the project's GitHub releases.

Each release carries, for Linux, the wheel and sdr-tools-relay.json ({"version", "wheel",
"sha256"}), installed with pip into the venv scripts/install.sh made; and for Windows the
installer and sdr-tools-relay-windows.json ({"version", "installer", "sha256"}), run
silently, which restarts the relay. Files must come from this repository's releases and
match their checksum. Running from a source checkout, it only reports that an update exists.
"""

import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import REPO, __version__

LOG = logging.getLogger(__name__)
API = f"https://api.github.com/repos/{REPO}/releases/latest"
WINDOWS = sys.platform == "win32"
INFO_ASSET = "sdr-tools-relay-windows.json" if WINDOWS else "sdr-tools-relay.json"
CHECK_EVERY = 12 * 3600
USER_AGENT = f"sdr-tools-relay/{__version__}"


@dataclass
class Update:
    version: str
    url: str                # the file's API address
    name: str
    sha256: str
    link: str = ""          # its plain download link, the fallback


def parse_version(version: str) -> tuple[int, ...]:
    parts = []
    for part in version.lstrip("v").split("."):
        digits = "".join(c for c in part if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _get(url: str, accept: str | None = None, timeout=20) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.read()


def _trusted(url: str) -> bool:
    return url.startswith((f"https://api.github.com/repos/{REPO}/releases/assets/",
                           f"https://github.com/{REPO}/releases/download/"))


def _download(api_url: str, link: str) -> bytes:
    """A release file: through the API first (GitHub's plain links time out at busy times), then the link."""
    errors = []
    for url, accept in ((api_url, "application/octet-stream"), (link, None)):
        if not url or not _trusted(url):
            continue
        try:
            return _get(url, accept, timeout=120)
        except OSError as e:
            errors.append(f"{url}: {e}")
    raise OSError("; ".join(errors) or "no trusted download address")


def check() -> Update | None:
    release = json.loads(_get(API, "application/vnd.github+json"))
    assets = {a["name"]: (a["url"], a["browser_download_url"]) for a in release.get("assets", [])}
    if INFO_ASSET not in assets:
        return None
    info = json.loads(_download(*assets[INFO_ASSET]))
    if parse_version(info["version"]) <= parse_version(__version__):
        return None
    name = info["installer" if WINDOWS else "wheel"]
    if name not in assets:
        return None
    return Update(info["version"], assets[name][0], name, info["sha256"].lower(), assets[name][1])


def state_dir() -> Path:
    if WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    path = base / "sdr-tools-relay"
    path.mkdir(parents=True, exist_ok=True)
    return path


def due() -> bool:
    try:
        last = json.loads((state_dir() / "update.json").read_text())["checked_at"]
    except (OSError, ValueError, KeyError):
        last = 0
    return time.time() - last > CHECK_EVERY


def mark_checked():
    (state_dir() / "update.json").write_text(json.dumps({"checked_at": time.time()}))


def can_install() -> bool:
    if WINDOWS:
        return bool(getattr(sys, "frozen", False))
    return (Path(sys.prefix) / "sdr-tools-relay-venv").exists()


def install(update: Update):
    """Download and verify; on Linux pip-install (restart() afterwards), on Windows start the installer
    (which closes and restarts the relay). Raises on any failure."""
    if not can_install():
        raise RuntimeError(f"This copy runs from source. Get {update.version} from "
                           f"https://github.com/{REPO}/releases")
    data = _download(update.url, update.link)
    if hashlib.sha256(data).hexdigest() != update.sha256:
        raise RuntimeError("The download didn't match its checksum; nothing was installed.")
    folder = Path(tempfile.mkdtemp(prefix="sdr-tools-relay-update-"))
    path = folder / update.name
    path.write_bytes(data)
    if WINDOWS:
        subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                         close_fds=True)
        return
    result = subprocess.run([sys.executable, "-m", "pip", "install", str(path)], capture_output=True, text=True)
    path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"pip couldn't install the update:\n{result.stderr[-2000:]}")


def restart():
    os.execv(sys.executable, [sys.executable, "-m", "sdr_relay"])
