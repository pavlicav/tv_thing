# TV Thing API and tuner ownership

The supported integration surface is `/api/v1`; discover it with GET
`api/v1` and download its core control contract at `api/v1/openapi.json`.
Existing unversioned endpoints and fields remain compatible. Additional JSON
fields may appear; clients must ignore fields they do not understand.

## Addressing and streams

Configure an application base ending in `/`: `http://viviserv/tv/` through
Apache, or `http://127.0.0.1:8080/` directly. Resolve `stream.playback[].url`
against that base, **not against the API endpoint**. Legacy `playlist_path`
remains root-relative for old direct clients. Never hard-code MediaMTX ports
or manufacture a playlist URL from a channel identifier.

Digital and analog are independent lanes, each with a shared channel and
quality. Reading a playlist does not tune, reserve, or release a tuner.
A lane URL follows that lane through channel changes. It is not a permanent
URL for the channel selected at the time it was obtained.

Each lane's `stream` retains `id`, `active`, `ready`, `playlist_path`, and
`health`, and adds:

* `generation`: alias of `id`; changes when the encoder generation changes.
* `state`: `idle`, `tuning`, `buffering`, `ready`, `navigating` (Roku only), or
  `failed`.
* `error`: null or an object with `code` and `message`.
* `playback`: available protocol/URL/content-type descriptors; currently HLS.
* `shared`: true.

`ready` means the current generation has a fresh playlist referencing complete
media files and no reported failure. It does not mean a particular browser
has downloaded or decoded enough media. The browser separately waits for a
1.8-second continuous buffer before automatic playback. Startup uses four
playlist segments. LIVE is displayed after playback starts, not after parsing
a manifest. Pause is local; resume returns to the live position. There is no
DVR timeline.

## Channel numbers

Every catalog item carries its TV Thing channel number: the antenna
(`/api/v1/sources/digital`), each Roku source (`/api/v1/sources/{source}`) and
the guide's channels (`/api/v1/guide`) add

