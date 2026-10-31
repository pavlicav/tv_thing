"""Fast regressions for the browser handoff and operator-facing About page."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static" if (ROOT / "static").is_dir() else ROOT
INDEX = (STATIC / "index.html").read_text()
ABOUT = (STATIC / "about.html").read_text()
CABLE = (STATIC / "cable.html").read_text()
GUIDE = (STATIC / "guide.html").read_text()
VCR = (STATIC / "vcr.html").read_text()


def section(source, start, end):
    return source[source.index(start):source.index(end)]


def test_cable_is_a_primary_source_tab():
    source_tabs = section(INDEX, 'id="source-tabs"', 'id="status-dot"')

    assert source_tabs.index('id="tab-tv"') < source_tabs.index('id="tab-cable"')
    assert ">All channels<" in source_tabs
    assert "window.location.href" not in source_tabs
    # Capture cards were removed; only the card's own tuners remain.
    assert 'id="tab-capture"' not in source_tabs


def test_channel_change_waits_for_matching_ready_stream_before_handoff():
    tune = section(INDEX, "async function tuneTv", "// Playback retries")

    assert "const tuneSerial = ++state.tuneSerial" in tune
    assert "if (hls) hls.stopLoad()" in tune
    assert "await waitForTvStream(state.streamId)" in tune
    assert tune.index("await waitForTvStream") < tune.index("startTvPlayback")
    assert "if (tuneSerial !== state.tuneSerial) return" in tune
    assert 'setStatus(tuned.name, "live")' in tune


def test_initial_manifest_failure_recreates_hls_without_retuning():
    playback = section(INDEX, "function startTvPlayback", "function startFmPlayback")

    assert "startTvPlayback(streamId, attempt + 1, onReady)" in playback
    assert "api/tune" not in playback
    assert "attempt < 5" in playback


def test_resume_cannot_override_a_newer_channel_click():
    resume = section(INDEX, "async function resume", "const loaders")

    assert "const resumeStreamId = state.streamId" in resume
    assert "state.streamId !== resumeStreamId || state.tuneSerial" in resume


def test_capture_card_support_is_absent_from_the_ui():
    """The UI drives the tuner card only -- no V4L2 grabbers."""
    assert "tuneCapture" not in INDEX
    assert "captureDevices" not in INDEX
    assert "api/v1/sources/capture" not in INDEX
    assert "api/capture/tune" not in INDEX


def test_source_inventory_uses_the_versioned_catalog():
    data = section(INDEX, "async function loadSources", "async function loadStations")

    assert "api/v1/sources" in data
    assert "loadSources(false)" in INDEX


def test_main_ui_lists_and_tunes_all_cable_channels():
    data = section(INDEX, "async function loadCableChannels", "async function loadSources")
    render = section(INDEX, "function renderList", "function renderFoot")
    tune = section(INDEX, "async function tuneCable", "// Playback retries")

    assert "api/v1/sources/cable" in data
    assert "state.cableChannels = data.items || data.channels || []" in data
    assert '`<span class="item-key">RF ${item.number}</span>`' in render
    assert "Math.ceil(item.frequency_hz / 1e6)" in render
    assert '"api/v1/tune", "api/cable/tune"' in tune
    assert 'source: "cable"' in tune
    assert "channel: channelNumber" in tune
    assert "await waitForTvStream(state.streamId)" in tune


def test_main_ui_uses_versioned_api_for_all_video_sources():
    assert '"api/v1/sources/digital", "api/channels"' in INDEX
    assert '`api/v1/sources/cable?' in INDEX
    assert '`api/v1/sources?' in INDEX
    assert '"api/v1/status", "api/status"' in INDEX
    # Digital, analog cable and the Roku are the three video sources.
    assert INDEX.count('"api/v1/tune",') == 3


def test_main_ui_falls_back_during_a_zero_downtime_server_restart():
    helper = section(INDEX, "async function fetchWithFallback", "function normalizeStatus")

    assert "response.status === 404" in helper
    assert "response = await fetch(fallback, options)" in helper
    assert '"api/v1/tune", "api/tune"' in INDEX
    assert '"api/v1/tune", "api/cable/tune"' in INDEX


def test_cable_page_uses_fixed_channel_list_without_scanning():
    assert "api/cable/channels" in CABLE
    assert 'fetch("api/cable/tune"' in CABLE
    assert "await waitForStream(state.streamId)" in CABLE
    assert "Fixed channel list" in CABLE
    assert "no scan" in CABLE
    assert "api/rescan" not in CABLE


def test_cable_page_lists_every_rf_channel_in_frequency_order():
    render = section(CABLE, "function renderChannels", "async function tune")

    assert 'class="channel-list list scroll"' in CABLE
    assert "left.frequency_hz - right.frequency_hz" in render
    assert "Math.ceil(exactMhz)" in render
    assert '`<span class="item-key">RF ${item.number}</span>`' in render
    assert '`<span class="item-tag">${displayMhz} MHz</span>`' in render


def test_about_page_matches_the_current_streaming_stack():
    for expected in (
        "dvb_source.py",
        "stream_sanity_check.py",
        "600 ms",
        "libx264",
        "2.5 Mb/s",
        "Three real retunes",
    ):
        assert expected in ABOUT

    for stale in ("cvlc", "dvr0 BROKEN", "8–10 seconds"):
        assert stale not in ABOUT


# ---------------------------------------------------------------------------
# Guide
# ---------------------------------------------------------------------------

def test_guide_is_reachable_from_every_page():
    for page in (INDEX, ABOUT, CABLE, GUIDE):
        assert 'href="guide.html"' in page


def test_guide_positions_everything_from_one_window_origin():
    """Ticks, blocks and the now line must share an origin or they drift."""
    for anchor in ("(offset / 60) * PPM",
                   "((event.start - windowStart) / 60) * PPM",
                   "((now - windowStart) / 60) * PPM"):
        assert anchor in GUIDE


def test_guide_clips_events_that_started_before_the_window():
    block = section(GUIDE, "function block(", "function drawNowLine")
    assert "Math.max(0, ((event.start - windowStart) / 60) * PPM)" in block
    assert "Math.max(2, right - left - 2)" in block


def test_guide_titles_are_escaped_not_interpolated_raw():
    """Titles come off the air or the web, so they are untrusted input."""
    assert "escape(ListingTitle.listingTitle(event))" in GUIDE
    assert "escape(nameOf(channel))" in GUIDE
    assert "escape([episode, named]" in GUIDE
    assert "escape(event.rating)" in GUIDE
    assert "node.textContent = text == null" in GUIDE


def test_guide_refresh_uses_the_streaming_endpoint():
    refresh = section(GUIDE, "function refresh()", "function escape(")
    assert "api/guide/refresh/stream?descriptions=" in refresh
    for event in ("status", "frequency", "result", "saved", "failed"):
        assert f"addEventListener('{event}'" in refresh
    # The button must come back even when the sweep dies mid-stream.
    assert refresh.count("$('btn-refresh').disabled = false") >= 3


def test_guide_explains_why_a_refresh_interrupts_live_tv():
    assert "interrupts live TV" in GUIDE
    assert "tuning a channel yourself stops it early" in GUIDE


def test_guide_orders_primaries_before_subchannels():
    """The grid must match the channel list's order, not group by station."""
    sort = section(GUIDE, "function channelSortKey", "function renderMeta")
    assert "minor === 1 ? [0, major, 0] : [1, major, minor]" in sort
    # The naive major-then-minor comparison is the thing being replaced.
    assert "aMajor - bMajor || aMinor - bMinor" not in GUIDE


