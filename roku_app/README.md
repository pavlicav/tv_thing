# TV Thing — Roku App

A cable box for TV Thing, with the website's look: the Watch page's info banner on a channel
change and the Guide page's grid on OK, in the same navy, blue and gold (`static/guide.html`'s
`--stb` palette). The channels are the Watch page's lineup (`static/tunable-channels.js`):
VizelVision, the Sports Guide, the internet streams, every antenna channel, every YouTube TV
channel and today's games, by TV Thing channel number. Numbers, names and listings come from
the server, so lineup changes and new internet streams need no app rebuild.

## Install

1. Turn on developer mode on the Roku (Home ×3, Up ×2, Right, Left, Right, Left, Right) and
   set its developer password.
2. Build the package: `./package.sh` makes `tv_thing.zip`.
3. Open `http://<ROKU_IP>/`, sign in as `rokudev` with that password, and upload the zip in
   the Development Application Installer. A Roku holds one sideloaded app, so this replaces
   whatever is there.

It runs on the living-room TCL (10.0.0.28), which hazel's `roku-autostart.service` opens
whenever the TV is turned on. Never install it on the source Roku (10.0.0.240), the box TV
Thing captures. The server address is `serverUrl` at the top of
[`source/main.brs`](source/main.brs): `http://10.0.0.98:8080`, TV Thing on the LAN the Rokus
share. Every stream is read from that base, at the paths the API gives.

## Downloads

TV Thing serves the latest build and its source, so they can be fetched later from any
browser on the LAN:

* `http://viviserv/tv/roku/tv_thing.zip` (or `http://10.0.0.98:8080/roku/tv_thing.zip`): the
  app, ready to sideload.
* `http://viviserv/tv/roku/tv_thing-source.zip`: this folder and `test_roku_app.py`, to
  build it again.

`static/roku/` holds links to the two zips here, and `package.sh` builds both, so the
downloads are always the last build.

## Remote

| Key | Watching | In the guide |
|---|---|---|
| OK | bring up the guide | watch the highlighted channel |
| Left / Right | channel down / up | the programme before / after |
| Up / Down | channel up / down | the channel above / below (wraps) |
| ◀◀ / ▶▶ | | a page of channels |
| Back | show the banner | leave the guide |
| `*` | show the banner | |
| Play | back to the live edge | |
| Replay | the channel before | |

Left / Right and Up / Down change channel once you stop for a second, so flipping past a few
channels tunes only the one you land on. The banner shows each channel as you pass it.

## Starting up

It starts on the channel watched last with no keypress, or on channel 0 (VizelVision) the
first time. That tunes the channel if its tuner shows something else, but never over a lock:
if the antenna tuner or the source Roku is locked, or the channel will not tune (a game from
another day), it starts on channel 0 instead and its banner says why.

## The screens

* **Watching:** the picture full screen. On a channel change the Watch page's info banner
  comes up along the bottom: the number, call sign and where it comes from; NOW with its time,
  a gold progress bar and the description; LATER, the next three programmes as guide cells;
  and the clock.
* **The guide (OK):** the Guide page full screen. The picture keeps playing in the preview box
  at the top left, beside the highlighted programme's details (channel, title, time and length,
  ON NOW or LIVE, description) and the clock. Below is the grid: 10 channels, two hours across
  in half-hour columns, the highlighted programme in gold, the channel on screen marked gold
  at its left edge, and programmes running off the window marked ◂ and ▸. It scrolls up to six
  hours ahead, never into the past. Channels without listings (internet streams, VizelVision,
  games) are one long cell named for what they play; antenna channels without listings say
  "No information", as on the website.

The face is DejaVu Sans (bold), which is what the website's set-top-box font stack renders in
on viviserv. Its licence is in [`fonts/LICENSE.txt`](fonts/LICENSE.txt).

## Channels

* **Internet streams** (`kind: "internet"` in the lineup, such as Pentulive on 29 and Cab Ride
  on 144) play TV Thing's relay at their `stream_path`
  (`/stream/internet/<id>/index.m3u8`). Nothing is tuned and no lock is involved. A stream
  that publishes listings has them in the guide. When one will not play, the app asks the
  relay why and says so: not live (503, retried every 15 s), its source not answering (502),
  refused here (403) or gone (404).
* **The Sports Guide** (60) plays TV Thing's video of the slides at its `stream_path`. TV Thing
  starts it on demand, so the first seconds may say it is reconnecting.
