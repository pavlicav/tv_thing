# TV Thing — Code Review

## How It Works

TV Thing is an over-the-air ATSC TV streaming server built around a hardware quirk: the Hauppauge HVR-1600's DVB kernel driver (`cx18`) tunes correctly but its `/dev/dvb` device never produces data. VLC's built-in DVB stack bypasses that broken path entirely and works.

### Data flow

```
Antenna → HVR-1600 (cx18) → cvlc (DVB tune + demux + transcode) → HLS segments → HTTP → hls.js → browser
```

1. `w_scan -fa -c US -X` scans ATSC frequencies and outputs channel data in channels.conf format
2. `parse_channels.py` converts that to `channels.json` with virtual channel numbers
3. `server.py` serves the web UI and API on port 8080
4. When a channel is selected, `cvlc` is spawned to tune the tuner, extract the requested program from the MPEG-TS stream, transcode to H.264/AAC, and write HLS segments to `streams/`
5. The browser loads `live.m3u8` via hls.js and plays the stream

### Component map

| File | Role |
|---|---|
| `server.py` | Main HTTP server, tuner control, rescan SSE |
| `parse_channels.py` | Converts `w_scan` output → `channels.json` |
| `scan_channels.sh` | One-shot shell wrapper for manual scans |
| `static/index.html` | Player UI with channel list |
| `static/rescan.html` | Rescan page with live scan progress |
| `tv-thing.service` | systemd unit |

### VLC command

```
cvlc atsc://frequency=FREQ
     --dvb-adapter=0
     --program=SERVICE_ID
     --no-sout-all
     --sout "#transcode{vcodec=h264,vb=2000,acodec=aac,ab=128,channels=2}
             :std{access=livehttp{seglen=4,delsegs=true,numsegs=5,
                  index=streams/live.m3u8,index-url=segment_###.ts},
                  mux=ts{use-key-frames},dst=streams/segment_###.ts}"
```

4-second segments, 5 kept at a time, old ones deleted. The client waits 5 seconds after the tune call before loading the playlist to let the first segments accumulate.

### Channel numbering

`parse_channels.py` groups channels by RF frequency and assigns virtual channel numbers in two ways:
- **VCT data** (`vct_data.json`): Uses the actual major.minor numbers broadcast in the ATSC Virtual Channel Table (e.g., `7.1`, `7.2`)
- **Fallback**: Derives the RF channel number from the frequency, then numbers sub-channels sequentially (e.g., RF 7 → `7.1`, `7.2`, ...)

### Rescan page

The `/api/rescan/stream` endpoint is a Server-Sent Events stream that runs `w_scan` and feeds live progress to the browser. It parses `w_scan`'s stderr for frequency/lock status and its stdout for found channels. The UI renders a live list of RF groups with locked/no-signal labels and channel names appearing under each group as they're found.

### ATSC frequency table

The server pre-computes center frequencies for RF channels 2–36 (the entire post-repack ATSC band):
- VHF-Lo (ch 2–6): 57–87 MHz
- VHF-Hi (ch 7–13): 177–213 MHz
- UHF (ch 14–36): 473–605 MHz

---

## Bugs

### 1. `checkStatus()` uses wrong field names — reconnect on refresh is broken

**`static/index.html:412–422`**

The `/api/status` endpoint returns:
```json
{"channel": {...channel object...}, "streaming": true, "quality": "medium"}
```

But `checkStatus()` reads fields that don't exist:
```js
if (data.tuned && data.channel_id) {   // data.tuned is always undefined
    currentChannelId = data.channel_id; // data.channel_id is always undefined
    statusEl.textContent = data.channel_name || "Playing"; // same
```

Should be `data.streaming`, `data.channel?.id`, and `data.channel?.name`. As-is, refreshing the page while a stream is active never reattaches the player — the "No Signal" overlay stays up even though VLC is running.

### 2. Race condition: `/api/tune` can contend with `w_scan`

**`server.py:241–391`**

The rescan handler stops any active stream under the lock, then releases it before running `w_scan`. A concurrent `POST /api/tune` could start VLC while `w_scan` is using the DVB device, causing both processes to fight over the tuner.

```python
# Lock released here after stop_streaming()
proc = subprocess.Popen(["w_scan", ...])  # w_scan runs outside the lock
```

The whole `w_scan` run (several minutes) should hold the lock, or there should be a separate "scanning" state that the tune endpoint checks.

### 3. `parse_channels.py` spawned as a subprocess with a bare `python3`

**`server.py:388–391`**

```python
subprocess.run(["python3", str(BASE_DIR / "parse_channels.py")], ...)
```

If the server is running under a conda environment or venv, `python3` on `PATH` may not be the same interpreter. Errors are silently swallowed (`capture_output=True`). Since `parse_channels.parse()` is a plain function, it should just be imported and called directly.

### 4. `scan_channels.sh` merges stdout and stderr

**`scan_channels.sh:12`**

```bash
w_scan -fa -c US -X > "$SCAN_OUTPUT" 2>&1
```

This mixes `w_scan`'s progress/status stderr with channel data stdout in the same file. The `grep -v "^;"` on line 15 partially compensates, but any stderr line that doesn't start with `;` (e.g., frequency lines like `57000: 8VSB ...`) ends up in `channels.conf`. The server's rescan path (`server.py:260`) correctly uses separate pipes; the shell script is inconsistent with that.

---

## Minor Issues

### `log_message` override drops HTTP method and status code

**`server.py:413–414`**

```python
def log_message(self, format, *args):
    print(f"[HTTP] {args[0]}" if args else "")
```

`args[0]` is the request line (e.g., `"GET /api/channels HTTP/1.1"`), which is fine. But `args[1]` (status code) and `args[2]` (response size) are dropped. Standard format would be: `f"[HTTP] {args[0]} {args[1]}"`.

### Thread-safety of `current_channel` and `current_quality` reads

**`server.py:136–143`**

The `/api/status` handler reads `current_channel` and `current_quality` without holding the lock. In CPython the GIL makes this safe in practice, but it's technically a data race. Reads should either hold the lock or the values should be read atomically.

### VLC scaling uses `canvas` filter

**`server.py:89`**

```python
transcode_opts += f",height={preset['scale']},vfilter=canvas{{width=0,height={preset['scale']}}}"
```

`canvas` is a padding filter, not a scale filter. `width=0` lets VLC choose the width. This may produce unexpected results on content with unusual aspect ratios. A `scale` or `croppadd` filter would be more appropriate.

### VLC stderr monitor thread

**`server.py:114–122`**

The monitor thread calls `vlc_proc.stderr.read()` after `wait()`. Since stderr is a `PIPE`, this will block until the pipe closes. If VLC produced a lot of output this could be slow, but in practice the pipe closes with the process. Low risk.

### 5-second fixed buffer delay

**`static/index.html:356`**

```js
setTimeout(() => startPlayback(), 5000);
```

The client always waits 5 seconds after tuning before loading the playlist, regardless of how quickly VLC produces the first segment. This could be replaced with polling for `live.m3u8` existence.