def test_pages_address_the_api_relatively():
    """Absolute API paths break the UI behind the Apache /tv/ proxy.

    A page served at /tv/guide.html that asks for "/api/guide" escapes the
    proxy prefix and gets Apache's document root back, so the failure is a
    chunk of HTML arriving where JSON was expected -- and it only ever
    reproduces through the proxy, never on :8080 directly.
    """
    import re

    pattern = re.compile(r"""(?:fetch|EventSource)\(\s*[`'"]/""")
    for name, page in (("index", INDEX), ("about", ABOUT),
                       ("cable", CABLE), ("guide", GUIDE), ("vcr", VCR)):
        assert not pattern.search(page), f"{name}.html calls the API absolutely"


# ---------------------------------------------------------------------------
# Tuner lock override
# ---------------------------------------------------------------------------

def test_override_is_never_automatic():
    """A 423 must reach a person, not be retried past by the page."""
    tune = section(INDEX, "async function tuneTv", "// Playback retries")
    assert "res.status === 423" in tune
    assert "await confirmOverride(" in tune
    # The retry only happens after the prompt resolves true.
    assert tune.index("await confirmOverride(") < tune.index('post({ override: true })')
    assert "if (!await confirmOverride" in tune


def test_the_override_prompt_warns_before_it_offers_the_button():
    prompt = section(INDEX, "function confirmOverride", "function closeOverride")
    assert "cannot be undone" in prompt
    assert "interrupts whatever that service is doing" in prompt
    # The safe choice holds focus so a stray Enter cannot break a lock.
    assert "els.ovrCancel.focus()" in prompt
    assert 'id="ovr-confirm"' in INDEX
    assert "Override the lock" in INDEX