* **VizelVision** (0 and 70) plays TV Thing's watch-only relay of the station,
  `/stream/vizelvision/index.m3u8`.
* **Antenna channels** tune TV Thing's digital lane (`POST api/v1/tune`, source `digital`) at
  the quality the lane already runs, so a channel change never re-encodes it for everyone
  else. The picture switches once that generation is ready.
* **YouTube TV channels and games** (Baseball, Thursday Night Football, Sunday Ticket, NHL)
  put the source Roku on them. That navigation takes a minute or two (Sunday Ticket up to
  about four). The old picture stays up meanwhile and the banner follows the navigation.
  Finished games are listed but dimmed.
* **Locks.** The app takes none and never overrides one. When a tune is refused (423) and the
  lane already shows that channel, it watches along ("Watching along with VCR"). Otherwise it
  stays on what was on and explains which tuner is locked, who holds it and why.
* **Following.** While you watch a lane, the app follows it: a new generation reattaches the
  player, and a channel someone else changed it to is shown ("Changed on TV Thing").
* **Failures** are said in the banner and the guide's status line, and leave what was on. A
  dropped picture reconnects every 3 s. The Roku's player does not always report a dead
  stream (when TV Thing stops, it can sit buffering or on the last frame with no error), so
  anything but a moving picture for 15 s reconnects too. Each time, the app asks TV Thing for
  the stream's playlist and shows why it will not play. After 90 s without a picture it goes
  to channel 0 with the reason (channel 0 itself keeps trying), except while TV Thing is not
  answering at all (restarting, or stopped for a deploy's stream gate): then it keeps trying
  the same channel, so the picture comes back on its own with TV Thing. If TV Thing is away,
  the guide keeps the channels it last listed and asks again every 15 s; otherwise the
  lineup and listings refresh every 3 minutes.

Not in the lineup, as on the Watch page: Manual Roku (the web page's on-screen remote, not a
channel), analog cable (the Cable page has it) and FM.

## Files

```
roku_app/
├── manifest                 TV Thing, HD, icons and splash
├── package.sh               builds tv_thing.zip
├── make_images.py           draws images/ (needs Pillow)
├── source/main.brs          the server address
├── components/
│   ├── MainScene.xml/.brs   picture, banner, guide, tuning and the remote
│   ├── Guide.brs            the guide grid's rows, cells and moves (pure functions)
│   ├── GuideRow.xml/.brs    one guide row: the channel and its programme cells
│   ├── Lineup.brs           the lineup and its listings, shared by the scene and LineupTask
│   ├── LineupTask.xml/.brs  loads every source and the guide's listings, in parallel
│   └── ApiTask.xml/.brs     one TV Thing request off the render thread
├── images/                  cells, ruler, panel, backdrops, icons and splash
├── fonts/                   DejaVu Sans and DejaVu Sans Bold, with their licence
└── tests/                   BrightScript scenarios run by test_roku_app.py
```

`make_images.py` draws everything in `images/` from the website's palette and the favicon's
set: `/home/vivian/miniconda3/bin/python3 roku_app/make_images.py`. Cells are 9-patches, so
the app stretches them to any width.

BrightScript's `Int()` takes a 32-bit Float, which rounds a Unix time by up to a minute or
so: times stay whole numbers (`LineupSeconds`), never `Int(time)`.

## Checks

`pytest -q test_roku_app.py` (part of the fast suite) checks that everything the app names is
in the package, that every component it creates is defined, that channel changes never send
`override` or take a lock, and that `package.sh` builds a complete zip. With `brs` on `PATH`
(`npm install -g brs`) it also runs the real BrightScript: the lineup is built from the same
fixture as the Watch page's `tunable-channels.js` and must come out in the same order, with
the internet streams' relay paths and every channel's listings; the guide grid's rows, cells
and Left / Right moves; and the scene's handlers against refused tunes, watching along,
relays, a relay that is not live, falling back to channel 0, and the remote's keys. The HTTP,
video and dialog boundaries are doubles; check the actual picture and remote on a Roku.
`npx brighterscript` (`bsc --project roku_app/bsconfig.json`) compiles it.

On a Roku in developer mode, `nc <ROKU_IP> 8085` shows the app's console (the `[TV Thing]`
video errors among it), and the Development Application Installer's Utilities page
(`POST /plugin_inspect`, `mysubmit=Screenshot`) takes a screenshot of the app; the video plane
comes out black.
