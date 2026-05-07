#!/usr/bin/env bash
# Install/refresh kalshi-pricer + eth-pricer systemd --user units on the VPS.
# Idempotent: copies units, daemon-reloads, enables them. Does NOT start —
# the caller decides when to flip from screen sessions to systemd.
#
# Usage (run on VPS as user `chris`):
#   bash ~/kalshi-pricers/kalshi-pricer/deploy/install_systemd.sh
#
# After install, see deploy/README.md or DEPLOY.md for the migration steps.

set -euo pipefail

REPO_ROOT="$HOME/kalshi-pricers"
SRC="$REPO_ROOT/kalshi-pricer/deploy/systemd"
DST="$HOME/.config/systemd/user"

if [[ ! -d "$SRC" ]]; then
  echo "error: $SRC not found — is the monorepo cloned at $REPO_ROOT?" >&2
  exit 1
fi

mkdir -p "$DST"
mkdir -p "$REPO_ROOT/kalshi-pricer/logs" "$REPO_ROOT/eth-pricer/logs"

cp -v "$SRC"/*.service "$SRC"/*.target "$DST/"

systemctl --user daemon-reload

for unit in \
  kalshi-pricer-selective.service \
  kalshi-pricer-aggressive.service \
  kalshi-pricer-web.service \
  eth-pricer-selective.service \
  eth-pricer-aggressive.service \
  eth-pricer-web.service \
  kalshi-bots.target ; do
  systemctl --user enable "$unit" >/dev/null
  echo "enabled $unit"
done

if ! loginctl show-user "$USER" | grep -q "Linger=yes"; then
  echo
  echo "NOTE: lingering is not enabled — services will stop on logout and not"
  echo "      auto-start at boot. To fix, run as root:"
  echo "          sudo loginctl enable-linger $USER"
fi

echo
echo "Done. Units installed in $DST."
echo "Migrate from screen with:"
echo "    bash $REPO_ROOT/kalshi-pricer/deploy/migrate_from_screen.sh"