def test_dismissing_the_prompt_leaves_the_lock_alone():
    tune = section(INDEX, "async function tuneTv", "// Playback retries")
    assert 'setStatus("Tuner locked", "error")' in tune
    close = section(INDEX, "function closeOverride", "els.ovrCancel.addEventListener")
    assert "overrideResolve(result)" in close
    # Escape and a backdrop click both resolve false, never true.
    assert "closeOverride(false)" in INDEX
    assert INDEX.count("closeOverride(true)") == 1


def test_releasing_a_lock_from_the_ui_goes_through_the_same_prompt():
    release = section(INDEX, 'els.lockPill.addEventListener',
                      "async function pollLock")
    assert "await confirmOverride(lock, \"release this lock\")" in release
    assert '"api/v1/tuner/lock/release", "api/tuner/lock/release"' in release
    assert "override: true" in release


def test_every_element_the_script_queries_exists_before_it_runs():
    """The script runs inline, so a later element resolves to null.

    Adding the override modal after the closing </script> made $() return
    null, and the TypeError that followed aborted the rest of the script --
    which is where the channel list is loaded. The page came up blank with a
    working API behind it.
    """
    import re

    script_start = INDEX.index("<script>")
    ids = set(re.findall(r'\$\("([a-z0-9-]+)"\)', INDEX))
    assert ids, "no $() lookups found -- has the pattern changed?"

    for element_id in sorted(ids):
        marker = f'id="{element_id}"'
        assert marker in INDEX, f"{element_id} is queried but never defined"
        assert INDEX.index(marker) < script_start, (
            f"{element_id} is defined after the script that queries it"
        )


def test_live_controls_do_not_advertise_dvr_seeking():
    assert '<video id="player" controls' not in INDEX
    assert 'id="live-play"' in INDEX
    assert 'id="live-volume"' in INDEX
    assert 'id="live-fullscreen"' in INDEX
    assert 'id="live-edge"' in INDEX
    assert 'initialLiveManifestSize: 4' in INDEX


def test_player_uses_lane_playlist_instead_of_shared_primary_alias():
    playback = section(INDEX, "function startTvPlayback", "function startFmPlayback")
    assert 'state.playing === "cable" ? "analog"' in playback
    assert '(state.playing === "roku" ? "roku" : "digital")' in playback
    assert 'stream/live.m3u8' not in playback


