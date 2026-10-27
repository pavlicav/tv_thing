#!/usr/bin/env python3
"""TV Thing's feed: the media plane, in a process of its own (Phase 2, INVARIANTS.md).

It holds the hardware and every ffmpeg: the antenna, cable and Roku lanes, their supervision,
the VCR's recorders, the slide channels' video and the relays. It serves /stream/ and, to
this host only, /feed/v1/ (feed_api.FeedAPI), and listens on 127.0.0.1 alone (:8086 unless
PORT says otherwise). Control (server.py with TV_THING_ROLE=control) decides what runs, and
reaches it only through feed_api.RemoteFeed, so control can restart without a feed dropping.

It is server.py in the feed role: the same code, one process per role.
"""
import os

os.environ["TV_THING_ROLE"] = "feed"

import server  # noqa: E402 - the role is read when server.py loads

if __name__ == "__main__":
    raise SystemExit(server.main())
