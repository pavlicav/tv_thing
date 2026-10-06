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
* `state`: `idle`, `tuning`, `buffering`, `ready`, or `failed`.
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

## Recording

The VCR records the digital lane into the DLNA media library. A running
recording holds the digital tuner lock as owner `VCR`; clients should treat it
like any other lock and not override it without asking the person.

* GET `api/v1/recordings`: `active` (or null), `timers` (scheduled and
  recording), `history`, `rules`, `library` (finished files), `storage`
  (`ready`, `free_bytes`, and `problems[]` each with a `fix`).
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
  
  201 returns `timer`. An overlapping timer is **409** `conflict` carrying the
  clashing timer. There is one digital tuner, so any overlap conflicts.
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

`api/v1/status` carries `recording`: the active timer, or null.