def test_roku_tab_lists_feeds_and_drives_the_remote_under_its_lock():
    source_tabs = section(INDEX, 'id="source-tabs"', 'id="status-dot"')
    assert source_tabs.index('id="tab-cable"') < source_tabs.index('id="tab-roku"')
    roku = section(INDEX, "// ---------- Roku ----------", "// ---------- events ----------")
    # Feeds come from the lane's sources; games follow the chosen date.
    assert 'source.lane === "roku"' in INDEX
    assert "api/v1/sources/${source.id}${query}" in roku
    assert "date=${encodeURIComponent(state.roku.date)}" in roku
    # Until someone picks a date, the lists follow the games' day as it turns.
    assert "if (!state.roku.datePicked) state.roku.date = gameDay();" in roku
    assert "state.roku.datePicked = state.roku.date !== gameDay();" in roku
    # Every change carries this page's own lock token, and a 423 is only ever
    # overridden by a person in the override dialog.
    assert "token: rokuToken()" in roku
    assert "await confirmOverride(data.lock, action)" in roku
    assert 'owner: ROKU_OWNER, duration_seconds: 3600' in roku
    # The remote: ECP keys only, and the keyboard drives it while it shows.
    remote = section(INDEX, 'id="roku-remote"', 'id="list"')
    for key in ("Up", "Down", "Left", "Right", "Select", "Back", "Home", "Play"):
        assert f'data-key="{key}"' in remote
    assert "PowerOff" not in remote
    assert 'ArrowUp: "Up"' in INDEX and 'Backspace: "Back"' in INDEX
    # The Roku's picture never drives the legacy alias, and has no quality.
    assert 'state.playing === "roku"' in section(INDEX, "function render()", "function matches")


def test_live_player_runtime():
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for live player runtime tests")
    subprocess.run([node, "--test", str(Path(__file__).with_name("test_live_player.cjs"))],
                   check=True, capture_output=True, text=True, timeout=15)


def test_listing_titles_name_the_game():
    """The guide and the Watch page call a game by its teams and league."""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for listing title runtime tests")
    subprocess.run([node, "--test", str(Path(__file__).with_name("test_listing_title.cjs"))],
                   check=True, capture_output=True, text=True, timeout=15)
    for page in (GUIDE, INDEX):
        assert '<script src="listing-title.js"></script>' in page
    assert "ListingTitle.listingTitle(on)" in INDEX and "ListingTitle.listingTitle(next)" in INDEX


def test_every_page_script_parses():
    """A syntax error stops a page's whole script, and the string checks in
    this file cannot see one: a doubled const once left the guide on
    "Loading…"."""
    import re
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to parse the pages' scripts")
    compile_stdin = "new (require('vm').Script)(require('fs').readFileSync(0, 'utf8'))"
    for page in sorted(STATIC.glob("*.html")):
        scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page.read_text(), re.S)
        for index, script in enumerate(scripts):
            result = subprocess.run([node, "-e", compile_stdin], input=script,
                                    capture_output=True, text=True, timeout=15)
            assert result.returncode == 0, f"{page.name} script {index}: {result.stderr[-400:]}"


# ---------------------------------------------------------------------------
# VCR
# ---------------------------------------------------------------------------

def test_vcr_is_reachable_from_every_page():
    for name in ("index", "about", "cable", "guide", "rescan", "dx", "vcr"):
        page = (STATIC / f"{name}.html").read_text()
        assert 'href="vcr.html"' in page, f"{name}.html has no VCR link"


def test_vcr_offers_both_channel_time_and_guide_name_programming():
    assert 'id="panel-ctime"' in VCR and "CH/TIME" in VCR
    assert 'id="panel-name"' in VCR and "GUIDE NAME" in VCR
    assert 'mode: "manual"' in VCR
    assert 'mode: "guide"' in VCR
    assert 'mode: "otr"' in VCR
    assert "api/v1/recordings/search" in VCR or "${API}/search" in VCR


def test_the_vcr_source_menu_comes_from_the_servers_inputs():
    form = section(VCR, 'id="f-manual"', "</form>")
    assert form.index('for="m-src"') < form.index('for="m-ch"')
    assert "fillInputs(state.inputs)" in section(VCR, "async function load()", "(async function start")
    # An older server lists no inputs; the page then records the digital tuner.
    assert "const DIGITAL_INPUT" in VCR and "[DIGITAL_INPUT]" in VCR


