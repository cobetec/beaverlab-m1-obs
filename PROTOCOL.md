# M1 (`DDLM1-218b`) protocol notes

Reverse-engineered from the Android app `com.beaverlab.mic` (decompiled with jadx)
plus live captures off my own scope. The app uses the Jieli
`com.jieli.lib.dv.control` SDK; the parts below are what `m1_bridge.py` reimplements.

## Transport

Control is plain TCP to `192.168.1.1:3333`. The microscope is the WiFi AP and you're
a client on its network.

Frame, both directions, lengths little-endian:

    "CTP:"        4 bytes
    topic_len     u16 le
    topic         <topic_len> bytes, ascii
    payload_len   u32 le
    payload       <payload_len> bytes, JSON

The topic is in the frame header, not the JSON. Outgoing JSON is
`{"op":"PUT","param":{...}}` with op being GET / PUT / NOTIFY. Param values are
strings.

## Handshake

1. you  -> `APP_ACCESS` PUT `{"type":"0","ver":"<appver>"}`
2. dev  -> `APP_ACCESS` (granted)
3. dev  -> `KEEP_ALIVE_INTERVAL` `{"timeout":<ms>}`
4. you  -> `CTP_KEEP_ALIVE` PUT every timeout/6 ms (floor 5000). Device acks with the
   same topic. Skip it and the socket gets closed.

## Starting video

    you -> OPEN_RT_STREAM PUT {"format":"0","w":"1920","h":"1080","fps":"30"}

- format 1 = H264, 0 = JPEG. **My unit ignores the request and always sends MJPEG.**
- Device replies `OPEN_RT_STREAM` with the params it actually chose, e.g.
  `{format:0, w:1920, h:1080, fps:20, rate:10}`.
- Resolution levels (app `jieli/util/d.i`): level0 = 640x480, level1 = 1280x720,
  level2 = 1920x1080. All three work. fps caps at 20.
- `rate` stays 10, not settable from here.
- Rear/pull stream (`OPEN_PULL_RT_STREAM`) is unsupported on my unit
  (`errno 16 CTP_PULL_NOSUPPORT`).

Stop with `CLOSE_RT_STREAM` PUT `{"status":"1"}`.

Heads up: the app's `VIDEO_PARAM` and the 640x480 still-photo params are for
photo/record, not the live feed. Different thing, don't mix them up.

## Where the video actually comes out

The app's SDP advertises RTP on udp 6666 (video) / 1234 (audio), and it even runs a
localhost SDP server. Red herring. The device does not push standard RTP to those
ports. The real media shows up on **UDP 2224** (the native
`nativeCreateClient(2224, ...)` port). After `OPEN_RT_STREAM`, send one byte to
`192.168.1.1:2224` to announce yourself, then read.

It's a proprietary muxed stream (PCM audio + JPEG video) chunked with a 20-byte
little-endian header per sub-chunk:

    [0]      u8   type: 0x01 audio, 0x02 video; +0x80 = last chunk of the frame
    [1]      u8   reserved
    [2:4]    u16  this chunk's payload length
    [4:8]    u32  frame seq (every chunk of one frame shares it)
    [8:12]   u32  total frame size
    [12:16]  u32  this chunk's byte offset within the frame
    [16:20]  u32  reserved

One datagram packs several sub-chunks back to back (video chunks were 1452-byte
payloads in my capture). Reassemble per seq using the offset; when the last chunk
lands you have a whole JPEG. `total` overshoots the JPEG by ~100-300 bytes of
padding, so trim to the final `FF D9` (EOI). Lost UDP just means an incomplete frame,
throw it out.

## Windows gotcha

The announce byte hits a port the device isn't listening on, which bounces back an
ICMP port-unreachable. Windows then raises WSAECONNRESET (10054) on your *next*
recvfrom. It's transient — swallow it and keep reading, don't treat it as fatal.

## Physical buttons / device controls

The device's own photo and record buttons map to plain Jieli control commands
(`DeviceClient.tryToTakePhoto` / `tryToRecordVideo`). You can fire the same
commands over WiFi — the device saves to its own storage and acks on the same
topic:

    you -> PHOTO_CTRL PUT {}              # take one photo (shutter)
    you -> VIDEO_CTRL PUT {"status":"1"}  # start recording
    you -> VIDEO_CTRL PUT {"status":"0"}  # stop recording
    you -> VIDEO_CTRL GET {}              # query current record status

`m1_bridge.py` exposes these as `--photo`, `--record start|stop`, an interactive
`--control` prompt, and `--reboot` (a soft `RESET` — handy to unstick a frozen
device, e.g. an unresponsive power button). **Storage caveat:** units without a
card (mine has no slot at all) just ack these with an SD error, so photo/record
are no-ops there — record in OBS instead. `handle()` reports the error honestly.

### Zoom — no command needed, the device does it

The M1 player (`xht/PlayingActivity` → `YUVImageView`) has **no zoom code at
all**, and no method anywhere in the app both zooms and sends a control topic.
The only `setZoom` (`MJPEGView.setZoom`, a local crop) belongs to a *different*
Beaverlab product. So the M1 does not zoom in software — the device zooms
**internally**: the physical zoom buttons act in the camera pipeline and the
already-magnified image simply arrives in the live stream. That's why `--monitor`
shows nothing on a press (no control message) yet the picture still changes.
Nothing to send and nothing to capture — just watch the stream.

### LED brightness — not solved

The app has a 3-level light control (`ivLight0/1/2`) that works over WiFi, but
it's a custom Beaverlab topic, obfuscated and tangled with the telescope product
in the APK — not cleanly extractable statically. Pinning it down needs a TCP 3333
sniff while toggling the app's light. Until then it isn't controllable from
`m1_bridge.py`. (More light is also the main fix for sensor grain.)

### Detecting a *physical* press (vs. triggering one)

Physical-press detection isn't in the Jieli vocabulary at all. The app models it
in a second, licensed SDK — `com.example.i4seasoncameralib` — via
`CameraConstant` / `CameraEventObserver`:

    EVENT_KEY_PHOTO_RECORDER (5):  KEY_TYPE_TAKEPHOTO=20,
                                   KEY_TYPE_RECODE_BEGIN=21, KEY_TYPE_RECODER_END=22
    EVENT_KEY_ZOOM (6):            KEY_TYPE_ZOOM_MAGNIFY=23, KEY_TYPE_ZOOM_SMALL=24

i4season is also TCP-socket based but its connection target isn't a hardcoded
literal, and these int codes are compile-time-inlined (no clean xref). Tested
with `m1_bridge.py --monitor`: physical presses produce **no** NOTIFY on
`192.168.1.1:3333`, so they are not remotely observable on this unit — they're
handled entirely on-device.

## Still open

- LED brightness: the `ivLight0/1/2` custom topic (see above) — still needs a
  TCP 3333 sniff to identify.
- Audio: it's right there in the 2224 stream, I just don't use it.
