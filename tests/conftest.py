"""Shared test setup."""
import os
import sys
from pathlib import Path

import pytest

# test_integration.py drives a running server (on :8080 unless TV_THING_URL says otherwise): it
# tunes, stops and rescans, so against production it retunes what VizelVision is airing. It is
# collected only when asked for, whatever the command line ignores.
collect_ignore = [] if os.environ.get("TV_THING_INTEGRATION") == "1" else ["test_integration.py"]

# The suite describes the shipped primary node (feed.json's viviserv), whichever host runs it:
# never this host's settings file (host_config.py), nor a role read from its hostname.
os.environ["TV_THING_HOST_CONFIG"] = str(Path(__file__).with_name("no-host-settings.json"))
os.environ["TV_THING_NODE_ROLE"] = "primary"


@pytest.fixture(autouse=True)
def _private_lineup_state(tmp_path, monkeypatch):
    """Numbers a test gives out land in its own lineup_state.json, never the
    checkout's: the production server reads that file."""
    server = sys.modules.get("server")
    if server is not None:
        import lineup
        monkeypatch.setattr(server, "channel_lineup", lineup.Lineup(
            server.LINEUP_FILE, tmp_path / "lineup_state.json"))
        # An internet stream's listings URL (internet_streams.json's "guide") is never
        # fetched in a test; a test that wants listings supplies them.
        monkeypatch.setattr(server.web_streams, "listings", lambda stream_id: [])
        # The feed's recorders and their journal, private to each test.
        import feed_node
        monkeypatch.setattr(server, "recorders", feed_node.Recorders(
            tmp_path / "recordings-journal.jsonl",
            spawn=lambda command: server._spawn_recorder(command),
            stop_timeout=server.RECORDER_STOP_TIMEOUT))
    # Likewise YouTube TV's stored listings: a test reads and writes its own.
    yttv_epg = sys.modules.get("yttv_epg")
    if yttv_epg is not None:
        monkeypatch.setattr(yttv_epg, "GUIDE_FILE", tmp_path / "yttv_epg.json")