def test_vcr_timers_record_the_chosen_source():
    submit = section(VCR, '$("f-manual").addEventListener("submit"', "function presetManualTimes")
    assert "lane: currentInput().lane" in submit
    assert "source_id: choice.source.id, channel_id: choice.item.id" in submit
    assert "body.date = choice.day" in submit


def test_the_shelf_has_a_recorder_per_input():
    build = section(VCR, "function buildShelf()", "function pressKey(")
    assert "units = inputs.map(" in build
    assert 'const LOOKS = { roku: "settop" }' in VCR       # the Roku is a cable box
    assert "LOOKS[input.lane] || \"vcr\"" in build           # anything else is a VCR
    for look in ("vcr", "settop"):
        start = VCR.index(f'<template id="unit-{look}">')
        template = VCR[start:VCR.index("</template>", start)]
        for key in ("rec", "stop", "prog", "tapes"):
            assert f'data-key="{key}"' in template, (look, key)
        for part in ("name", "seg-clock", "text", "text-inner", "i-rec", "i-timer", "led-power"):
            assert f'data-part="{part}"' in template, (look, part)
    assert VCR.index('<template id="unit-settop">') < VCR.index("<script>")


def test_each_recorder_tapes_and_stops_its_own_lane():
    press = section(VCR, "function pressKey(", "function renderShelf()")
    assert 'key === "rec") oneTouch(unit.lane)' in press
    assert 'key === "stop") stopRecording(unit.lane)' in press
    assert "selectInput(unit.lane)" in press                # PROG puts it on the TV
    assert "if (listed) ARROWS[key]()" in press             # arrows step only its own list
    start = VCR.index('<template id="unit-settop">')
    explorer = VCR[start:VCR.index("</template>", start)]
    for key in ("up", "down", "left", "right"):
        assert f'data-key="{key}"' in explorer
    rec = section(VCR, "async function oneTouch(lane)", "async function stopRecording(lane)")
    assert '{ lane, mode: "otr" }' in rec
    stop = section(VCR, "async function stopRecording(lane)", "// ---------- loading")
    assert "laneOf(timer) === lane" in stop
    render = section(VCR, "function renderUnit(unit)", "// ---------- tabs")
    assert "laneOf(timer) === unit.lane" in render and "laneOf(t) === unit.lane" in render


def test_vcr_lists_games_for_the_chosen_date():
    listing = section(VCR, "async function sourceItems", "function gameClock")
    assert "source.dated ? `?date=${day}`" in listing
    assert "serial === loadChannels.serial" in listing
    assert "loadChannels()" in section(VCR, "function stepDay", "function manualWindow")


def test_vcr_script_only_queries_elements_defined_before_it():
    import re

    script_start = VCR.index("<script>")
    ids = set(re.findall(r'\$\("([a-z0-9-]+)"\)', VCR))
    ids |= {f"tab-{t}" for t in ("ctime", "name", "timers", "tapes")}
    ids |= {f"panel-{t}" for t in ("ctime", "name", "timers", "tapes")}
    assert len(ids) > 20
    for element_id in sorted(ids):
        marker = f'id="{element_id}"'
        assert marker in VCR, f"{element_id} is queried but never defined"
        assert VCR.index(marker) < script_start


def test_vcr_never_interpolates_programme_titles_as_html():
    script = VCR[VCR.index("<script>"):]
    assert "innerHTML" not in script


def test_erasing_a_tape_asks_first_with_the_safe_choice_focused():
    assert 'id="confirm-no"' in VCR
    ask = VCR[VCR.index("function askErase"):VCR.index("function closeErase")]
    assert '$("confirm-no").focus()' in ask
    erase = VCR[VCR.index('$("confirm-yes").addEventListener'):]
    assert "${API}/erase" in erase[:400]


