"""Find a live ESPN Unlimited game on YouTube TV's ESPN page and focus its card.

ESPN Unlimited's events have no guide rows of their own. They are on the
ESPN network page, which the ESPN row's station tile opens: under **Live**,
one **On Now** row of cards, each captioned with its label ("ESPN+") and
title ("Colorado Avalanche vs. Philadelphia Flyers"). An NHL game has two
cards side by side, one per stream; on 2026-10-19 the away stream (Altitude)
came first and the home stream (NBC Sports Philadelphia) second. An ESPN+
exclusive has one.

The row scrolls so the focused card is always the leftmost, under a white
ring, with its caption below it. ``open_game`` walks right reading that
caption until it names both teams, takes the preferred stream, re-checks the
card, and leaves it focused: the caller presses the only key that plays.

By 2026-10-22 Live had one row per sport instead ("NHL", "NCAAW Soccer",
"NCAAW Volleyball", ..., "Also Live"), each labelled above its focused card;
Down moves to the next row's first card in the same place. Rows labelled
neither NHL nor On Now are passed over. A card YouTube TV blacks out where
the Roku is carries a crossed-out eye at its centre (Red Wings at Canucks,
in Lansing's market): such a stream is never selected.
"""
from __future__ import annotations

import re

from .errors import NavigationError
from .prime_video import teams_match

# Fractions of the frame, measured on 1280x720 captures of 2026-10-19.
CAPTION = (.12, .665, .425, .80)       # the focused card's label and title
ROW_LABEL = (.12, .25, .45, .32)       # the focused row's name: "NHL", "On Now", "NCAAW Soccer"
MENU = (.04, .25, .25, .95)            # the page's section menu: Live, Upcoming, Networks...
LIVE_PILL = (190 / 1280, 212 / 720)    # inside the white pill of a focused "Live"
# The focused card's ring: its left and right edges halfway down, and its top.
RING = ((166 / 1280, 330 / 720), (527 / 1280, 330 / 720), (300 / 1280, 267 / 720))
RING_LUMA = 150
# The On Now row seldom holds more than a few dozen events at once.
MAX_CARDS = 60
# Sport rows above NHL's; 2026-10-22 had NHL first of five.
MAX_ROWS = 12
FEEDS = ("home", "away")
# The blacked-out card's crossed-out eye, white, centred on the focused card:
# 1280x720 pixels 318-372 by 340-394 in steps of 2 (from 2026-10-22).
BLOCKED_ORIGIN, BLOCKED_STEP = (318, 340), 2
BLOCKED_ICON = (
    "...........#######..........", ".........###########........", ".......##############.......",
    "......####.........###......", "......###...........###.....", ".#...................###....",
    "####..................##....", ".####.......#####.....###...", "...####......#####.....##...",
    "...######.......###....##...", "...#######.......##....###..", "...##...####......#.....##..",
    "...##....#####..........##..", "...##.....#####........##...", "...##.....#######......##...",
    "...###.....########.....#...", "....##.......#######........", "....###...........####......",
    ".....##.............####....", ".....###.............#####..", "......###...........#######.",
    ".......##..........###..####", ".......###........###.....##", "........###......###........",
    ".........####...####........", "..........####.###..........", "............#####...........",
    ".............###............",
)
BLOCKED_IOU = .6  # blocked cards scored .97-1.0, other cards and frames .17 at most


def _crop(frame, box):
    return (int(frame.width * box[0]), int(frame.height * box[1]),
            int(frame.width * box[2]), int(frame.height * box[3]))


def card_focused(frame):
    """Whether the leftmost On Now card has the white focus ring."""
    return all(frame.luma(round(frame.width * x), round(frame.height * y)) >= RING_LUMA for x, y in RING)


def card_blocked(frame):
    """Whether the focused card shows the crossed-out eye of a blackout."""
    x0, y0 = BLOCKED_ORIGIN
    sx, sy = frame.width / 1280, frame.height / 720
    seen = expected = both = 0
    for row, line in enumerate(BLOCKED_ICON):
        for column, mark in enumerate(line):
            x, y = x0 + column * BLOCKED_STEP, y0 + row * BLOCKED_STEP
            white = frame.luma(round(x * sx), round(y * sy)) > 180
            icon = mark == "#"
            seen += white
            expected += icon
            both += white and icon
    union = seen + expected - both
    return bool(union) and both / union >= BLOCKED_IOU


def read_card(feedback, frame, game):
    """The focused card's caption, and whether it names the game."""
    text = feedback.ocr.read(frame, crop=_crop(frame, CAPTION), psm=6).text
    caption = " ".join(text.split())
    return {"caption": caption, "focused": card_focused(frame),
            "espn_plus": bool(re.search(r"\bespn\s*[+t]", caption, re.I)),
            "matched": card_focused(frame) and teams_match(caption, game),
            "blocked": card_blocked(frame)}


def read_row_label(feedback, frame):
    """The focused row's name, as OCR reads it."""
    return " ".join(feedback.ocr.read(frame, crop=_crop(frame, ROW_LABEL), psm=6).text.split())


def hockey_row(label):
    """Whether a Live row can list NHL games: its own row, or the old mixed On Now."""
    return bool(re.search(r"\bnhl\b|\bhockey\b|\bon now\b", label, re.I))


