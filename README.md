# beaverlab-m1-obs

Get the video off a Beaverlab M1 WiFi microscope and into OBS on Windows, without
running the phone app.

The M1 (the `DDLM1-xxx` variant) doesn't expose RTSP or any standard stream. The
official app talks a Jieli proprietary protocol over the scope's own WiFi. I picked
apart enough of it to:

- do the control handshake and tell the scope to start streaming,
- demux its proprietary UDP video into plain MJPEG, which the script then serves over
  HTTP so OBS can grab it as a Browser source, and
- send the odd device command over the same link — most usefully a soft reboot that
  unsticks a frozen scope (e.g. a dead power button).

Tested against my own unit only (`DDLM1-xxx`, MJPEG at 1080p). Other M1 sub-models
speak different protocols — see "Which M1 is this" below before you assume it works.

## Quick start

Needs Python 3.8+ and a PC joined to the microscope's WiFi (SSID starts with
`DDLM1-`). No pip installs, stdlib only.

```
# first run: pick the WLAN adapter, it gets remembered
python m1_bridge.py --setup

# start the stream and serve it locally
python m1_bridge.py --serve
```

Then in OBS: add a **Browser** source, URL `http://127.0.0.1:8080/`, size 1920x1080.
Done.

Want to eyeball it without OBS? Open `http://127.0.0.1:8080/` in any browser.

Lighter feed: `--width 1280 --height 720` (or 640x480). Framerate tops out around 20
no matter what you ask for.

## Which M1 is this

The app routes by exact WiFi SSID, so the SSID tells you which protocol your scope
speaks. This repo is for **`DDLM1-xxx`** (no hyphen), a Jieli-based unit. If your
SSID is different, the protocol probably is too:

- `DDL-M1` (with the hyphen) — different again, a custom UDP-JPEG thing.
- `DDL-M1S-A` / `DDL-M1S-B` — these actually have a normal RTSP server at
  `rtsp://192.168.1.1:7070/webcam`, so you can point OBS straight at it and skip this
  repo entirely.

Check your SSID first.

## How it works (short version)

- Control is plain TCP on `192.168.1.1:3333`. Frame is `CTP:` + u16le topic length +
  topic + u32le payload length + JSON.
- Send `APP_ACCESS`, the device grants it and hands you a keepalive interval. Ping it
  with `CTP_KEEP_ALIVE` on schedule or it hangs up.
- `OPEN_RT_STREAM` starts the feed. The device ignores the codec you request and
  sends MJPEG.
- Video does **not** arrive as RTP on the ports the app's SDP advertises. It comes as
  a proprietary muxed stream (audio + JPEG) on UDP 2224, behind a 20-byte per-chunk
  header. The script rebuilds the JPEGs and tosses the audio.

The full wire notes are in [PROTOCOL.md](PROTOCOL.md).

## The physical controls (zoom, reboot)

Dug these out of the app. Short version:

- **Zoom** — there's no zoom command, and you don't need one. The scope zooms
  *internally*: the physical zoom buttons act in the camera pipeline, so the
  already-magnified image just shows up in the live stream. Press them and watch.
- **Reboot** — `--reboot` sends a soft `RESET`. Genuinely handy when the scope locks
  up (frozen power button, WiFi gone): it restarts the firmware so the buttons work
  again. There's no true power-off command in the firmware.

Run `python m1_bridge.py --reboot` to unstick a frozen scope, or `--monitor` to
watch what the device pushes.

## What I haven't cracked

- LED brightness. The app controls it over WiFi (a 3-level light setting,
  `ivLight0/1/2`), but it's a Beaverlab-custom topic — obfuscated and tangled with a
  different product in the APK, so I haven't pinned it down. Easiest route is to watch
  TCP 3333 while toggling the app's light. (More light is also the real fix for
  grainy, low-light images.)
- Audio. It's in the 2224 stream (PCM) but I throw it away; I only wanted video.

## Caveats

- Windows-focused: the adapter picker shells out to PowerShell. The networking itself
  is stdlib, so the core would port elsewhere if you swap out adapter selection.
- One datagram on 2224 can carry several sub-chunks. UDP loss just drops a frame,
  which for a microscope is nothing to worry about.
- If `--serve` can't reach `192.168.1.1:3333`, you're on the wrong WiFi or the wrong
  adapter. Re-run `--setup`.

## Disclaimer

Unofficial. Not affiliated with Beaverlab or Jieli. I wrote this to use hardware I own
with software I want. No warranty — if it ruins your afternoon, that's on you.

## License

MIT, see [LICENSE](LICENSE).