def test_the_guide_can_hand_an_airing_to_the_vcr():
    assert 'id="d-record"' in GUIDE
    record = GUIDE[GUIDE.index("async function record"):]
    assert "'api/v1/recordings'" in record[:700]
    assert "mode: 'guide'" in record[:600]
    # A YouTube TV airing tapes the Roku, named by its channel, not a number.
    assert "lane: 'roku', mode: 'guide'" in record[:600]
    assert "target: { source_id: channel.source_id, channel_id: channel.channel_id }" in record[:600]


def test_guide_lists_youtube_tv_and_sends_its_rows_to_the_watch_page():
    # Rows are told apart by key: an antenna number or youtube-tv/NAME.
    assert "const keyOf = (channel) => channel.key || channel.number;" in GUIDE
    assert "row.dataset.key = keyOf(channel);" in GUIDE
    assert ".row[data-key=" in GUIDE and "data-number" not in GUIDE
    assert "fetch('api/guide?hours=30')" in GUIDE
    # The Watch page tunes the Roku, showing its progress and any lock.
    watch = section(GUIDE, "async function watch(channel, event)", "// Hand the airing to the VCR")
    assert "./#watch=${encodeURIComponent(keyOf(channel))}" in watch
    assert watch.index("isRoku(channel)") < watch.index("api/tune")
    # A Live Sports row is a different game each day: its airing names the target.
    assert "./#watch=${encodeURIComponent(`${event.source_id}/${event.channel_id}`)}" in watch
    assert watch.index("gameRefusal(event)") < watch.index("#watch=")
    refusal = section(GUIDE, "function gameRefusal(event)", "async function load()")
    assert "event.date !== gameDay()" in refusal and "!event.playable" in refusal
    assert "watch(selected.channel, selected.event)" in GUIDE
    # A row with no listing stays; Antenna numbers is the antenna's own view.
    assert "isRoku(channel) ? !antennaNumbers\n" in GUIDE
    assert "isGames(channel) ? !antennaNumbers && channel.events.length > 0" in GUIDE
    # An internet stream with listings (internet/cabride) plays its relay from Watch.
    assert "isInternet(channel) ? !antennaNumbers && channel.events.length > 0" in GUIDE
    assert "isRoku(channel) || isInternet(channel)" in watch
    assert "guide.sports" in GUIDE
    assert "guide.youtube_tv" in GUIDE


def test_watch_page_lists_hockey_greyed_with_where_to_find_it():
    # ESPN Unlimited's NHL games: listed, never tunable, the note on hover.
    assert "[...sources, ...(data.listed || [])]" in INDEX
    assert "if (item.playable === false && item.note) button.title = item.note;" in INDEX
    assert "if (item.playable === false && item.note) btn.title = item.note;" in INDEX
    assert '"nhl": "NHL"' in INDEX


def test_watch_page_tunes_the_roku_channel_the_guide_links_to():
    linked = section(INDEX, "function linkedRokuTarget()", "startSourcePolling();")
    assert "#watch=" in linked
    # Used once: a reload must not send the Roku off again.
    assert "history.replaceState(" in linked
    assert "tuneRoku(linked.source, linked.item)" in linked
    # A game is one of today's, as the Watch page lists them, and one the Roku can reach.
    assert "(!s.dated || state.roku.date === gameDay())" in linked
    assert "s.tunable !== false" in linked
    assert "else resume();" in linked


