#!/usr/bin/env bash
# Install the repo's deploy/nixos/tv-thing.nix as viviserv2's /etc/nixos/tv-thing.nix and switch to it.
# Run it as yourself: sudo asks only for the copy and the rebuild. It shows the change and asks
# first, evaluates the whole system with it before touching /etc/nixos, and keeps a dated copy
# of the module it replaces. The feed is never restarted by a rebuild (restartIfChanged =
# false): a change to its environment takes effect when you restart tv-thing-feed yourself.
set -euo pipefail
SRC=$(cd "$(dirname "$0")" && pwd)/tv-thing.nix
DST=/etc/nixos/tv-thing.nix

if cmp -s "$SRC" "$DST"; then
    echo "$DST is already this module; rebuilding anyway."
else
    diff -u "$DST" "$SRC" || true
    read -r -p "Install this module and rebuild? [y/N] " answer
    [ "$answer" = y ] || { echo "Nothing changed."; exit 1; }
    T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
    cp /etc/nixos/*.nix "$T"/ && cp "$SRC" "$T"/tv-thing.nix
    echo "Evaluating the system with it..."
    nix-instantiate "<nixpkgs/nixos>" -A system -I nixos-config="$T/configuration.nix" > /dev/null
    sudo cp -p "$DST" "$DST.before-$(date +%Y%m%d-%H%M%S)"
    sudo install -m 644 "$SRC" "$DST"
fi
sudo nixos-rebuild switch

echo
echo "=== checks"
if [ -e /run/opengl-driver/lib/dri/iHD_drv_video.so ]; then
    echo "iHD driver: installed"
    vainfo --display drm --device /dev/dri/renderD128 2>/dev/null | grep -E "Driver version|H264.*EncSlice" | head -3
else
    echo "iHD driver: MISSING from /run/opengl-driver/lib/dri"
fi
for unit in tv-thing-feed tv-thing tv-thing-web-basic httpd; do
    printf "%-20s %s\n" "$unit" "$(systemctl is-active "$unit" || true)"
done
# The unit file changes at the rebuild; the running feed keeps the environment it started with.
FEED=$(systemctl show -p MainPID --value tv-thing-feed)
if [ "$FEED" != 0 ] && ! tr "\\0" "\\n" < "/proc/$FEED/environ" | grep -q "^LIBVA_DRIVER_NAME="; then
    echo "tv-thing-feed is still on its old environment: restart it when nothing is recording:"
    echo "  sudo systemctl restart tv-thing-feed"
fi
