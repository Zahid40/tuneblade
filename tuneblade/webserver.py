"""HTTP server: browser player page plus live PCM / MP3 / WAV streams.

Browsers on any device (Android, Windows, iOS...) open the page and play the
PC's audio. Chromecast and DLNA renderers are pointed at /stream.mp3.
"""

import json
import logging
import queue
import socket
import struct
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import lameenc

log = logging.getLogger(__name__)

SAMPLE_RATE = 44100
PAGE = (Path(__file__).with_name("web") / "index.html")
MP3_BACKLOG_SECONDS = 1.0  # sent to new MP3 clients so they start without waiting


def local_ip_for(remote: str = "8.8.8.8") -> str:
    """The address of the interface that routes to `remote`."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class _Client:
    def __init__(self, kind: str, peer: str, user_agent: str):
        self.kind = kind  # "pcm" | "mp3" | "wav"
        self.peer = peer
        self.user_agent = user_agent
        self.q: queue.Queue[bytes | None] = queue.Queue(maxsize=400)

    def put(self, data: bytes) -> bool:
        try:
            self.q.put_nowait(data)
            return True
        except queue.Full:
            return False


class WebServer:
    def __init__(self, port: int = 8000, mp3_bitrate: int = 320, on_change=None):
        self.port = port
        self.mp3_bitrate = mp3_bitrate
        self.on_change = on_change
        self.clients: list[_Client] = []
        self._lock = threading.Lock()
        self._feed: queue.Queue = queue.Queue(maxsize=200)
        self._encoder = None
        self._mp3_backlog: deque[bytes] = deque()
        self._mp3_backlog_bytes = 0
        self._httpd = None
        self.error = None

    # ---------------------------------------------------------- lifecycle
    def start(self):
        handler = self._make_handler()
        for port in [self.port] + list(range(self.port + 1, self.port + 20)):
            try:
                self._httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
                self._httpd.daemon_threads = True
                self.port = port
                break
            except OSError:
                continue
        if not self._httpd:
            self.error = "no free port for web streaming"
            log.warning(self.error)
            return
        threading.Thread(target=self._httpd.serve_forever, daemon=True, name="http").start()
        threading.Thread(target=self._pump, daemon=True, name="http-pump").start()
        log.info("web streaming on %s", self.url())

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        with self._lock:
            for c in self.clients:
                c.put(None)

    def url(self, path: str = "/", remote: str = "8.8.8.8") -> str:
        return f"http://{local_ip_for(remote)}:{self.port}{path}"

    def listeners(self) -> list[_Client]:
        with self._lock:
            return list(self.clients)

    # -------------------------------------------------------------- audio
    def feed(self, pcm_bytes: bytes):
        """Called by the streamer for every packet (s16le stereo)."""
        if not self.clients:
            return
        try:
            self._feed.put_nowait(pcm_bytes)
        except queue.Full:
            pass

    def _pump(self):
        while True:
            pcm = self._feed.get()
            with self._lock:
                clients = list(self.clients)
            mp3 = None
            if any(c.kind == "mp3" for c in clients):
                mp3 = self._encode(pcm)
            dead = []
            for c in clients:
                data = mp3 if c.kind == "mp3" else pcm
                if data and not c.put(data):
                    dead.append(c)  # too slow to keep up: cut it loose
            for c in dead:
                log.info("dropping slow client %s", c.peer)
                self._remove(c)
                c.q.queue.clear()
                c.q.put_nowait(None)

    def _encode(self, pcm: bytes) -> bytes:
        if self._encoder is None:
            enc = lameenc.Encoder()
            enc.set_bit_rate(self.mp3_bitrate)
            enc.set_in_sample_rate(SAMPLE_RATE)
            enc.set_channels(2)
            enc.set_quality(2)
            self._encoder = enc
        data = bytes(self._encoder.encode(pcm))
        if data:
            self._mp3_backlog.append(data)
            self._mp3_backlog_bytes += len(data)
            limit = self.mp3_bitrate * 1000 // 8 * MP3_BACKLOG_SECONDS
            while self._mp3_backlog_bytes > limit:
                self._mp3_backlog_bytes -= len(self._mp3_backlog.popleft())
        return data

    def _add(self, c: _Client):
        with self._lock:
            self.clients.append(c)
        self._notify()

    def _remove(self, c: _Client):
        with self._lock:
            if c in self.clients:
                self.clients.remove(c)
        self._notify()

    def _notify(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    # ------------------------------------------------------------ handler
    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "TuneBlade/1.0"

            def log_message(self, fmt, *args):
                log.debug("%s %s", self.address_string(), fmt % args)

            def do_HEAD(self):
                self._route(head=True)

            def do_GET(self):
                self._route(head=False)

            def _route(self, head):
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    self._send_bytes(PAGE.read_bytes(), "text/html; charset=utf-8", head)
                elif path == "/status":
                    body = json.dumps({"listeners": len(server.clients), "sampleRate": SAMPLE_RATE}).encode()
                    self._send_bytes(body, "application/json", head)
                elif path == "/pcm":
                    self._stream("pcm", "application/octet-stream", head)
                elif path in ("/stream.mp3", "/live.mp3"):
                    self._stream("mp3", "audio/mpeg", head)
                elif path == "/stream.wav":
                    self._stream("wav", "audio/wav", head)
                else:
                    self.send_error(404)

            def _send_bytes(self, body, ctype, head):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if not head:
                    self.wfile.write(body)

            def _stream(self, kind, ctype, head):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Connection", "close")
                # DLNA renderers look for these before they agree to play.
                self.send_header("transferMode.dlna.org", "Streaming")
                pn = "DLNA.ORG_PN=MP3;" if kind == "mp3" else ""
                self.send_header("contentFeatures.dlna.org",
                                 f"{pn}DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000")
                if kind == "mp3":
                    self.send_header("icy-name", "TuneBlade")
                self.end_headers()
                if head:
                    return
                self.close_connection = True
                c = _Client(kind, self.client_address[0], self.headers.get("User-Agent", ""))
                try:
                    if kind == "wav":
                        self.wfile.write(_wav_header())
                    elif kind == "mp3":
                        for chunk in list(server._mp3_backlog):
                            self.wfile.write(chunk)
                    server._add(c)
                    log.info("%s listener connected from %s", kind, c.peer)
                    while True:
                        data = c.q.get()
                        if data is None:
                            break
                        self.wfile.write(data)
                except (ConnectionError, OSError):
                    pass
                finally:
                    server._remove(c)
                    log.info("%s listener %s left", kind, c.peer)

        return Handler


def _wav_header() -> bytes:
    # Endless stream: sizes set to the maximum so players keep reading.
    data_size = 0xFFFFFFFF - 36
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, SAMPLE_RATE, SAMPLE_RATE * 4, 4, 16)
            + b"data" + struct.pack("<I", data_size))