def test_the_pages_turn_the_games_day_at_3_am():
    """As the server does (roku_control/days.py): a night's late games stay
    today's until 3 AM, so the Watch page lists them and the guide's Watch
    puts them on."""
    import json
    import os
    import re
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to run the pages' gameDay")
    moments = ["2026-10-24T23:30:00", "2026-10-25T00:30:00", "2026-10-25T02:59:00",
               "2026-10-25T03:00:00", "2026-11-01T01:30:00"]
    run = """
const RealDate = Date, gameDay = eval(`(${process.argv[1]})`);
console.log(JSON.stringify(JSON.parse(process.argv[2]).map((moment) => {
  global.Date = class extends RealDate { constructor(...a) { super(...(a.length ? a : [moment])); } };
  return gameDay();
})));"""
    for page in (INDEX, GUIDE):
        script = re.search(r"function gameDay\(\) \{.*?\n\s*\}\n", page, re.S).group(0)
        result = subprocess.run([node, "-e", run, script, json.dumps(moments)],
                                env={**os.environ, "TZ": "America/New_York"},
                                capture_output=True, text=True, timeout=15, check=True)
        assert json.loads(result.stdout) == [
            "2026-10-24", "2026-10-24", "2026-10-24", "2026-10-25", "2026-10-31"]


def test_watch_page_shows_what_is_on_youtube_tv():
    tunable = section(INDEX, "function renderTunableList()", "function renderAntennaList()")
    assert "listingFor(`${source.id}/${item.id}`" in tunable
    banner = section(INDEX, "// ---------- info banner", "// Controls fade away")
    assert "listingHtml(rokuGuideKey(selection), now)" in banner


# ---------------------------------------------------------------------------
# Shared chrome
# ---------------------------------------------------------------------------

PAGES = ("index", "guide", "vcr", "cable", "rescan", "dx", "about", "roku", "inputs", "config")


def test_every_page_has_the_same_nav_in_the_same_place():
    """The links must not move between pages.

    They are left-aligned straight after the brand and identical everywhere
    except for which one is marked current; page-specific controls live in
    .topbar-tools on the right, where they cannot push the nav around.
    """
    import re

    navs = {}
    for name in PAGES:
        page = (STATIC / f"{name}.html").read_text()
        header = section(page, '<header class="topbar">', "</header>")
        nav = section(header, '<nav class="nav"', "</nav>")
        assert header.index('class="brand"') < header.index('<nav class="nav"')
        assert 'class="spacer"' not in header
        assert nav.count('aria-current="page"') == 1, name
        navs[name] = re.sub(r"\s+", " ", nav.replace(' aria-current="page"', ""))
    assert len(set(navs.values())) == 1, navs


def test_no_page_restyles_the_shared_top_bar():
    for name in PAGES:
        page = (STATIC / f"{name}.html").read_text()
        style = page[:page.index("</style>")] if "</style>" in page else ""
        assert ".navlink" not in style, name
        assert ".brand span" not in style, name


def test_guide_preview_only_watches_and_never_tunes():
    preview = section(GUIDE, "// ---------- Picture-in-picture preview",
                      "// ---------- Clock")
    assert "api/v1/status" in preview
    assert "api/tune" not in preview and "method:" not in preview
    assert '<video id="pip-video" muted' in GUIDE


def test_guide_keyboard_moves_like_a_remote():
    keys = section(GUIDE, "document.addEventListener('keydown'", "let resizeFrame")
    for key in ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Enter"):
        assert key in keys


def test_tapes_can_be_played_and_saved_from_the_vcr():
    tapes = section(VCR, "function renderTapes()", "function askErase(")
    assert '"▶ PLAY"' in tapes and '"SAVE"' in tapes
    assert "save.download = tape.name" in tapes
    assert 'download ? "&download=1"' in VCR
    assert "api/v1/recordings" in VCR and "/file?name=${encodeURIComponent(name)}" in VCR
    assert 'id="tape-video"' in VCR and 'id="player-stop"' in VCR
    # Erasing the tape that is playing ejects it first.
    erase = VCR[VCR.index('$("confirm-yes").addEventListener'):]
    assert "stopTape();" in erase[:200]


def test_watch_page_banner_uses_the_guide_without_tuning():
    banner = section(INDEX, "// ---------- info banner", "// Controls fade away")
    assert 'fetch("api/guide/now"' in banner
    assert "api/guide?channel=" in banner
    assert "method:" not in banner and "api/tune" not in banner
    assert "escapeHtml(ListingTitle.listingTitle(on))" in banner
    assert "escapeHtml(ListingTitle.listingTitle(e))" in banner


