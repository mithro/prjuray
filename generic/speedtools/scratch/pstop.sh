#!/bin/bash
# Stop the pstest runs (only my sp-viv scopes).
pkill -f "${2:-pstest.sh}"
for u in $(systemctl --user list-units --plain --no-legend "prjuray-sp-${1:-viv}-*" | cut -d' ' -f1); do
  echo "stopping $u"
  systemctl --user stop "$u"
done
systemctl --user list-units --plain --no-legend "prjuray-sp-*"
