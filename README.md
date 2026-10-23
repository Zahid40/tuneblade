# TuneBlade (Python)

Streams whatever Windows is playing to AirPlay speakers, Chromecast / Google Cast devices, DLNA TVs and renderers, and any web browser (Android, Windows, iPhone, smart TV) — one or several at once.

## Run

**Standalone:** `dist\TuneBlade.exe` is a single self-contained file (no Python needed). Copy it anywhere and double-click.
Rebuild it with `build.bat`. Logs go to `%APPDATA%\TuneBlade\tuneblade.log`; the exe also accepts the command-line options below.

**From source:** double-click `TuneBlade.bat`. On first run it creates `.venv` and installs the dependencies (Python 3.10+ required).

Or from a terminal:

```
.venv\Scripts\python -m tuneblade                 # desktop app
.venv\Scripts\python -m tuneblade --list          # list receivers
.venv\Scripts\python -m tuneblade --connect "Kitchen" --connect 192.168.1.20:5000 --volume 60
```

## Features

- Captures system audio via WASAPI loopback of the default output device (follows device changes).
- Finds AirPlay and Chromecast devices over Bonjour/mDNS and DLNA renderers over SSDP; AirPlay receivers can also be added by IP.
- Web browsers: any device on the same Wi-Fi opens `http://<pc-ip>:8000/` (or scans the QR code in the app). *Low latency* mode uses Web Audio (~0.3 s); *Background* mode plays an MP3 stream that keeps going with the screen off.
- Chromecast and DLNA devices play a live 320 kbps MP3 stream served by the app (a few seconds of delay is normal for these).
- Multi-room: every receiver gets the same RTP clock and sync packets, so they play in lockstep.
- Per-receiver volume plus a master volume.
- Password-protected receivers (RTSP digest auth). Passwords are remembered.
- Adjustable buffer (latency) for weak Wi-Fi; lost packets are resent on request.
- Optional start with Windows; only one instance runs at a time.
- Tray icon: closing the window keeps streaming. Receivers that were connected at exit reconnect on the next start.

Settings are stored in `%APPDATA%\TuneBlade\config.json`.

## Compatibility

Windows Firewall asks on first launch whether TuneBlade may accept connections. Allow it on **Private networks**, or browsers, Chromecast and DLNA devices can't fetch the stream.

AirPlay receivers stay sample-synced with each other. Browsers, Chromecast and DLNA buffer differently, so they are not sample-synced with AirPlay or with each other.

Speaks AirPlay 1 (RAOP, ALAC, unencrypted). That covers AirPort Express, shairport-sync (Raspberry Pi etc.), and most AirPlay-enabled speakers and AV receivers. AirPlay 2-only devices that require HomeKit pairing (HomePod, recent macOS receivers) are not supported yet.

macOS receivers that only allow the current Apple ID return "access denied". On the Mac, set System Settings → General → AirDrop & Handoff → **Allow AirPlay for: Everyone** (or *Anyone on the same network*).

## Layout

| File | Role |
|---|---|
| `tuneblade/capture.py` | WASAPI loopback → FIFO of 16-bit stereo frames |
| `tuneblade/alac.py` | Wraps PCM in ALAC "uncompressed" frames |
| `tuneblade/raop.py` | Per-receiver RTSP session, RTP audio, timing and retransmit services |
| `tuneblade/streamer.py` | Sender clock, packet pacing, fan-out to all sessions |
| `tuneblade/discovery.py` | mDNS (AirPlay, Cast) + SSDP (DLNA) discovery |
| `tuneblade/webserver.py`, `web/index.html` | HTTP server: browser player, live PCM / MP3 / WAV streams |
| `tuneblade/cast.py` | Google Cast session (Default Media Receiver plays our MP3) |
| `tuneblade/dlna.py` | SSDP browser + UPnP AVTransport / RenderingControl session |
| `tuneblade/gui.py` | Tk window and tray icon |

## Tests

```
.venv\Scripts\python tests\test_stream.py
.venv\Scripts\python tests\test_outputs.py
```

`test_outputs.py` checks the web streams (bit-exact PCM, decodable MP3 when ffmpeg is installed), drives a fake DLNA TV end to end, and checks that an unreachable Chromecast reports an error.

Streams a sine wave into two in-process fake receivers and checks the handshake, bit-exact audio, sync packets, timing replies, retransmits and volume. `python tests\fake_receiver.py` runs a fake speaker on the network ("Test Speaker") that writes what it receives to `received.wav`.