* `tv_number`: the channel's number in the lineup (an integer);
* `tv_name`: its name there (usually the item's own `name`);
* `tv_section`: the lineup-card heading it sits under.

They are absent when the server's `lineup.json` is unusable. Identity is
unchanged: tune and record by `channel_id` (or the antenna `number`) as before,
never by `tv_number`. A day's games are numbered per date, so a game's number
belongs to the date it was listed for.

`GET /api/v1/sources` adds `lineup`: `{path, sections, house}`, where `house`
lists VizelVision's numbers. `GET /api/v1/lineup[?date=YYYY-MM-DD]` returns
the whole lineup in number order: VizelVision, every antenna channel and every
Roku catalog item, each with `kind`, `source_id`, `channel_id` and (for games)
`date`. A Roku catalog that could not be read is named in `errors` and left out.

## Integration flow

1. GET `api/v1/sources/digital` (or `sources/cable`) and select an item.
2. POST `api/v1/tuner/lock` with `lane`, a descriptive `owner`, and
   `duration_seconds`. Use `until: "duration"` explicitly or omit it.
   Successful acquisition returns a private `token`, `lock.expires_at`,
   `truncated`, and `max_seconds`. Check the actual granted expiry.
3. POST `api/v1/tune` with `source`, selection, `quality`, and that token.
   Acquisition does **not** tune. `channel_id` on a lock is descriptive
   metadata (or the guide lookup for `program_end`), not a tuning instruction.
4. Save the response's `lane` and `stream.id`. Poll GET `api/v1/status` and
   examine `lanes[that_lane].stream`. Wait for the same generation and
   `ready: true`. Fail promptly on `state: "failed"`; use a bounded timeout.
   If the generation changes, rediscover the lane and verify its selection
   and lock ownership before consuming it: recovery may create a generation,
   but another operator may also have deliberately overridden the lock.
5. Consume a returned playback URL. Continue monitoring status during use.
6. Renew by POSTing the same lock endpoint with the same lane, owner, token,
   and duration. Renew well before expiry (for example halfway through the
   granted duration), respecting the returned expiry on every renewal.
7. In cleanup, POST `api/v1/tuner/lock/release` with lane and token. Release
   leaves always-on playback running. Closing a player does not stop a tuner.

Example tune body after acquiring a digital lock:

```json
{"source":"digital","channel_id":"<discovered-id>","quality":"medium","token":"<private-lock-token>"}
```

For analog use `source: "cable"` and `channel: 3` instead of `channel_id`.
Persist tokens securely if the program needs to renew after its own restart;
public status never includes them. TV Thing persists unexpired locks across
its restart. Expired locks confer no ownership; acquire again before tuning.

## Concurrency, retries, and errors

* A conflicting owner receives HTTP 423 and a public `lock` summary. Versioned
  tune errors use `error.code` and `error.message`, including `locked`.
* Tuning checks ownership again under the same mutex used by lock acquisition.
  Scans and guide refresh cannot take a locked tuner. A digital lock request
  during an active scan/guide refresh returns 409; retry later with backoff.
* Same channel and quality on a running, healthy lane (including its initial
  buffering period) returns the same
  generation and `stream_reused: true`. Other successful tunes return false.
  An unhealthy stream can be restarted; this is state-based
  retry safety, **not** an exactly-once request-ID protocol.
* Without a lock, serialized competing tune requests can supersede one another.
  A tune response is a snapshot of its own lane at completion, not a promise
  that its generation will still be current when the response arrives.
* HTTP 400 means invalid input, 403 `not_lock_owner` means release used the
  wrong token, 404 unknown selection, 409 busy, and 500/503
  tuning/unavailability failures. Inspect the body; use bounded backoff for
  transient failures, not repeated immediate retunes.
* If lock acquisition's response is lost, an identical request without its
  returned token does not prove ownership. Wait for expiry or operator help;
  owner names are labels, not credentials.
* Releasing an absent lock is harmless. Renewing/releasing someone else's
  active lock requires the correct token. Recovery of the selected channel
  continues while locked; the lock prevents competing changes, not recovery.

## The Roku lane

The `roku` lane is the source Roku (10.0.0.240) through a Hauppauge HD PVR.
Its stream is the HD PVR's own H.264 (CBR 6.5 Mbit/s, 1280×720 at 60 fps,
a keyframe every 0.53 s) and AAC, copied to `stream/roku.m3u8` in 0.5 s
segments: no transcode, no `quality`. The capture runs all the time, so the
stream is usually `ready`, whatever the Roku shows, menus included. The
Roku has its own lock (`lane: "roku"`), independent of the tuners, and is
not a video lane for the legacy `/stream/live.m3u8` alias.

What the Roku shows is the lane's `selection`; `lanes.roku.roku` adds
`operation` (the last navigation), `player` (ECP's active app and player,
refreshed in the background), `generation` and `reuse_max_age_seconds`.

* **Sources** (`GET api/v1/sources/<id>`, games with `?date=YYYY-MM-DD`):
  `youtube-tv` (channels in Live-guide order, with aliases), `mlb` (the
  day's games and the route each would take: a national YouTube TV channel
  or MLB.TV), `thursday-night-football` (Prime Video), `sunday-ticket`
  (Sunday-afternoon CBS/FOX games), and `roku-manual`. Every item has an
  `id` (the `channel_id` to tune) and a `name`; games carry `game`,
  `state`, `start_time` and `playable`.
  `GET api/v1/sources` also has `listed`: catalogs in the same shape, with
  `tunable: false`, that nothing can tune yet. Today that is `nhl`, the NHL
  games ESPN Unlimited streams in YouTube TV (`GET api/v1/sources/nhl?date=`,
  every item `playable: false` with a `note` on where to find it). They are
  not under `sources`, so a client that tunes what it lists never offers
  them; a tune of one is **400**. A day's games are numbered across the whole
  games block, `nhl` included, so a game's `tv_number` is the same in every
  catalog, the lineup and the guide.
