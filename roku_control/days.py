"""The day a game schedule means by today.

It turns at 3 AM Eastern, not at midnight: a night's late games (West Coast
hockey, a Pac-12 kickoff, extra innings) run past twelve, and stay that
night's games, on the same Live Sports numbers, until they are over.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo('America/New_York')
# The hour on Eastern clocks when today's games become tomorrow's.
TURNS_AT = 3


def today(now=None):
    """The games' day at ``now`` (an aware datetime; default the present), YYYY-MM-DD."""
    now = datetime.now(EASTERN) if now is None else now.astimezone(EASTERN)
    day = now.date()
    return (day - timedelta(days=1) if now.hour < TURNS_AT else day).isoformat()
