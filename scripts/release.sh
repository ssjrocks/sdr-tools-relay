#!/usr/bin/env bash
# Makes a draft GitHub release with the wheel and sdr-tools-relay.json (version, wheel name,
# SHA-256) for the in-app updater and scripts/install.sh. Pushing the tag starts
# .github/workflows/windows.yml, which builds and tests the Windows installer, attaches it and
# sdr-tools-relay-windows.json, and publishes the release (15-20 minutes). Until then the
# release is a draft nobody sees; if the Windows build fails it stays one.
#
# Usage: scripts/release.sh NOTES.md
# Bump __version__ in src/sdr_relay/__init__.py and commit first.
set -euo pipefail
cd "$(dirname "$0")/.."
notes="${1:?usage: scripts/release.sh NOTES.md}"
[[ -f "$notes" ]] || { echo "no notes file: $notes" >&2; exit 2; }
version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/sdr_relay/__init__.py)
if git rev-parse -q --verify "refs/tags/v$version" >/dev/null; then
  echo "tag v$version already exists" >&2; exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "commit your changes first" >&2; exit 1
fi
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
uv build -q --wheel --out-dir "$tmp"
wheel=$(cd "$tmp" && ls sdr_tools_relay-"$version"-*.whl)
sha=$(sha256sum "$tmp/$wheel" | cut -d' ' -f1)
printf '{"version": "%s", "wheel": "%s", "sha256": "%s"}\n' "$version" "$wheel" "$sha" > "$tmp/sdr-tools-relay.json"
git tag -a "v$version" -m "SDR Tools Relay $version"
git push origin HEAD "v$version"
gh release create "v$version" "$tmp/$wheel" "$tmp/sdr-tools-relay.json" \
  --title "SDR Tools Relay $version" --notes-file "$notes" --verify-tag --draft
echo "draft release SDR Tools Relay $version ($wheel, sha256 $sha): the Windows build publishes it"
echo "  watch: gh run list -R ssjrocks/sdr-tools-relay -L 1"