* **Tune**: POST `api/v1/tune` `{"source": "youtube-tv", "channel_id": "ESPN",
  "token": …}` (games add `"date"`). **200** with `reused: true` when the Roku
  already shows that verified selection, recently and on the current capture,
  or after the still-playing check below passes (`resumed: true`). Otherwise
  **202** and an `operation`: the Roku navigates (YouTube TV takes 40–125 s,
  Sunday Ticket up to about 4 min). Poll `lanes.roku.roku.operation` until its
  `state` is `succeeded`, `failed` or `cancelled`. Options: `force: true`
  always navigates; `navigate: false` never does, answering **409**
  `not_prepared` instead; `resume: false` skips the still-playing check.
  A Sunday Ticket game plays from wherever it is watchable: when a local
  station's guide row names both teams (Sunday Ticket blacks those games
  out), or after YouTube TV shows its blackout card ("airing on your local
  station"), the Roku plays the local station instead. The selection stays
  the requested game, with `resolved_source` `{"provider": "youtube_tv",
  "label": "FOX (local station)"}`. Only when no station showing it is found
  does the operation fail with `sunday_ticket_blackout`; for the next 30
  minutes a tune of it then answers **409** `sunday_ticket_blackout` at once,
  unless `force: true`.
* **Generation**: `stream.generation` is `<capture id>.<serial>`. It changes
  when a navigation starts, when the remote moves the Roku, and when the
  capture restarts. A holder of the roku lane must fail closed when it
  changes, then tune again: an unchanged Roku comes back `reused` without
  navigating.
* **Selection**: `source_id`, `channel_id`, `date` (games), `name`,
  `verified_at`, `age_seconds`, `reusable`, `app` (the app it was verified
  in), `resolved_source` (`provider` and `label`), or `{"manual": true}` for
  Manual Roku. Null means the Roku shows something unverified.
* **Still playing**: POST `api/v1/roku/check` asks whether the verified
  selection still plays: the same app, its player playing, and a moving
  picture within 4 s. A pass renews the verification on the current capture.
  It never presses a key and needs no lock.
* **Remote**: POST `api/v1/roku/keypress` `{"key"}` (`Home`, `Back`, `Up`,
  `Down`, `Left`, `Right`, `Select`, `Play`, `Rev`, `Fwd`, `InstantReplay`,
  `Info`, `Search`, `Backspace`, `Enter`), `api/v1/roku/text` `{"text"}`
  (up to 64 characters), and `api/v1/roku/launch` `{"app_id"}` (from GET
  `api/v1/roku/apps`). Any of them makes the selection Manual Roku. They
  answer **409** while a navigation runs; stop it first with POST
  `api/v1/roku/cancel`.
* Every change (tune, remote, cancel, `api/v1/roku/calibrate`) honours the
  roku lock exactly like a tuner change: **423** with the public lock, and an
  `override` only from a person.
* GET `api/v1/roku` is the engineering view: capture, signal and the 720p60
  calibration target, verifier, ECP. GET `api/v1/roku/preview` is an MJPEG
  stream of what the verifier sees; it keeps the decoder running.

Example: hold the Roku for a take, as vizelVision does.

```json
POST api/v1/tuner/lock  {"lane":"roku","owner":"VizelVision master","duration_seconds":90}
POST api/v1/tune        {"source":"youtube-tv","channel_id":"NBC","token":"<token>"}
GET  api/v1/status      -> lanes.roku.roku.operation.state == "succeeded",
                           lanes.roku.selection.channel_id == "NBC",
                           lanes.roku.stream.ready, lanes.roku.stream.generation
```

## Operator override and trust boundary

This API is currently for trusted LAN callers and has no authentication.
Existing `override: true` behavior is retained for the browser's explicit
operator confirmation. An explicit tune/scan/stop override revokes the
previous lease and logs the previous owner; overrides are not an authorization
boundary. An
integration must never automatically set override after a refusal. Do not
expose this API to untrusted callers. Authenticated operator-only overrides
need a separate authentication design before expanding that trust boundary.

The legacy `/api/stop` is a no-op. `/api/v1/stop` stops the requested lane
and enforces its lock; the always-on keeper may subsequently resume digital
playback. Integrations should release ownership instead of stopping shared
playback. Future breaking changes require
an explicit API version change.

## Programme guide

GET `api/v1/guide[?channel=KEY][&hours=N]` returns `channels`, an object
keyed by row, with events that have finished dropped (`hours` keeps those
starting in the next N hours). Three kinds of row share it:

* **Antenna channels**, keyed by antenna number (`7.1`), read off the air
  (PSIP) at the `refresh_times`. A read retunes the digital lane, so it runs
  at night. `available`, `event_count`, `coverage_hours` and the other
  top-level figures describe this guide.
* **YouTube TV's channels**, keyed `youtube-tv/NAME` (the Roku catalog's
  `channel_id`), with `kind: "roku"`, `source_id`, `channel_id` and `listing`
  (where the schedule came from, or null where none is published, as for
  Local Now). The listings are read from the web daily, two days ahead,
  without touching a tuner. `youtube_tv` describes them: `available`,
  `generated_at`, `coverage_hours`, `event_count`, `next_refresh`,
  `last_error` and `failed_channels`. A row whose source failed keeps its
  last listing with `stale: true`. Every YouTube TV row is listed, with or
  without events.
