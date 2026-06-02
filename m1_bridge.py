#!/usr/bin/env python3
"""
Beaverlab M1 (DDLM1-218b) -> OBS bridge / protocol probe.

Reimplements the Jieli CTP control protocol (reverse-engineered from
com.beaverlab.mic) so a Windows PC can ask the microscope to start its
realtime stream, then captures the media so we can hand it to OBS/ffmpeg.

Phases:
  1. TCP control handshake on 192.168.1.1:3333 (CTP framing, APP_ACCESS).
  2. Keepalive loop (CTP_KEEP_ALIVE every timeout/6, learned from device).
  3. OPEN_RT_STREAM request (format=0/MJPEG, 1920x1080 by default; the device
     ignores the codec field and always sends MJPEG anyway).
  4. MEDIA PROBE: bind the candidate UDP ports the SDP names (6666 video,
     1234 audio) plus the native client port (2224), nudge the device, and
     log whatever arrives so we can confirm it is standard RTP/H264.

Run:
    python m1_bridge.py --setup    # first run: pick your WLAN adapter (saved)
    python m1_bridge.py --serve    # MAIN: serve http://127.0.0.1:8080 for OBS
    python m1_bridge.py --control  # interactive photo/record (device buttons)
    python m1_bridge.py --photo    # one-shot: take a photo on the device
    python m1_bridge.py --record start   # one-shot: start recording (stop=stop)
    python m1_bridge.py --monitor --seconds 30   # print device pushes only
    python m1_bridge.py            # diagnostic probe, ~20s of capture
    python m1_bridge.py --capture  # framed udp/2224 dump + header table

Physical buttons: the device's own photo/record buttons map to the WiFi
commands PHOTO_CTRL and VIDEO_CTRL{status} (--photo / --record above). On a unit
with no storage they just return an SD error -- recording is done in OBS instead.
Zoom needs no command at all: the device zooms internally (the physical zoom
buttons act in the camera pipeline), so it simply shows up in the live stream.
LED brightness IS adjustable from the app but over a custom/obfuscated topic we
haven't pinned down -- not controllable from here yet (needs a TCP 3333 sniff).

Adapter binding (for multi-NIC PCs, e.g. a dedicated USB WLAN dongle):
    The first run (no m1_config.json) prompts you to pick the adapter on the
    DDLM1-218b WiFi and remembers it by name. Override per-run with
    --adapter "Wi-Fi 2" or --bind-ip 192.168.1.5 ; re-pick with --setup.

OBS: add a Browser source, URL http://127.0.0.1:8080/ , 1920x1080, then run
this script with --serve while on the DDLM1-218b WiFi. Override resolution
with --width/--height (e.g. --width 1280 --height 720 for a lighter feed).

No third-party dependencies (Python 3.8+). PC must be on the DDLM1-218b WiFi.
"""

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

DEVICE_IP = "192.168.1.1"
CTRL_PORT = 3333
CTP_SIG = b"CTP:"

# Media ports taken from the app's SDP (w0/v.java) and configure(6666,1234).
VIDEO_PORT = 6666
AUDIO_PORT = 1234
NATIVE_PORT = 2224  # UDPRTStreamImpl create() port for the front camera

# OPEN_RT_STREAM request params. Live test proved the DDLM1-218b is MJPEG
# (format=0) only: it ignored our format=1 request and sent JPEG on udp/2224.
# The device honours 1920x1080 for the live RT stream (best quality, 0 drops,
# ~20fps); 1280 and 640 widths also work. fps caps at 20 regardless of request.
REQ_FORMAT = 0      # 0 = JPEG (this device), 1 = H264 (unsupported here)
REQ_WIDTH = 1920
REQ_HEIGHT = 1080
REQ_FPS = 30
APP_VERSION = "1.0.0"
HTTP_PORT = 8080    # local MJPEG server for OBS Browser source


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Network-adapter selection. On a multi-NIC PC the OS may route 192.168.1.1
# through the wrong interface, so we bind our sockets to a chosen adapter's
# local IPv4. The choice is remembered by adapter NAME (survives DHCP changes)
# in m1_config.json next to this script.
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "m1_config.json")


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        log(f"Saved adapter choice to {CONFIG_PATH}")
    except OSError as e:
        log(f"WARN: could not write {CONFIG_PATH}: {e}")


