"""Fast package/contract checks for the TV Thing Roku app (roku_app/), and
off-device runs of its lineup, guide grid and scene handlers. The brs interpreter
(npm's brs) runs the BrightScript; without it on PATH those tests skip. It
exercises state handlers, not the device's video or UI."""

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
APP = ROOT / "roku_app"
INDEX = (ROOT / "static" / "index.html").read_text()
SCENE = (APP / "components" / "MainScene.brs").read_text()
LINEUP = (APP / "components" / "Lineup.brs").read_text()

# SceneGraph nodes the app takes from the device rather than defining.
BUILT_IN = {"ContentNode", "StandardMessageDialog", "Group", "Poster", "Label", "Font"}

# A lineup shaped like /api/v1/sources, its catalogs and /api/v1/guide.
# Real Unix times: a whole second off (Int() through a 32-bit Float) shows.
NOW = 1792970000
FIXTURE = {
    "date": "2026-10-25",
    "lineup": {
        "house": [{"tv_number": 0, "tv_name": "VizelVision", "tv_section": "House channel"},
                  {"tv_number": 70, "tv_name": "VizelVision", "tv_section": "House channel"}],
        "slides": [{"tv_number": 60, "tv_name": "VizelVision Sports Guide", "channel_id": "sports-guide",
                    "page_path": "sports-guide.html", "stream_path": "/stream/sports-guide/index.m3u8"}],
        "internet": [
            {"tv_number": 29, "tv_name": "Pentulive", "channel_id": "pentulive", "name": "Pentulive",
             "note": "Yle's puppy cam", "stream_path": "/stream/internet/pentulive/index.m3u8"},
            {"tv_number": 144, "tv_name": "Cab Ride", "channel_id": "cabride", "name": "Cab Ride",
             "note": "Train cab rides", "stream_path": "/stream/internet/cabride/index.m3u8"},
            {"channel_id": "unlisted", "name": "Aurora Cam", "stream_path": "/stream/internet/unlisted/index.m3u8"},
        ],
    },
    "channels": [
        {"id": "wjla_7_1", "number": "7.1", "name": "WJLA", "rf_channel": 7, "tv_number": 7, "tv_name": "WJLA"},
        {"id": "wzdc_44_1", "number": "44.1", "name": "WZDC", "rf_channel": 34, "tv_number": 44},
        {"id": "wxyz_99_1", "number": "99.1", "name": "Bonus 10", "rf_channel": 30},
        {"id": "wxyz_99_2", "number": "99.2", "name": "Bonus 9", "rf_channel": 30},
        # Another market's, relayed from the box there: its 7.1 is not this box's 7.1 (WJLA).
        {"id": "lansing/wlaj_7_1", "number": "7.1", "name": "WLAJ", "rf_channel": 14, "tv_number": 407,
         "market": "lansing", "market_name": "Lansing", "node": "viviserv2", "relayed": True},
    ],
    "sources": [
        {"id": "youtube-tv", "name": "YouTube TV", "lane": "roku", "dated": False},
        {"id": "mlb", "name": "Baseball", "lane": "roku", "dated": True},
    ],
    "feeds": {
        "youtube-tv": [{"id": "NBC", "name": "NBC", "tv_number": 16, "tv_name": "WILX"},
                       {"id": "AMC", "name": "AMC"}],
        "mlb": [{"id": "776", "name": "Tigers at Guardians", "state": "pre", "status": "Scheduled",
                 "start_time": "2026-10-25T23:05:00Z", "tv_number": 300, "playable": True},
                {"id": "777", "name": "Cubs at Reds", "state": "final", "status": "Final",
                 "playable": False}],
    },
    "guide": {
        "7.1": {"events": [{"start": NOW - 600, "duration": 3600, "title": "Jeopardy!", "captions": True},
                           {"start": NOW - 4200, "duration": 3600, "title": "Already over"}]},
        "youtube-tv/NBC": {"events": [{"start": NOW - 1800, "duration": 3600, "title": "Dateline",
                                       "description": "A mystery.", "live": True}]},
        "internet/cabride": {"events": [
            {"start": NOW + 2578, "duration": 6819, "title": "Rural Akita in Spring"},
            {"start": NOW - 547, "duration": 3125, "title": "San'in Line Along the Sea of Japan",
             "description": "JR San'in Main Line"}]},
        "games/300": {"events": [{"start": NOW + 3000, "duration": 10800, "title": "Tigers at Guardians",
                                  "episode_title": "MLB Baseball · MLB.TV"}]},
    },
}


