#!/usr/bin/env bash
# Package the Roku app for sideloading (tv_thing.zip), and its source with
# its tests (tv_thing-source.zip, which unpacks to roku_app/ and
# test_roku_app.py). TV Thing serves both under roku/ (static/roku links here).
# Usage: ./package.sh
set -e
cd "$(dirname "$0")"
rm -f tv_thing.zip tv_thing-source.zip
zip -q -r tv_thing.zip manifest source/ components/ images/ fonts/
here=$(basename "$PWD")
tests=""
[ -f ../test_roku_app.py ] && tests=test_roku_app.py
(cd .. && zip -q -r "$here/tv_thing-source.zip" "$here" $tests -x "$here/*.zip" -x '*/__pycache__/*')
echo "Built tv_thing.zip and tv_thing-source.zip"
echo "Sideload at: http://<ROKU_IP>/ → Development Application Installer → Upload"
