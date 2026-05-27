#!/usr/bin/env bash
# Package the Roku app as tv_thing.zip for sideloading.
# Usage: ./package.sh
set -e
cd "$(dirname "$0")"
rm -f tv_thing.zip
zip -r tv_thing.zip manifest source/ components/ images/ 2>/dev/null || \
zip -r tv_thing.zip manifest source/ components/
echo "Built tv_thing.zip"
echo "Sideload at: http://<ROKU_IP>/ → Development Application Installer → Upload"