def app_sources():
    return {path: path.read_text() for path in APP.rglob("*")
            if path.suffix in (".brs", ".xml") or path.name == "manifest"}


def brs_or_skip():
    interpreter = shutil.which("brs")
    if interpreter is None:
        pytest.skip("brs (npm install -g brs) is required to run the Roku app's BrightScript")
    return interpreter


def run_brs(tmp_path, source):
    script = tmp_path / "scenario.brs"
    script.write_text(source)
    result = subprocess.run([brs_or_skip(), str(script)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL:" not in result.stdout, result.stdout
    return result.stdout


def brs_string(text):
    return '"' + text.replace('"', '""') + '"'


def test_manifest_is_tv_thing_with_its_artwork():
    manifest = dict(line.split("=", 1) for line in (APP / "manifest").read_text().splitlines()
                    if "=" in line)
    assert manifest["title"] == "TV Thing"
    assert manifest["ui_resolutions"] == "hd"
    for key in ("mm_icon_focus_hd", "mm_icon_focus_sd", "mm_icon_side_hd", "splash_screen_hd"):
        assert (APP / manifest[key].removeprefix("pkg:/")).is_file(), key


def test_every_pkg_path_is_in_the_package():
    for path, text in app_sources().items():
        for ref in re.findall(r'pkg:/([^"\s]+)', text):
            assert (APP / ref).is_file(), f"{path.name} names missing pkg:/{ref}"


def test_every_component_it_creates_is_defined():
    defined = set()
    for xml in (APP / "components").glob("*.xml"):
        component = ET.parse(xml).getroot()
        assert component.tag == "component"
        defined.add(component.get("name"))
        for script in component.iter("script"):
            assert (APP / script.get("uri").removeprefix("pkg:/")).is_file()
    used = set()
    for text in app_sources().values():
        used |= set(re.findall(r'CreateObject\("roSGNode", "(\w+)"\)', text))
        used |= set(re.findall(r'itemComponentName="(\w+)"', text))
        used |= set(re.findall(r'CreateScene\("(\w+)"\)', text))
        used |= set(re.findall(r'<(GuideRow)\b', text))
        used |= set(re.findall(r'createChild\("(\w+)"\)', text))
    assert {"MainScene", "GuideRow", "ApiTask", "LineupTask"} <= used
    assert used <= defined | BUILT_IN, used - defined - BUILT_IN


def test_channel_changes_never_override_or_take_a_lock():
    assert not re.search(r"\boverride\s*[:=]", SCENE)
    assert "tuner/lock" not in SCENE
    # A holder already showing the channel is watched along with.
    locked = SCENE[SCENE.index('if p.phase = "locked"\n'):SCENE.index('if p.phase = "navigating"')]
    assert locked.index("Watching along with") < locked.index("ReturnFromLockedTune()")


def test_server_address_is_tv_thing_on_the_lan():
    main = (APP / "source" / "main.brs").read_text()
    assert re.search(r'serverUrl = "http://[0-9.]+:8080"', main)


def test_short_names_match_the_watch_page():
    web = dict(re.findall(r'"([a-z-]+)": "([A-Z]+)"',
                          INDEX[INDEX.index("const ROKU_SHORT"):INDEX.index("let rokuPollTimer")]))
    app = dict(re.findall(r'"([a-z-]+)": "([A-Z]+)"',
                          LINEUP[LINEUP.index("function LineupShortName"):LINEUP.index("function LineupSortKey")]))
    # Manual Roku is the web remote, not a channel, so the app leaves it out.
    web.pop("roku-manual")
    assert app == web


def test_lineup_matches_the_watch_page_order(tmp_path):
    brs_or_skip()
    if shutil.which("node") is None:
        pytest.skip("node is required to run the Watch page's tunable-channels.js")
    web = subprocess.run(
        ["node", "-e", "const {catalog} = require(process.argv[1]);"
         "const f = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
         "console.log(JSON.stringify(catalog(f.channels, f.sources, f.feeds, f.date, f.lineup).map(e => e.id)));",
         str(ROOT / "static" / "tunable-channels.js")],
        input=json.dumps(FIXTURE), capture_output=True, text=True, timeout=30, check=True)
    entries = lineup_entries(tmp_path)
    assert [entry["id"] for entry in entries] == json.loads(web.stdout)


def lineup_entries(tmp_path):
    source = LINEUP + f"""
sub Main()
    f = ParseJson({brs_string(json.dumps(FIXTURE))})
    for each e in LineupBuild(f.channels, f.sources, f.feeds, f.date, f.guide, {NOW}, f.lineup)
        print "ENTRY " + FormatJson({{id: e.id, kind: e.kind, name: e.name, key: e.key, tag: e.tag, detail: e.detail, label: e.label, section: e.section, events: e.events, progress: e.progress, enabled: e.enabled, streamPath: e.streamPath, sourceId: e.sourceId, channelId: e.channelId, date: e.date}})
    end for
end sub
"""
    out = run_brs(tmp_path, source)
    return [json.loads(line.removeprefix("ENTRY ")) for line in out.splitlines() if line.startswith("ENTRY ")]


def test_internet_streams_play_their_relay_with_listings(tmp_path):
    entries = {entry["id"]: entry for entry in lineup_entries(tmp_path)}
    pentulive = entries["internet:pentulive"]
    assert pentulive["key"] == "29" and pentulive["kind"] == "internet" and pentulive["tag"] == "WEB"
    assert pentulive["streamPath"] == "/stream/internet/pentulive/index.m3u8"
    assert pentulive["detail"] == "Yle's puppy cam"   # no listings: its note
    assert pentulive["events"] == [] and pentulive["label"] == "Internet stream"
    cab = entries["internet:cabride"]
    assert cab["key"] == "144"
    assert cab["detail"] == "San'in Line Along the Sea of Japan"
    assert cab["progress"] == 17
    # The guide's listings, in time order, as {start, finish}.
    assert [e["title"] for e in cab["events"]] == ["San'in Line Along the Sea of Japan", "Rural Akita in Spring"]
    assert cab["events"][0]["finish"] == NOW - 547 + 3125
    assert cab["events"][0]["description"] == "JR San'in Main Line"
    # Not in the lineup: listed after the numbered channels, by name.
    assert entries["internet:unlisted"]["key"] == "WEB"
    assert entries["internet:unlisted"]["detail"] == "Internet stream"
    for station in ("vizelvision:0", "vizelvision:70"):
        assert entries[station]["streamPath"] == "/stream/vizelvision/index.m3u8"
    assert entries["vizelvision:0"]["section"] == "House channel"
    assert entries["slides:sports-guide"]["streamPath"] == "/stream/sports-guide/index.m3u8"


def test_tuned_channels_carry_their_listings(tmp_path):
    entries = {entry["id"]: entry for entry in lineup_entries(tmp_path)}
    wjla = entries["digital:wjla_7_1"]
    assert wjla["streamPath"] == "" and wjla["label"] == "Antenna 7.1"
    assert wjla["detail"] == "Jeopardy!"
    # A programme already over is dropped.
    assert [e["title"] for e in wjla["events"]] == ["Jeopardy!"] and wjla["events"][0]["captions"] is True
    wlaj = entries["digital:lansing/wlaj_7_1"]
    assert wlaj["events"] == [] and wlaj["detail"] == "Lansing · Antenna 7.1 · RF 14"
    assert wlaj["label"] == "Lansing 7.1" and wlaj["channelId"] == "lansing/wlaj_7_1"
    nbc = entries["roku:youtube-tv:NBC"]
    assert nbc["detail"] == "Dateline" and nbc["events"][0]["live"] is True and nbc["label"] == "YouTube TV"
    game = entries["roku:mlb:2026-10-25:776"]
    assert game["date"] == "2026-10-25" and game["key"] == "300"
    # A game's row is the guide's games/NUMBER; its sport line stands in for a description.
    assert game["events"][0]["title"] == "Tigers at Guardians"
    assert game["events"][0]["description"] == "MLB Baseball · MLB.TV"
    assert entries["roku:mlb:2026-10-25:777"]["enabled"] is False


def test_guide_grid_rows_cells_and_moves(tmp_path):
    source = (APP / "tests" / "guide_scenarios.brs").read_text() + "\n" + (APP / "components" / "Guide.brs").read_text()
    assert "GUIDE SCENARIOS PASSED" in run_brs(tmp_path, source)


def test_scene_handlers_locks_relays_and_the_remote(tmp_path):
    brs_or_skip()
    # Execute the real handlers; replace only the SceneGraph dialog factory.
    handlers = ["OnTune", "AdvancePending", "ReturnFromLockedTune", "OnStatus",
                "OnLockNoticeChoice", "OnLockNoticeClosed", "LaneStatus", "StreamId",
                "StreamReady", "LockOwner", "Choose", "LaneOf", "OnProbe", "ShowNotice",
                "RetrySeconds", "RelayProblem", "ErrorMessage", "TuneProblem", "Fail",
                "ChannelZero", "GiveUp", "onKeyEvent", "RefreshBanner", "CheckPlayback",
                "Reconnect", "Probe", "LanePath", "IsPoolTuner", "TunedLane"]
    source = (APP / "tests" / "scene_scenarios.brs").read_text()
    for name in handlers:
        match = re.search(rf"^(sub|function) {name}\(.*?^end \1$", SCENE, re.M | re.S)
        assert match is not None, name
        source += "\n" + match.group().replace(
            'CreateObject("roSGNode", "StandardMessageDialog")', "TestDialog()") + "\n"
    assert "SCENE SCENARIOS PASSED" in run_brs(tmp_path, source)


def test_package_script_builds_a_sideloadable_zip(tmp_path):
    if shutil.which("zip") is None:
        pytest.skip("zip is required to package the Roku app")
    copy = tmp_path / "roku_app"
    shutil.copytree(APP, copy, ignore=shutil.ignore_patterns("*.zip"))
    subprocess.run(["bash", str(copy / "package.sh")], check=True, capture_output=True, timeout=30)
    with zipfile.ZipFile(copy / "tv_thing.zip") as package:
        names = set(package.namelist())
    for name in ("manifest", "source/main.brs", "components/MainScene.xml", "components/MainScene.brs",
                 "components/Lineup.brs", "components/Guide.brs", "components/GuideRow.xml",
                 "components/LineupTask.xml", "components/ApiTask.xml", "fonts/DejaVuSans.ttf",
                 "fonts/DejaVuSans-Bold.ttf", "images/cell.9.png", "images/gold.9.png",
                 "images/screen_hd.jpg", "images/banner_hd.jpg", "images/splash_hd.jpg"):
        assert name in names, name
    assert not any(name.endswith((".zip", ".py")) or name.startswith("tests/") for name in names)
    # The source, for keeping: everything but the packages, unpacking to roku_app/.
    with zipfile.ZipFile(copy / "tv_thing-source.zip") as source:
        names = set(source.namelist())
    for name in ("roku_app/package.sh", "roku_app/make_images.py", "roku_app/README.md",
                 "roku_app/components/MainScene.brs", "roku_app/tests/scene_scenarios.brs",
                 "roku_app/fonts/LICENSE.txt"):
        assert name in names, name
    assert not any(name.endswith(".zip") for name in names)
