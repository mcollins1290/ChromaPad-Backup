#!/bin/sh
# Debian rolling recovery backup. Subsequent runs update one image in place.
set -eu
worker=/usr/local/lib/chromapad-backup-promoter/rolling_backup.py
unit=chromapad-backup.service
case "${1:-}" in
  '')
    [ "$#" -eq 0 ] || exit 2
    [ "$(id -u)" -eq 0 ] || { echo 'Run this script with sudo.' >&2; exit 1; }
    /usr/bin/python3 "$worker" --check >/dev/null
    systemctl start --no-block "$unit"
    printf '%s\n' 'Debian backup started in the background.' \
      'Showing live progress. Ctrl+C exits the viewer; the backup keeps running.' \
      'Successful completion updates /media/raid1/coprint/coprint.img.' \
      'The rolling image is incomplete during updates; copy it only after success.' \
      'Klipper is stopped and restored automatically; do not print during backup.'
    exec journalctl -fu "$unit"
    ;;
  --plan) [ "$#" -eq 1 ]; exec /usr/bin/python3 "$worker" --plan ;;
  --status) [ "$#" -eq 1 ]; exec systemctl status --no-pager "$unit" ;;
  --logs) [ "$#" -eq 1 ]; exec journalctl -fu "$unit" ;;
  --help|-h)
    printf '%s\n' 'Usage: sudo ./update_factory_recovery_img.sh [--plan|--status|--logs]' \
      'Run while idle with heaters off. Klipper is stopped and restored automatically.' \
      'Live logs open automatically. --logs reconnects to them without starting a backup.' \
      'Updates one Debian recovery image in place. No dated images accumulate.' \
      'The original stock fallback is preserved. Does not flash USB or restore the pad.' ;;
  *) echo 'Unknown argument. Use --help.' >&2; exit 2 ;;
esac