def list_adapters():
    """Return [(alias, ipv4), ...] via PowerShell Get-NetIPAddress
    (locale-independent, present on Win10/11). Empty list on failure."""
    ps = ("Get-NetIPAddress -AddressFamily IPv4 | "
          "Where-Object {$_.IPAddress -ne '127.0.0.1'} | "
          "Select-Object InterfaceAlias,IPAddress | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    txt = (out.stdout or "").strip()
    if not txt:
        return []
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    res = []
    for d in data:
        alias, ip = d.get("InterfaceAlias"), d.get("IPAddress")
        if alias and ip:
            res.append((alias, ip))
    return res


def resolve_adapter_ip(alias):
    for a, ip in list_adapters():
        if a == alias:
            return ip
    return None


def choose_adapter_interactive():
    adapters = list_adapters()
    if not adapters:
        log("Could not enumerate adapters (PowerShell Get-NetIPAddress failed).")
        return None
    print("\nAvailable network adapters:")
    for i, (alias, ip) in enumerate(adapters):
        hint = "   <-- looks like the microscope" if ip.startswith("192.168.1.") else ""
        print(f"  [{i}] {alias:28s} {ip}{hint}")
    while True:
        sel = input("\nPick the adapter on the DDLM1-218b WiFi [number]: ").strip()
        if sel.isdigit() and 0 <= int(sel) < len(adapters):
            return adapters[int(sel)][0]
        print("Invalid selection, try again.")


def resolve_bind_ip(args):
    """Decide which local IPv4 to bind to. Order: --bind-ip > --adapter >
    saved config > interactive picker (first run / --setup). Returns an IP
    string, or None to let the OS choose the route."""
    if getattr(args, "bind_ip", None):
        return args.bind_ip
    cfg = load_config()
    alias = getattr(args, "adapter", None) or cfg.get("adapter")
    if getattr(args, "setup", False) or not alias:
        alias = choose_adapter_interactive()
        if alias:
            cfg["adapter"] = alias
            save_config(cfg)
    if not alias:
        log("No adapter selected; letting Windows pick the route.")
        return None
    ip = resolve_adapter_ip(alias)
    if ip is None:
        log(f"Adapter '{alias}' has no IPv4 right now -- connect it to the "
            f"DDLM1-218b WiFi, or re-run with --setup to pick another.")
    else:
        log(f"Using adapter '{alias}' -> {ip}")
    return ip


def make_udp_socket(port, bind_ip=None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_ip or "0.0.0.0", port))
    sock.settimeout(0.5)
    return sock


# ---------------------------------------------------------------------------
# CTP wire format (matches AbstractDeviceSocket.d / TcpCmdReceiver):
#   b"CTP:" | u16_le topic_len | topic | u32_le payload_len | json_payload
# Outgoing PUT/GET payload: {"op":"PUT","param":{...}}  (errno omitted)
# ---------------------------------------------------------------------------
def encode_ctp(topic, op="PUT", params=None):
    topic_b = topic.encode()
    if op:
        obj = {"op": op}
        if params:
            obj["param"] = {k: str(v) for k, v in params.items()}
        # Match the app's exact key order: op first, then param.
        payload = json.dumps(obj, separators=(",", ":")).encode()
    else:
        payload = b""
    return (CTP_SIG
            + struct.pack("<H", len(topic_b)) + topic_b
            + struct.pack("<I", len(payload)) + payload)


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def read_ctp(sock):
    """Read one CTP frame -> (topic, dict). Returns None on EOF."""
    sig = recv_exact(sock, 4)
    if sig is None:
        return None
    if sig != CTP_SIG:
        # Resync: scan forward for the signature.
        log(f"WARN out-of-sync, got {sig!r}, hunting for CTP:")
        window = sig
        while window[-4:] != CTP_SIG:
            b = sock.recv(1)
            if not b:
                return None
            window += b
    tlen = struct.unpack("<H", recv_exact(sock, 2))[0]
    topic = recv_exact(sock, tlen).decode(errors="replace")
    plen = struct.unpack("<I", recv_exact(sock, 4))[0]
    payload = recv_exact(sock, plen) if plen else b""
    obj = {}
    if payload:
        try:
            obj = json.loads(payload.decode(errors="replace"))
        except json.JSONDecodeError:
            obj = {"_raw": payload.decode(errors="replace")}
    return topic, obj


