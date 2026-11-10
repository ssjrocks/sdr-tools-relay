#!/usr/bin/env bash
# Installs SDR Tools Relay on Linux (Ubuntu / Debian / Raspberry Pi OS) for the current user:
# the RTL-SDR library and Qt from the distribution (asks for your password once), the app in
# ~/.local/share/sdr-tools-relay/venv, and its app-menu entry. Windows has its own installer.
#
#   From a checkout:  scripts/install.sh
#   Anywhere:         curl -fsSL https://raw.githubusercontent.com/ssjrocks/sdr-tools-relay/main/scripts/install.sh | bash
#   Remove:           scripts/install.sh --uninstall
set -euo pipefail

APP_ID=io.github.ssjrocks.SDRToolsRelay
REPO=ssjrocks/sdr-tools-relay
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
ROOT="$DATA/sdr-tools-relay"
VENV="$ROOT/venv"
DESKTOP="$DATA/applications/$APP_ID.desktop"
ICON="$DATA/icons/hicolor/256x256/apps/sdr-tools-relay.png"

if [[ "${1:-}" == --uninstall ]]; then
  rm -rf "$ROOT"
  rm -f "$DESKTOP" "$ICON" "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/sdr-tools-relay.desktop"
  update-desktop-database -q "$DATA/applications" 2>/dev/null || true
  echo "SDR Tools Relay removed."
  exit 0
fi

step() { printf '\n== %s\n' "$*"; }

src=""
if [[ -f "${BASH_SOURCE[0]:-}" ]]; then
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  [[ -f "$here/../pyproject.toml" ]] && src="$(cd "$here/.." && pwd)"
fi
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

step "System packages (asks for your password)"
sudo apt-get update -q
sudo apt-get install -y -q python3-venv python3-pip python3-pyqt6 rtl-sdr

if [[ -n "$src" ]]; then
  target="$src"
else
  step "Downloading the latest release"
  # Release files come through GitHub's API (its plain download links time out at busy times).
  read -r wheel_api wheel_name sha < <(python3 - "$REPO" <<'PY'
import json, sys, urllib.request
repo = sys.argv[1]
def get(url, accept):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "sdr-tools-relay-installer"})
    return urllib.request.urlopen(req, timeout=60).read()
rel = json.loads(get(f"https://api.github.com/repos/{repo}/releases/latest", "application/vnd.github+json"))
assets = {a["name"]: a["url"] for a in rel["assets"]}
info = json.loads(get(assets["sdr-tools-relay.json"], "application/octet-stream"))
print(assets[info["wheel"]], info["wheel"], info["sha256"])
PY
)
  target="$tmp/$wheel_name"
  curl -fsSL -H "Accept: application/octet-stream" -o "$target" "$wheel_api"
  echo "$sha  $target" | sha256sum -c --quiet
fi

step "The app"
mkdir -p "$ROOT"
python3 -m venv --clear --system-site-packages "$VENV"
"$VENV/bin/pip" install -q "$target"
touch "$VENV/sdr-tools-relay-venv"     # tells the in-app updater it may update this venv

step "App-menu entry"
mkdir -p "$(dirname "$DESKTOP")" "$(dirname "$ICON")"
QT_QPA_PLATFORM=offscreen "$VENV/bin/python" -c "
import sys
from PyQt6.QtWidgets import QApplication
app = QApplication([])
from sdr_relay.gui import make_icon
make_icon(256).pixmap(256, 256).save(sys.argv[1])" "$ICON"
cat > "$DESKTOP" <<DESK
[Desktop Entry]
Version=1.1
Type=Application
Name=SDR Tools Relay
Comment=Lend this PC's RTL-SDR to SDR Toolkit over the LAN or the internet
Exec=$VENV/bin/python -m sdr_relay
Icon=sdr-tools-relay
Terminal=false
Categories=Network;HamRadio;
StartupWMClass=sdr-tools-relay
DESK
update-desktop-database -q "$DATA/applications" 2>/dev/null || true
echo
echo "Installed. Open SDR Tools Relay from the app menu."