# The menu's other sections, as fragments OCR keeps ("Upcoming" has read "Jpcoming").
MENU_WORDS = ("coming", "network", "studio", "films", "documentar", "original", "movies", "series")


def menu_live_focused(feedback, frame):
    """Whether the page's section menu is open with Live, its first section, focused."""
    text = feedback.ocr.read(frame, crop=_crop(frame, MENU), psm=6).text.lower()
    pill = frame.luma(round(frame.width * LIVE_PILL[0]), round(frame.height * LIVE_PILL[1]))
    return sum(word in text for word in MENU_WORDS) >= 2 and pill >= 200


def _step(controller, key, events, action):
    controller.ecp.keypress(key)
    events.append({"event": "keypress", "key": key, "action": action})
    controller._sleep(controller.config.navigation.key_delay_seconds)
    return controller.feedback.capture()


def open_game(controller, game, events, *, feed="home"):
    """From the ESPN row's verified current program: open the ESPN page, find
    the game's card in Live: On Now, and focus the preferred stream's card."""
    feedback = controller.feedback
    if feedback is None or feedback.ocr is None:
        raise NavigationError("Local OCR is required to find an ESPN Unlimited game")
    if feed not in FEEDS:
        raise NavigationError(f"feed must be one of {', '.join(FEEDS)}")
    title = str(game.get("title") or "the game")
    controller._publish_operation(phase="opening_espn_page", message="Opening the ESPN page")
    events.append({"event": "espn_tile_focus", "result": controller._focus_network_tile(events)})
    controller.ecp.keypress("Select")
    events.append({"event": "keypress", "key": "Select", "action": "open_network"})
    controller._sleep(controller.config.feedback.network_page_settle_seconds)
    page = None
    for attempt in range(1, controller.config.feedback.verify_attempts + 1):
        try:
            page = feedback.verify_network_page("ESPN", ("ESPN",))
            break
        except NavigationError as exc:
            events.append({"event": "espn_page_retry", "attempt": attempt, "error": str(exc)})
            if attempt == controller.config.feedback.verify_attempts:
                raise NavigationError("The ESPN page did not open from its guide row") from exc
            controller._sleep(controller.config.feedback.verify_retry_seconds)
    events.append({"event": "espn_page", "result": page})

    # The page opens on its banner; Down reaches the section menu, Live first.
    frame = _step(controller, "Down", events, "espn_menu")
    if not menu_live_focused(feedback, frame):
        frame = _step(controller, "Down", events, "espn_menu_retry")
        if not menu_live_focused(feedback, frame):
            raise NavigationError("The ESPN page's Live section did not come into focus")
    controller._publish_operation(phase="finding_espn_game", message=f"Looking for {title} on ESPN: On Now")
    frame = _step(controller, "Right", events, "espn_on_now")
    # Down past other sports' rows to NHL's (or the old mixed On Now).
    label, last_label = read_row_label(feedback, frame), None
    for _ in range(MAX_ROWS):
        events.append({"event": "espn_row", "label": label})
        if hockey_row(label) or label == last_label:
            break  # The last row repeats: Down moved nothing.
        frame = _step(controller, "Down", events, "espn_next_row")
        label, last_label = read_row_label(feedback, frame), label
    if not hockey_row(label):
        raise NavigationError(f"The ESPN page lists no NHL games live; {title} may not have started",
                              details={"code": "espn_game_not_listed", "row": label})

    previous, repeats, found = None, 0, None
    for position in range(MAX_CARDS):
        card = read_card(feedback, frame, game)
        events.append({"event": "espn_card", "position": position, **card})
        if card["matched"]:
            found = position
            break
        # Right at the end of the row moves nothing: the same caption again.
        repeats = repeats + 1 if card["caption"] and card["caption"] == previous else 0
        if repeats >= 2:
            break
        previous = card["caption"]
        frame = _step(controller, "Right", events, "espn_next_card")
    if found is None:
        raise NavigationError(f"{title} is not among the ESPN page's live NHL games; it may not have started, "
                              "or be blacked out", details={"code": "espn_game_not_listed"})

    # A game's streams are adjacent cards, away then home; an exclusive has one.
    first = card
    frame = _step(controller, "Right", events, "espn_second_stream")
    second = read_card(feedback, frame, game)
    events.append({"event": "espn_card", "position": found + 1, **second})
    two = second["matched"]
    # The preferred stream unless it is blacked out where the Roku is.
    if two and not second["blocked"] and (feed == "home" or first["blocked"]):
        choice = "second"
    elif not first["blocked"]:
        choice = "first"
    else:
        raise NavigationError(f"{title} is blacked out on YouTube TV where the Roku is",
                              details={"code": "espn_game_blacked_out", "streams": 2 if two else 1})
    if choice == "second":
        chosen, stream, position = second, "home", found + 1
    else:
        frame = _step(controller, "Left", events, "espn_first_stream")
        chosen = read_card(feedback, frame, game)
        events.append({"event": "espn_card", "position": found, **chosen})
        stream, position = ("away" if two else None), found
    # Re-check the focused card before the only key that plays.
    if not chosen["matched"] or chosen["blocked"]:
        raise NavigationError(f"Lost focus on {title}'s card on the ESPN page")
    return {"matched": True, "surface": "espn_on_now_card", "network_page": page,
            "caption": chosen["caption"], "first_caption": first["caption"],
            "stream": stream, "streams": 2 if second["matched"] else 1, "position": position}
