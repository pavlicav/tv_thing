"""Shared test setup."""
import sys

import pytest


@pytest.fixture(autouse=True)
def _private_lineup_state(tmp_path, monkeypatch):
    """Numbers a test gives out land in its own lineup_state.json, never the
    checkout's: the production server reads that file."""
    server = sys.modules.get("server")
    if server is not None:
        import lineup
        monkeypatch.setattr(server, "channel_lineup", lineup.Lineup(
            server.LINEUP_FILE, tmp_path / "lineup_state.json"))