def test_watch_page_video_leaves_room_for_the_banner():
    assert "max-height: calc(100% - var(--banner-min))" in INDEX
    assert "grid-template-columns: minmax(0, 1fr) var(--panel-w)" in INDEX


def test_hidden_attribute_always_wins():
    css = (STATIC / "app.css").read_text()
    assert "[hidden] { display: none !important; }" in css


# ---------------------------------------------------------------------------
# TV Thing channel numbers
# ---------------------------------------------------------------------------

def test_watch_lists_the_lineup_by_number_with_the_antenna_a_tab_away():
    assert 'id="tab-antenna"' in INDEX
    assert "if (data.lineup) state.lineup = data.lineup;" in INDEX
    tunable = section(INDEX, "function renderTunableList()", "function renderAntennaList()")
    assert "sectionHeading(entry.section)" in tunable
    assert "tuneVizelVision(entry.number)" in tunable
    # The Antenna tab is the old Digital list: antenna numbers and RF channels.
    antenna = section(INDEX, "function renderAntennaList()", "function digitalLabel(")
    assert "escapeHtml(channel.number)" in antenna and "RF ${escapeHtml(channel.rf_channel)}" in antenna
    assert "tuneTv(channel.id)" in antenna


def test_an_internet_stream_plays_its_relay_without_a_tuner_or_lock():
    tunable = section(INDEX, "function renderTunableList()", "function renderAntennaList()")
    assert 'entry.kind === "internet"' in tunable and "tuneInternet(entry)" in tunable
    play = section(INDEX, "function tuneInternet(entry)", "function watchRelay(")
    assert 'watchRelay("internet", entry.streamPath' in play
    relay = section(INDEX, "function watchRelay(", "// A slide channel plays in the browser")
    assert "showLock(null)" in relay and "fetch(" not in relay
    assert '["roku", "vizelvision", "slides", "internet"].includes(state.playing)' in INDEX


def test_guide_follows_the_lineup_unless_antenna_numbers_are_chosen():
    assert 'id="opt-antenna"' in GUIDE
    assert "tv-thing.guide.antenna-numbers" in GUIDE
    assert "escape(shownNumber(channel))" in GUIDE
    # Tuning and taping still name the antenna channel itself.
    assert "channel_number: channel.number" in GUIDE
    assert "channel_id: channel.id" in GUIDE


# ---------------------------------------------------------------------------
# VizelVision's Sports Guide (channel 6)
# ---------------------------------------------------------------------------

def test_the_sports_guide_pages_through_its_listings_from_the_lan_alone():
    import re
    page = (STATIC / "sports-guide.html").read_text()
    # Behind the /tv/ proxy too, and with no internet: relative API, fonts beside it.
    assert 'fetch("api/v1/sports-guide"' in page
    assert not re.search(r"""(?:src|href)=["']?https?://|url\(["']?https?://""", page)
    for style in ("regular", "bold", "italic", "bolditalic"):
        assert f'url("fonts/guide-serif-{style}.woff2")' in page
        assert (STATIC / "fonts" / f"guide-serif-{style}.woff2").read_bytes()[:4] == b"wOF2"
    assert "Today’s Sports" in page
    # Every listing is escaped, never interpolated raw.
    assert "${esc(game.network)}" in page and "${esc(game.tv_number)}" in page
    assert "esc(game.title" in page


def test_watch_plays_a_slide_channel_in_a_frame_and_stops_it_on_leaving():
    tunable = section(INDEX, "function renderTunableList()", "function renderAntennaList()")
    assert 'entry.kind === "slides"' in tunable and "tuneSlides(entry)" in tunable
    tune = section(INDEX, "function tuneSlides(entry)", "function renderFoot()")
    assert "destroyHls()" in tune and 'state.playing = "slides"' in tune
    assert "fetch(" not in tune     # no tune, no lock: the page plays on its own
    render = section(INDEX, "function render()", "function matches")
    assert 'els.slideStage.removeAttribute("src")' in render
