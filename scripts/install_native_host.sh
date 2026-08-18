#!/bin/sh
# One-time setup: registers braindamage/steam_offers_host.py as a Firefox
# native-messaging host so the webext/ extension can send it scraped Steam
# Market data. Run this yourself -- it writes outside the repo, to
# ~/.mozilla/native-messaging-hosts/, which is Firefox's per-user native
# messaging host directory on Linux.
#
# Usage: scripts/install_native_host.sh

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
WRAPPER_PATH="$REPO_ROOT/native_host/wrapper.sh"
HOST_DIR="$HOME/.mozilla/native-messaging-hosts"
MANIFEST_PATH="$HOST_DIR/braindamage_steam_offers.json"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "No venv found at $VENV_PYTHON -- run 'uv sync' first." >&2
    exit 1
fi

sed "s#__VENV_PYTHON__#$VENV_PYTHON#; s#__REPO_ROOT__#$REPO_ROOT#" \
    "$REPO_ROOT/native_host/wrapper.sh.template" > "$WRAPPER_PATH"
chmod +x "$WRAPPER_PATH"

mkdir -p "$HOST_DIR"
sed "s#__WRAPPER_PATH__#$WRAPPER_PATH#" \
    "$REPO_ROOT/native_host/manifest.template.json" > "$MANIFEST_PATH"

echo "Installed native messaging host manifest: $MANIFEST_PATH"
echo "Wrapper script: $WRAPPER_PATH"
echo ""
echo "Now load webext/ in Firefox (about:debugging -> This Firefox -> Load Temporary Add-on,"
echo "select webext/manifest.json) and make sure your Steam account currency is set to USD."