* **Live Sports**, keyed `games/NUMBER` (`kind: "games"`), one row per
  number of the lineup's games block. Each day's games of every game source
  are numbered together by start time, so a row's airings are different
  games, each with its own `source_id`, `channel_id` (the game ID), `date`,
  `episode_title` (the sport and what carries it) and `playable` (whether
  the Roku can be sent to it). An airing runs from the start to the
  projected final; finished games are left out. The `nhl` source is NHL
  games on ESPN Unlimited (out-of-market and ESPN+ exclusives), with
  `exclusive` and, for a team that may be blacked out, `blackout` (the
  markets: `["DC"]`). It is listed only: `playable` is false and a tune of it
  is **400**. `sports` describes them: `available`, `games`, `by_source` and
  `errors` (a schedule that did not answer, by `source_id` and `date`). A
  request for one other row (`?channel=7.1`) reads no schedule and gives
  `sports: null`.

Every row has `key` and `kind` (`digital`, `roku` or `games`) and the
channel-number fields above. Antenna events have `start`, `duration`, `title`, `captions`
and sometimes `description`. YouTube TV events may also have `episode_title`,
`season`, `episode`, `new`, `live` and `rating`. GET `api/v1/guide/now` gives
`now` and `next` per row under the same keys (Live Sports is not in it).
Tune a YouTube TV row through its Roku source (`source: "youtube-tv"`,
`channel_id`), and a game by its airing's `source_id`, `channel_id` and
`date`, never by a row's key.

## Recording

The VCR records the digital lane and the Roku lane into the DLNA media
library. A running recording holds that lane's lock as owner `VCR`; clients
should treat it like any other lock and not override it without asking the
person. Each lane records one thing at a time, and the two lanes record side
by side.

