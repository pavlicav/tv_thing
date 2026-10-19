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
"""
from __future__ import annotations

import re

from .errors import NavigationError
from .prime_video import teams_match

# Fractions of the frame, measured on 1280x720 captures of 2026-10-19.
CAPTION = (.12, .665, .425, .80)       # the focused card's label and title
MENU = (.04, .25, .25, .95)            # the page's section menu: Live, Upcoming, Networks...
LIVE_PILL = (190 / 1280, 212 / 720)    # inside the white pill of a focused "Live"
# The focused card's ring: its left and right edges halfway down, and its top.
RING = ((166 / 1280, 330 / 720), (527 / 1280, 330 / 720), (300 / 1280, 267 / 720))
RING_LUMA = 150
# The On Now row seldom holds more than a few dozen events at once.
MAX_CARDS = 60
FEEDS = ("home", "away")


def _crop(frame, box):
    return (int(frame.width * box[0]), int(frame.height * box[1]),
            int(frame.width * box[2]), int(frame.height * box[3]))


def card_focused(frame):
    """Whether the leftmost On Now card has the white focus ring."""
    return all(frame.luma(round(frame.width * x), round(frame.height * y)) >= RING_LUMA for x, y in RING)


def read_card(feedback, frame, game):
    """The focused card's caption, and whether it names the game."""
    text = feedback.ocr.read(frame, crop=_crop(frame, CAPTION), psm=6).text
    caption = " ".join(text.split())
    return {"caption": caption, "focused": card_focused(frame),
            "espn_plus": bool(re.search(r"\bespn\s*[+t]", caption, re.I)),
            "matched": card_focused(frame) and teams_match(caption, game)}


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
        raise NavigationError(f"{title} is not on the ESPN page's On Now row; it may not have started, "
                              "or be blacked out", details={"code": "espn_game_not_listed"})

    # A game's streams are adjacent cards, away then home; an exclusive has one.
    first = card
    frame = _step(controller, "Right", events, "espn_second_stream")
    second = read_card(feedback, frame, game)
    events.append({"event": "espn_card", "position": found + 1, **second})
    if second["matched"] and feed == "home":
        chosen, stream, position = second, "home", found + 1
    else:
        frame = _step(controller, "Left", events, "espn_first_stream")
        chosen = read_card(feedback, frame, game)
        events.append({"event": "espn_card", "position": found, **chosen})
        stream, position = ("away" if second["matched"] else None), found
    # Re-check the focused card before the only key that plays.
    if not chosen["matched"]:
        raise NavigationError(f"Lost focus on {title}'s card on the ESPN page")
    return {"matched": True, "surface": "espn_on_now_card", "network_page": page,
            "caption": chosen["caption"], "first_caption": first["caption"],
            "stream": stream, "streams": 2 if second["matched"] else 1, "position": position}