class ControlClient(threading.Thread):
    def __init__(self, open_stream=True, width=REQ_WIDTH, height=REQ_HEIGHT,
                 fps=REQ_FPS, bind_ip=None):
        super().__init__(daemon=True)
        self.open_stream = open_stream
        self.req_w = width
        self.req_h = height
        self.req_fps = fps
        self.bind_ip = bind_ip
        self.sock = None
        self.running = False
        self.keepalive_ms = 5000
        self._ka_thread = None
        self.rt_open = threading.Event()
        self.rt_params = {}
        # Set once the device grants APP_ACCESS; control commands wait on it.
        self.access_granted = threading.Event()
        # Tracks recording state from VIDEO_CTRL acks (None = unknown).
        self.recording = None

    def connect(self):
        src = (self.bind_ip, 0) if self.bind_ip else None
        log(f"Connecting control TCP {DEVICE_IP}:{CTRL_PORT} "
            f"{'via ' + self.bind_ip if self.bind_ip else ''}...")
        self.sock = socket.create_connection(
            (DEVICE_IP, CTRL_PORT), timeout=5, source_address=src)
        self.sock.settimeout(1.0)
        self.running = True
        log("Control connected.")

    def send(self, topic, op="PUT", params=None):
        frame = encode_ctp(topic, op, params)
        self.sock.sendall(frame)
        log(f"  -> {topic} {op} {params or ''}")

    def run(self):
        # Handshake: request device access (APP_ACCESS PUT type=0, ver=...).
        self.send("APP_ACCESS", "PUT", {"type": "0", "ver": APP_VERSION})
        while self.running:
            try:
                frame = read_ctp(self.sock)
            except socket.timeout:
                continue
            except OSError as e:
                if self.running:
                    log(f"Control socket error: {e}")
                break
            if frame is None:
                log("Control connection closed by device.")
                break
            topic, obj = frame
            self.handle(topic, obj)

    def handle(self, topic, obj):
        if topic == "CTP_KEEP_ALIVE":
            return  # heartbeat ack
        log(f"  <- {topic} {obj}")
        param = obj.get("param", {}) if isinstance(obj, dict) else {}
        if topic == "KEEP_ALIVE_INTERVAL":
            timeout = int(param.get("timeout", 30000))
            self.keepalive_ms = max(timeout // 6, 5000)
            self.start_keepalive()
        elif topic == "APP_ACCESS":
            self.access_granted.set()
            if self.open_stream:
                log("Device GRANTED access. Requesting OPEN_RT_STREAM ...")
                self.open_rt_stream()
            else:
                log("Device GRANTED access.")
        elif topic in ("OPEN_RT_STREAM", "OPEN_PULL_RT_STREAM"):
            self.rt_params = param
            self.rt_open.set()
            log(f"*** STREAM OPENED: format={param.get('format')} "
                f"{param.get('w')}x{param.get('h')} fps={param.get('fps')} "
                f"rate={param.get('rate')} ***")
        elif topic == "PHOTO_CTRL":
            err = obj.get("errno") if isinstance(obj, dict) else None
            if err:
                log(f"*** PHOTO FAILED: {param.get('msg', err)} "
                    f"-- is a TF card inserted? ***")
            else:
                log("*** PHOTO taken (saved to device TF card) ***")
        elif topic == "VIDEO_CTRL":
            err = obj.get("errno") if isinstance(obj, dict) else None
            if err:
                log(f"*** RECORD command FAILED: {param.get('msg', err)} "
                    f"-- is a TF card inserted? ***")
            else:
                # ack carries the new status; remember it so --record toggle works.
                st = param.get("status")
                if st is not None:
                    self.recording = (str(st) == "1")
                log(f"*** RECORD {'STARTED' if self.recording else 'STOPPED'} ***")

    def start_keepalive(self):
        if self._ka_thread:
            return
        log(f"Keepalive every {self.keepalive_ms} ms")
        def loop():
            while self.running:
                time.sleep(self.keepalive_ms / 1000.0)
                try:
                    self.send("CTP_KEEP_ALIVE", "PUT", None)
                except OSError:
                    break
        self._ka_thread = threading.Thread(target=loop, daemon=True)
        self._ka_thread.start()

    def open_rt_stream(self):
        self.send("OPEN_RT_STREAM", "PUT", {
            "format": REQ_FORMAT,
            "w": self.req_w,
            "h": self.req_h,
            "fps": self.req_fps,
        })

    # -- Physical-button equivalents over the Jieli control link ------------
    # These are the WiFi commands the app sends for the device's own photo /
    # record buttons (DeviceClient.tryToTakePhoto / tryToRecordVideo). The
    # device saves to its own storage and acks on the same topic -- on a unit
    # with no card it acks with an SD error (handle() reports that honestly).
    #   PHOTO_CTRL PUT {}              -> shutter (take one photo)
    #   VIDEO_CTRL PUT {status:1|0}    -> start / stop recording
    # NOTE: there is no zoom command -- the device zooms internally and the
    # result arrives in the live stream. And no LED-brightness command is known
    # (custom/obfuscated topic; would need a TCP 3333 sniff to identify).

    def wait_ready(self, timeout=8):
        """Block until the device has granted APP_ACCESS (commands need it)."""
        return self.access_granted.wait(timeout)

    def take_photo(self):
        self.send("PHOTO_CTRL", "PUT", None)

    def record(self, start):
        self.send("VIDEO_CTRL", "PUT", {"status": "1" if start else "0"})

    def toggle_record(self):
        # If we've never seen a state, assume not recording and start.
        self.record(not bool(self.recording))

    def request_record_state(self):
        # Device replies on VIDEO_CTRL; handle() updates self.recording.
        self.send("VIDEO_CTRL", "GET", None)

    def reboot(self):
        # Jieli DeviceClient.tryToResetDev: a soft reboot (RESET PUT, no params).
        # NOT a factory reset -- that would be SYSTEM_DEFAULT {"def":"1"}, which
        # we intentionally do not send. There is no true power-off command in
        # this firmware; rebooting clears a frozen input/power-button handler.
        self.send("RESET", "PUT", None)

    def close(self):
        self.running = False
        try:
            if self.sock:
                self.send("CLOSE_RT_STREAM", "PUT", {"status": "1"})
                self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Media probe: listen on the SDP-named ports and classify the first packets.
# ---------------------------------------------------------------------------
def classify(data):
    if len(data) >= 12 and (data[0] >> 6) == 2:  # RTP version 2
        pt = data[1] & 0x7F
        seq = struct.unpack(">H", data[2:4])[0]
        ts = struct.unpack(">I", data[4:8])[0]
        ssrc = struct.unpack(">I", data[8:12])[0]
        names = {26: "JPEG", 96: "H264?", 97: "L16-audio?"}
        return f"RTP pt={pt}({names.get(pt,'?')}) seq={seq} ts={ts} ssrc={ssrc:08x}"
    if data[:3] == b"\xff\xd8\xff":
        return "JPEG SOI (raw MJPEG)"
    if data[:4] == b"\x00\x00\x00\x01" or data[:3] == b"\x00\x00\x01":
        return "H264 Annex-B NAL (raw)"
    return "unknown/custom"


class UdpProbe(threading.Thread):
    def __init__(self, port, label, dump=False, bind_ip=None):
        super().__init__(daemon=True)
        self.port = port
        self.label = label
        self.dump = dump
        self.count = 0
        self.bytes = 0
        self.first_logged = False
        self.sock = make_udp_socket(port, bind_ip)
        self.running = True
        self._fh = open(f"raw_{label}_{port}.bin", "wb") if dump else None

    def nudge(self):
        """Some Jieli firmwares stream to whoever pinged the port first."""
        try:
            self.sock.sendto(b"\x00", (DEVICE_IP, self.port))
        except OSError:
            pass

    def run(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            self.count += 1
            self.bytes += len(data)
            if self._fh:
                self._fh.write(data)
            if not self.first_logged:
                self.first_logged = True
                log(f"  [{self.label}:{self.port}] FIRST packet from {addr[0]}:{addr[1]} "
                    f"len={len(data)} :: {classify(data)}")
                log(f"        hex: {data[:32].hex(' ')}")

    def stop(self):
        self.running = False
        if self._fh:
            self._fh.close()


def write_sdp(rt_params):
    fmt = int(rt_params.get("format", REQ_FORMAT))
    rate = int(rt_params.get("rate", 8000))
    fps = int(rt_params.get("fps", REQ_FPS))
    vpt, vcodec = (96, "H264") if fmt == 1 else (26, "JPEG")
    sdp = (
        "v=0\n"
        "o=- 0 0 IN IP4 127.0.0.1\n"
        "s=M1\n"
        "c=IN IP4 127.0.0.1\n"
        "t=0 0\n"
        f"m=video {VIDEO_PORT} RTP/AVP {vpt}\n"
        f"a=rtpmap:{vpt} {vcodec}/90000\n"
        f"a=framerate:{fps}\n"
        f"m=audio {AUDIO_PORT} RTP/AVP 97\n"
        f"a=rtpmap:97 L16/{rate}/1\n"
        "a=ptime:20\n"
    )
    with open("m1.sdp", "w") as f:
        f.write(sdp)
    log("Wrote m1.sdp. Try:  ffplay -protocol_whitelist file,udp,rtp m1.sdp")
    log("  (or add m1.sdp as a Media Source in OBS / open in VLC)")


def capture_2224(args):
    """Framed capture of the muxed stream on udp/2224 so we can reverse the
    20-byte header offline. Records each datagram as [u32LE len][payload] into
    packets_2224.bin and prints a header table for the first packets."""
    seconds = args.seconds
    bind_ip = getattr(args, "resolved_bind_ip", None)
    sock = make_udp_socket(NATIVE_PORT, bind_ip)

    ctrl = ControlClient(bind_ip=bind_ip)
    try:
        ctrl.connect()
    except OSError as e:
        log(f"FATAL: cannot reach control port ({e}). On DDLM1-218b WiFi?")
        return 1
    ctrl.start()
    ctrl.rt_open.wait(timeout=8)
    sock.sendto(b"\x00", (DEVICE_IP, NATIVE_PORT))  # nudge

    fh = open("packets_2224.bin", "wb")
    n = 0
    printed = 0
    deadline = time.time() + seconds
    log("idx   len  hdr[0:20]                                          firstpayload")
    while time.time() < deadline:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            continue  # transient Windows UDP reset from the nudge; keep going
        fh.write(struct.pack("<I", len(data)) + data)
        n += 1
        if printed < 40:
            printed += 1
            hdr = data[:20].hex(' ')
            pl = data[20:30].hex(' ')
            log(f"{n:4d} {len(data):5d}  {hdr}  {pl}")
    fh.close()
    log(f"Captured {n} packets -> packets_2224.bin")
    ctrl.close()
    sock.close()
    return 0


# ---------------------------------------------------------------------------
# MJPEG reassembler: demux the proprietary udp/2224 stream into JPEG frames.
# Sub-chunk header (20 bytes, little-endian) -- confirmed against live capture:
#   [0]    u8  type:  0x01=audio 0x02=video ; +0x80 = LAST chunk of frame
#   [1]    u8  reserved (0)
#   [2:4]  u16 this chunk's payload length
#   [4:8]  u32 frame seq (all chunks of one frame share it)
#   [8:12] u32 total frame size
#   [12:16]u32 byte offset of this chunk within the frame
#   [16:20]u32 reserved (0)
# A datagram packs 1..N sub-chunks back to back. Video frames are full JPEGs
# starting ff d8 ff e0. UDP loss simply yields incomplete frames we drop.
# ---------------------------------------------------------------------------
class MjpegReassembler:
    def __init__(self, on_frame):
        self.on_frame = on_frame      # callback(jpeg_bytes)
        self.frames = {}              # seq -> [buf, got_bytes, total]
        self.frames_done = 0
        self.frames_dropped = 0

    def feed(self, datagram):
        o = 0
        n = len(datagram)
        while o + 20 <= n:
            typ = datagram[o]
            plen = struct.unpack_from("<H", datagram, o + 2)[0]
            seq = struct.unpack_from("<I", datagram, o + 4)[0]
            tot = struct.unpack_from("<I", datagram, o + 8)[0]
            coff = struct.unpack_from("<I", datagram, o + 12)[0]
            payload = datagram[o + 20:o + 20 + plen]
            o += 20 + plen
            if (typ & 0x7F) != 0x02:      # only video here; ignore audio
                continue
            self._video(typ, seq, tot, coff, payload)

    def _video(self, typ, seq, tot, coff, payload):
        ent = self.frames.get(seq)
        if ent is None:
            if coff != 0:                 # joined mid-frame; can't complete it
                return
            if tot == 0 or tot > 8_000_000:
                return
            ent = [bytearray(tot), 0, tot]
            self.frames[seq] = ent
            # Evict stale partial frames (anything older than this one).
            for old in [s for s in self.frames if s < seq]:
                del self.frames[old]
                self.frames_dropped += 1
        buf, got, total = ent
        if coff + len(payload) > total:
            return
        buf[coff:coff + len(payload)] = payload
        ent[1] = got + len(payload)
        if (typ & 0x80) and ent[1] >= total:
            del self.frames[seq]
            if bytes(buf[:2]) == b"\xff\xd8":
                # `tot` overshoots the JPEG by a small padding tail; trim to EOI.
                eoi = buf.rfind(b"\xff\xd9")
                jpeg = bytes(buf[:eoi + 2]) if eoi >= 0 else bytes(buf)
                self.frames_done += 1
                self.on_frame(jpeg)


class FrameHub:
    """Holds the latest JPEG and wakes HTTP streamers when a new one lands."""
    def __init__(self):
        self.cond = threading.Condition()
        self.jpeg = None
        self.version = 0

    def publish(self, jpeg):
        with self.cond:
            self.jpeg = jpeg
            self.version += 1
            self.cond.notify_all()

    def wait_next(self, last_version, timeout=5.0):
        with self.cond:
            if self.version == last_version:
                self.cond.wait(timeout)
            return self.jpeg, self.version


# Preview page: just the full-bleed MJPEG feed. No zoom/photo/record controls --
# the device zooms internally (physical buttons act in the camera pipeline and
# show up in the stream), and recording is done in OBS.
INDEX_HTML = (
    "<!doctype html><html><head><meta charset=utf-8><title>M1</title>"
    "<style>html,body{margin:0;background:#000;height:100%}"
    "img{display:block;width:100vw;height:100vh;object-fit:contain}</style>"
    "</head><body><img src='/stream'></body></html>"
)


def make_http_handler(hub):
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = INDEX_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/snapshot":
                jpeg = hub.jpeg
                if not jpeg:
                    self.send_error(503, "no frame yet")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.end_headers()
                self.wfile.write(jpeg)
                return
            if self.path == "/stream":
                self.send_response(200)
                self.send_header("Cache-Control", "no-cache")
                self.send_header(
                    "Content-Type",
                    "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                ver = 0
                try:
                    while True:
                        jpeg, ver = hub.wait_next(ver, timeout=5.0)
                        if not jpeg:
                            continue
                        self.wfile.write(b"--frame\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(
                            f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            self.send_error(404)

    return Handler


def serve_mode(args):
    """Full pipeline: control handshake -> open MJPEG stream -> reassemble
    udp/2224 -> serve http://127.0.0.1:8080 for an OBS Browser source."""
    import http.server

    hub = FrameHub()
    stats = {"pkts": 0, "bytes": 0}
    reasm = MjpegReassembler(hub.publish)

    bind_ip = getattr(args, "resolved_bind_ip", None)
    sock = make_udp_socket(NATIVE_PORT, bind_ip)

    ctrl = ControlClient(open_stream=True, width=args.width,
                         height=args.height, fps=args.fps, bind_ip=bind_ip)
    try:
        ctrl.connect()
    except OSError as e:
        log(f"FATAL: cannot reach control port ({e}). On DDLM1-218b WiFi?")
        return 1
    ctrl.start()
    if not ctrl.rt_open.wait(timeout=8):
        log("WARN: no OPEN_RT_STREAM confirmation; listening anyway.")
    sock.sendto(b"\x00", (DEVICE_IP, NATIVE_PORT))  # nudge media to us

    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", HTTP_PORT), make_http_handler(hub))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    res = f"{ctrl.rt_params.get('w','?')}x{ctrl.rt_params.get('h','?')}"
    log(f"MJPEG server up. In OBS add a Browser source -> URL "
        f"http://127.0.0.1:{HTTP_PORT}/  (device sending {res}).")
    log(f"  Preview in any browser: http://127.0.0.1:{HTTP_PORT}/")
    log("  Ctrl+C to stop.")

    last_report = time.time()
    err_streak = 0
    try:
        while True:
            try:
                data, _ = sock.recvfrom(65535)
                err_streak = 0
            except socket.timeout:
                if not ctrl.running:
                    log("Control thread died; stopping.")
                    break
                continue
            except OSError as e:
                # Windows reports the ICMP port-unreachable from our nudge as
                # WSAECONNRESET(10054) on the NEXT recvfrom. It's transient --
                # keep listening; only bail if it never recovers.
                err_streak += 1
                if err_streak > 20:
                    log(f"UDP socket failing repeatedly ({e}); stopping.")
                    break
                continue
            stats["pkts"] += 1
            stats["bytes"] += len(data)
            reasm.feed(data)
            now = time.time()
            if now - last_report >= 5.0:
                log(f"  frames={reasm.frames_done} dropped={reasm.frames_dropped} "
                    f"pkts={stats['pkts']} ({stats['bytes']//1024} KiB)")
                last_report = now
    except KeyboardInterrupt:
        log("Stopping ...")
    finally:
        httpd.shutdown()
        ctrl.close()
        sock.close()
    log(f"Done. Delivered {reasm.frames_done} JPEG frames.")
    return 0


def monitor_mode(args):
    """Connect + keepalive WITHOUT opening the stream, and print every message
    the device pushes. Press the physical light/zoom buttons to see if the
    device emits any NOTIFY (proves whether they are remotely observable)."""
    ctrl = ControlClient(open_stream=False,
                         bind_ip=getattr(args, "resolved_bind_ip", None))
    try:
        ctrl.connect()
    except OSError as e:
        log(f"FATAL: cannot reach control port ({e}). On DDLM1-218b WiFi?")
        return 1
    ctrl.start()
    log(f"Monitoring for {args.seconds}s. Press the light wheel and zoom "
        f"buttons now; watch for new lines below.")
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    ctrl.close()
    log("Monitor done.")
    return 0


def control_mode(args):
    """Connect + keepalive (no video stream) and trigger the device's physical
    photo/record buttons over WiFi. One-shot with --photo / --record, otherwise
    an interactive prompt: p=photo, r=toggle record, s=record start, x=stop, q=quit."""
    ctrl = ControlClient(open_stream=False,
                         bind_ip=getattr(args, "resolved_bind_ip", None))
    try:
        ctrl.connect()
    except OSError as e:
        log(f"FATAL: cannot reach control port ({e}). On DDLM1-218b WiFi?")
        return 1
    ctrl.start()
    if not ctrl.wait_ready(timeout=8):
        log("WARN: no APP_ACCESS grant; sending anyway.")

    # One-shot mode.
    if args.photo or args.record or args.reboot:
        if args.reboot:
            log("Sending RESET (soft reboot) -- device will restart in a few seconds.")
            ctrl.reboot()
        if args.photo:
            ctrl.take_photo()
        if args.record:
            ctrl.record(args.record == "start")
        time.sleep(1.5)          # let the device ack before we drop the link
        ctrl.close()
        return 0

    # Interactive mode.
    log("Control ready. Keys: [p]hoto  [r]ecord toggle  [s]tart  [x]stop  [q]uit")
    try:
        while ctrl.running:
            cmd = input("m1> ").strip().lower()
            if cmd in ("q", "quit", "exit"):
                break
            elif cmd in ("p", "photo"):
                ctrl.take_photo()
            elif cmd in ("r", "rec", "record"):
                ctrl.toggle_record()
            elif cmd in ("s", "start"):
                ctrl.record(True)
            elif cmd in ("x", "stop"):
                ctrl.record(False)
            elif cmd:
                print("  keys: p=photo  r=toggle  s=start  x=stop  q=quit")
            time.sleep(0.3)       # give the ack time to print inline
    except (KeyboardInterrupt, EOFError):
        pass
    ctrl.close()
    log("Control done.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sdp", action="store_true", help="write m1.sdp for ffmpeg/OBS")
    ap.add_argument("--dump", action="store_true", help="dump raw packets to raw_*.bin")
    ap.add_argument("--seconds", type=int, default=20, help="capture duration")
    ap.add_argument("--capture", action="store_true",
                    help="framed per-packet capture of udp/2224 -> packets_2224.bin + header table")
    ap.add_argument("--serve", action="store_true",
                    help="reassemble MJPEG and serve http://127.0.0.1:8080 for OBS")
    ap.add_argument("--monitor", action="store_true",
                    help="connect+keepalive only, print all device messages (no stream)")
    ap.add_argument("--control", action="store_true",
                    help="interactive photo/record control over WiFi (no stream)")
    ap.add_argument("--photo", action="store_true",
                    help="one-shot: take a photo on the device, then exit")
    ap.add_argument("--record", choices=("start", "stop"),
                    help="one-shot: start/stop recording on the device, then exit")
    ap.add_argument("--reboot", action="store_true",
                    help="one-shot: soft-reboot the device (RESET) to clear a "
                         "lockup, e.g. an unresponsive power button, then exit")
    ap.add_argument("--width", type=int, default=REQ_WIDTH,
                    help="OPEN_RT_STREAM width (try 1920 for 1080p)")
    ap.add_argument("--height", type=int, default=REQ_HEIGHT,
                    help="OPEN_RT_STREAM height (try 1080 for 1080p)")
    ap.add_argument("--fps", type=int, default=REQ_FPS, help="requested fps")
    ap.add_argument("--adapter", metavar="NAME",
                    help="bind to this WLAN adapter (InterfaceAlias), e.g. \"Wi-Fi 2\"")
    ap.add_argument("--bind-ip", metavar="IP",
                    help="bind sockets to this exact local IPv4 (overrides --adapter)")
    ap.add_argument("--setup", action="store_true",
                    help="re-run the interactive adapter picker and save the choice")
    args = ap.parse_args()

    # Pick which local interface to bind to (saved in m1_config.json).
    args.resolved_bind_ip = resolve_bind_ip(args)

    if args.setup and not (args.serve or args.monitor or args.capture
                           or args.control or args.photo or args.record
                           or args.reboot):
        log("Adapter configured. Run 'python m1_bridge.py --serve' to start.")
        return 0

    if args.serve:
        return serve_mode(args)
    if args.monitor:
        return monitor_mode(args)
    if args.control or args.photo or args.record or args.reboot:
        return control_mode(args)
    if args.capture:
        return capture_2224(args)

    bind_ip = args.resolved_bind_ip
    # Start media listeners BEFORE opening the stream so we miss nothing.
    probes = [
        UdpProbe(VIDEO_PORT, "video", args.dump, bind_ip),
        UdpProbe(AUDIO_PORT, "audio", args.dump, bind_ip),
        UdpProbe(NATIVE_PORT, "native", args.dump, bind_ip),
    ]
    for p in probes:
        p.start()

    ctrl = ControlClient(bind_ip=bind_ip)
    try:
        ctrl.connect()
    except OSError as e:
        log(f"FATAL: cannot reach {DEVICE_IP}:{CTRL_PORT} ({e}).")
        log("Are you connected to the DDLM1-218b WiFi network?")
        return 1
    ctrl.start()

    # Wait for the stream to open, then nudge the media ports.
    if ctrl.rt_open.wait(timeout=8):
        for p in probes:
            p.nudge()
    else:
        log("WARN: no OPEN_RT_STREAM confirmation; capturing anyway.")
        for p in probes:
            p.nudge()

    log(f"Capturing media for {args.seconds}s ... (Ctrl+C to stop early)")
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass

    log("--- PROBE SUMMARY ---")
    for p in probes:
        verdict = "NO DATA" if p.count == 0 else f"{p.count} pkts, {p.bytes} bytes"
        log(f"  {p.label} udp/{p.port}: {verdict}")
        p.stop()

    if args.sdp and any(p.count for p in probes):
        write_sdp(ctrl.rt_params)

    ctrl.close()
    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
