#!/bin/bash
# Scan for ATSC channels and save results.
# Run this once (or whenever you move the antenna).

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCAN_OUTPUT="$SCRIPT_DIR/w_scan_output.txt"
CHANNELS_CONF="$SCRIPT_DIR/channels.conf"

echo "Scanning for ATSC channels (this takes a few minutes)..."
w_scan -fa -c US -X > "$SCAN_OUTPUT" 2>/dev/null || true

# Extract just the channel lines (VLC/xine format)
grep -v "^;" "$SCAN_OUTPUT" | grep ":" > "$CHANNELS_CONF" 2>/dev/null || true

echo "Scan complete. Raw output: $SCAN_OUTPUT"
echo "Channels conf: $CHANNELS_CONF"
echo ""
echo "Now run: python3 parse_channels.py"
