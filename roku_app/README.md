# TV Thing — Roku App

Streams live over-the-air ATSC TV from the TV Thing server to any Roku device
on your LAN. Roku natively supports HLS, so no extra decoding is needed.

## Setup

### 1. Set your server address

Edit [`source/main.brs`](source/main.brs) and change `SERVER_URL` to match your
TV Thing server's LAN IP:

```brightscript
const SERVER_URL = "http://192.168.86.31:8080"
```

Run `python3 server.py` on the host machine first (or make sure the systemd
service is running).

### 2. Enable Developer Mode on the Roku

1. On the Roku remote, press: **Home × 3, Up × 2, Right, Left, Right, Left, Right**
2. Follow the prompts to create a developer password.
3. Note your Roku's IP address (Settings → Network → About).

### 3. Package and sideload

```bash
chmod +x package.sh
./package.sh
```

Then open `http://<ROKU_IP>/` in a browser, log in with the developer password,
and upload `tv_thing.zip` via the Development Application Installer.

## Using the app

| Remote key | Action |
|---|---|
| Up / Down | Navigate channel list |
| OK | Tune to selected channel |
| Back (while playing) | Stop stream, return to channel list |
| Back (idle) | Exit app |

- All `.1` primary channels appear first, then subchannels grouped by station
  (matching the web UI order).
- The channel list stays focused while video plays — you can browse and switch
  channels without any extra steps.
- Video plays on the right half of the screen (letterboxed 16:9).

## Quality

Default quality is `medium` (720p, 1 Mbps H.264). To change it, edit
`m.quality` in [`components/MainScene.brs`](components/MainScene.brs):

```brightscript
m.quality = "low"    ' 480p, 500 kbps
m.quality = "medium" ' 720p, 1000 kbps  ← default
m.quality = "high"   ' native, 2000 kbps
```

## App structure

```
roku_app/
├── manifest                    Roku app metadata
├── source/
│   └── main.brs                Entry point; set SERVER_URL here
├── components/
│   ├── MainScene.xml/brs       Main UI: channel list + video player
│   ├── FetchTask.xml/brs       Background HTTP task (channels/tune/stop)
│   └── ChannelItem.xml         Custom list row renderer
└── images/                     Add icons here for channel store submission
    (optional for sideloading)
```

## Channel store submission

For submission to the Roku Channel Store, you would need to:
- Add channel icons (`mm_icon_focus_hd.png`, `mm_icon_side_hd.png`)
- Add a splash screen (`splash_screen_hd.png`)
- Switch to HTTPS (requires setting up TLS on the TV Thing server)
- Pass Roku certification requirements

For personal LAN use, sideloading is sufficient and HTTP works fine.
