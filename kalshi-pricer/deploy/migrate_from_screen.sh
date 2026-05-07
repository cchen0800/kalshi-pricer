#!/usr/bin/env bash
# One-time migration: stop the legacy `screen` sessions and start the
# corresponding systemd --user services. Migrates one service at a time
# so a startup failure on any single bot doesn't leave the others stranded.
#
# Idempotent: if a screen isn't running, the kill is skipped; if a unit
# is already active, `start` is a no-op.

set -euo pipefail

# Each row: <screen-session-name> <systemd-unit-name>
PAIRS=(
  "kalshi-selective       kalshi-pricer-selective.service"
  "kalshi-aggressive      kalshi-pricer-aggressive.service"
  "kalshi-web             kalshi-pricer-web.service"
  "kalshi-eth-selective   eth-pricer-selective.service"
  "kalshi-eth-aggressive  eth-pricer-aggressive.service"
  "kalshi-eth-web         eth-pricer-web.service"
)

for row in "${PAIRS[@]}"; do
  read -r screen_name unit <<<"$row"
  echo "=== $unit ==="

  if screen -ls | grep -q "\.${screen_name}\b"; then
    echo "  stopping screen $screen_name…"
    screen -S "$screen_name" -X quit || true
    sleep 2
  else
    echo "  screen $screen_name not running"
  fi

  echo "  starting $unit…"
  systemctl --user start "$unit"
  sleep 3
  if systemctl --user is-active --quiet "$unit"; then
    echo "  OK: $unit is active"
  else
    echo "  FAIL: $unit not active — see: journalctl --user -u $unit -n 50"
    exit 1
  fi
done

echo
echo "Migration complete. Status:"
systemctl --user status kalshi-bots.target --no-pager | head -20 || true