* GET `api/v1/recordings`: `inputs`, `active` (or null), `active_recordings`
  (every lane's), `timers` (scheduled and
  recording), `history`, `rules`, `library` (finished files), `storage`
  (`ready`, `free_bytes`, and `problems[]` each with a `fix`).
  `inputs` lists the lanes that can record, in menu order, each with `lane`,
  `name`, `available`, `modes` (the POST modes it takes), `held_by` (the
  lock owner other than the VCR, or null), `shares_with_holder` (whether a
  timer tapes alongside a holder already showing its target instead of
  waiting), `lead_seconds` (how long a timer can take to reach its target
  before the tape starts), and `sources`: catalogs under `api/v1/sources`,
  each with `id`, `name`, `items_path`, `dated` (listed per `?date=`), and
  `typical_seconds` (a game's usual length, or null). The VCR page builds
  its SOURCE menu from this list.
  A library file named `<tape> (no ads).mp4` is an ad-free copy made by the
  ad stripper, which runs outside TV Thing. Such a file carries `ad_strip`:
  `source` (the original's name), `recorded_seconds`, `kept_seconds` and
  `breaks` (the mid-show breaks it cut). `ad_strip` is null if the stripper's
  report is missing.
* POST `api/v1/recordings` with one of:
  * `{"mode": "manual", "channel_id", "start", "end", "title"?}` (Unix times)
  * `{"mode": "guide", "channel_number", "event_start"}`, padded 1 min before
    and 2 min after
  * `{"mode": "otr"}`: the digital channel now playing, to programme end

  Any mode also takes `"from_live": true`. The timer then copies the live
  lane's own HLS (the same H.264/AAC as `stream/digital.m3u8`, at the lane's quality, with no
  tap and no second encode) instead of encoding the broadcast at recording
  quality. That costs a few percent of a core instead of about one and a
  half. It is meant for tapes made for analysis. A live-encoder restart
  splits the tape into pieces, which are joined when it ends.
  
  Roku timers add `"lane": "roku"`:
  * `{"lane": "roku", "mode": "manual", "target": {"source_id", "channel_id",
    "date"?}, "start", "end", "title"?}`: a YouTube TV channel, a game, or
    `roku-manual` (whatever is on) at a time.
  * `{"lane": "roku", "mode": "otr", "end"?}`: what the Roku shows now, for
    half an hour unless `end` says otherwise.
  * `{"lane": "roku", "mode": "guide", "target": {"source_id": "youtube-tv",
    "channel_id"}, "event_start"}`: a YouTube TV airing from the guide,
    padded like a digital guide timer. It starts `lead_seconds` earlier still,
    so the Roku has reached the channel when the programme starts. The VCR
    page's GUIDE NAME search covers the antenna only, so the Roku input's
    `modes` do not list `guide`.

  The `target` fields may also be given flat, beside `lane`. The VCR page
  sends every manual timer as `{"lane", "mode": "manual", "source_id",
  "channel_id", "date"?, "start", "end", "title"?}`, using an item from one
  of that input's `sources`; the digital lane reads `channel_id` alone. Any
  other `lane` is **400** `invalid_lane`.

  A Roku timer takes the roku lock as `VCR` and navigates when it starts.
  If someone else holds the Roku on that very target, it tapes without the
  lock and stops if the holder moves away. Otherwise it waits and retries.
  It copies `stream/roku.m3u8` from near the live edge once the target is
  verified on screen: H.264 and AAC, about 2.9 GB an hour, never transcoded.

  201 returns `timer`. An overlapping timer on the same lane is **409**
  `conflict` carrying the clashing timer. There is one digital tuner and one
  source Roku, so any overlap on a lane conflicts.
* GET `api/v1/recordings/search?q=`: upcoming guide airings whose title
  contains `q`, each with `scheduled` and `conflict`.
* POST `api/v1/recordings/cancel` `{"id"}`: clears a timer, or stops the
  active recording and keeps what was taped.
* POST `api/v1/recordings/rules` `{"title", "channel_number"?}` records every
  airing of that title. POST `api/v1/recordings/rules/delete` `{"id"}`.
* GET `api/v1/recordings/file?name=` streams one library file to play in a
  browser; byte ranges are honoured for seeking. Add `&download=1` to save
  it instead (`Content-Disposition: attachment`).
* POST `api/v1/recordings/erase` `{"name"}` deletes one library file.

`api/v1/status` carries `recording`: the active timer (digital first), or
null.


### VizelVision station playback

`GET /stream/vizelvision/index.m3u8` relays the existing VizelVision master HLS.
Its relative child playlists and media resolve under `/stream/vizelvision/`.
This is a watch-only station feed, independent of the hardware lane selection;
it does not use `/api/v1/tune` and never acquires or overrides a tuner lock.
Upstream errors propagate as HTTP errors (unreachable: 502). The optional
`TV_THING_VIZELVISION_MEDIA_URL` sets the upstream HLS directory; by default it
is `http://127.0.0.1:6970/v1/master/media/`. Only flat HLS asset filenames are
accepted; query strings and Range headers are forwarded.
