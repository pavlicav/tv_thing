#!/bin/bash
# Scan for ATSC channels and save results.
# Run this once (or whenever you move the antenna).

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCAN_OUTPUT="$ROOT/w_scan_output.txt"
CHANNELS_CONF="$ROOT/channels.conf"

echo "Scanning for ATSC channels (this takes a few minutes)..."
w_scan -fa -c US -X > "$SCAN_OUTPUT" 2>/dev/null || true

# Extract just the channel lines (VLC/xine format)
grep -v "^;" "$SCAN_OUTPUT" | grep ":" > "$CHANNELS_CONF" 2>/dev/null || true

echo "Scan complete. Raw output: $SCAN_OUTPUT"
echo "Channels conf: $CHANNELS_CONF"
echo ""
echo "Now run: python3 scripts/parse_channels.py"
