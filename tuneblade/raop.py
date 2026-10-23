"""AirPlay 1 (RAOP) sender: RTSP session + RTP audio/control/timing over UDP."""

import hashlib
import logging
import random
import re
import socket
import struct
import threading
import time
from collections import OrderedDict

from .alac import FRAMES_PER_PACKET

log = logging.getLogger(__name__)

SAMPLE_RATE = 44100
NTP_EPOCH_OFFSET = 2208988800
USER_AGENT = "iTunes/7.6.2 (Windows; N;)"

# One monotonic-anchored wall clock shared by every session, so all receivers
# agree on what "now" is. That shared clock is what keeps multi-room in sync.
_wall0 = time.time()
_mono0 = time.monotonic()


def now_seconds() -> float:
    return _wall0 + (time.monotonic() - _mono0)


def ntp_bytes(t: float) -> bytes:
    t += NTP_EPOCH_OFFSET
    sec = int(t)
    frac = int((t - sec) * (1 << 32)) & 0xFFFFFFFF
    return struct.pack(">II", sec & 0xFFFFFFFF, frac)


class RaopError(Exception):
    pass


class RtspResponse:
    def __init__(self, code: int, reason: str, headers: dict, body: bytes):
        self.code, self.reason, self.headers, self.body = code, reason, headers, body

    def header(self, name: str, default=None):
        return self.headers.get(name.lower(), default)


class RaopSession:
    """One connected receiver.

    The streamer pushes every packet into every session; each session keeps its
    own sockets and a short history so it can answer retransmit requests.
    """

    HISTORY = 1024  # packets (~8s) kept for resend requests

    def __init__(self, receiver, password: str | None = None, latency_frames: int = 88200,
                 on_closed=None):
        self.receiver = receiver
        self.password = password
        self.latency = latency_frames
        self.on_closed = on_closed

        self.sock: socket.socket | None = None
        self.cseq = 0
        self.session_id = None
        self.url = None
        self.local_ip = None
        self._auth = None  # (realm, nonce) once challenged
        self._rtsp_lock = threading.Lock()

        self.server_port = None
        self.control_port = None
        self.timing_port = None
        self.audio_sock = None
        self.control_sock = None
        self.timing_sock = None

        self.ssrc = random.getrandbits(32)
        self.history: OrderedDict[int, bytes] = OrderedDict()
        self.first_packet = True
        self.connected = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

        self.dacp_id = "%016X" % random.getrandbits(64)
        self.active_remote = str(random.getrandbits(32))
        self.client_instance = self.dacp_id

    # ------------------------------------------------------------------ RTSP
    def _request(self, method: str, uri: str | None = None, headers: dict | None = None,
                 body: bytes = b"", content_type: str | None = None) -> RtspResponse:
        with self._rtsp_lock:
            for attempt in range(2):
                self.cseq += 1
                h = OrderedDict()
                h["CSeq"] = str(self.cseq)
                h["User-Agent"] = USER_AGENT
                h["Client-Instance"] = self.client_instance
                h["DACP-ID"] = self.dacp_id
                h["Active-Remote"] = self.active_remote
                if self.session_id:
                    h["Session"] = self.session_id
                if self._auth:
                    h["Authorization"] = self._digest(method, uri or self.url)
                if headers:
                    h.update(headers)
                if body:
                    h["Content-Type"] = content_type or "application/octet-stream"
                    h["Content-Length"] = str(len(body))
                lines = [f"{method} {uri or self.url} RTSP/1.0"] + [f"{k}: {v}" for k, v in h.items()]
                self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
                resp = self._read_response()
                if resp.code == 401 and attempt == 0 and self.password is not None:
                    m = re.search(r'realm="([^"]*)".*nonce="([^"]*)"', resp.header("www-authenticate", ""))
                    if m:
                        self._auth = (m.group(1), m.group(2))
                        continue
                return resp
        raise RaopError("authentication failed")

    def _digest(self, method: str, uri: str) -> str:
        realm, nonce = self._auth
        user = "iTunes"
        ha1 = hashlib.md5(f"{user}:{realm}:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
                f'uri="{uri}", response="{resp}"')

    def _read_response(self) -> RtspResponse:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RaopError("receiver closed the connection")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        lines = head.decode("utf-8", "replace").split("\r\n")
        parts = lines[0].split(" ", 2)
        code = int(parts[1])
        reason = parts[2] if len(parts) > 2 else ""
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0"))
        while len(rest) < length:
            chunk = self.sock.recv(length - len(rest))
            if not chunk:
                break
            rest += chunk
        return RtspResponse(code, reason, headers, rest[:length])

    def _check(self, resp: RtspResponse, what: str):
        if resp.code != 200:
            if resp.code == 401:
                raise RaopError("password required" if self.password is None else "wrong password")
            if resp.code == 453:
                raise RaopError("receiver is busy (in use by another sender)")
            if resp.code == 403:
                raise RaopError("access denied by receiver (on a Mac: set 'Allow AirPlay for' to Everyone)")
            raise RaopError(f"{what} failed: {resp.code} {resp.reason}")

    # -------------------------------------------------------------- connect
    def connect(self, seq: int, rtptime: int, volume: float):
        r = self.receiver
        self.sock = socket.create_connection((r.host, r.port), timeout=5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.local_ip = self.sock.getsockname()[0]
        sid = str(random.getrandbits(32))
        self.url = f"rtsp://{self.local_ip}/{sid}"

        resp = self._request("OPTIONS", "*", {"Apple-Challenge": "U2lsZW5jZUlzR29sZGVuLg"})
        if resp.code == 401 and self.password is None:
            raise RaopError("password required")

        sdp = (
            "v=0\r\n"
            f"o=iTunes {sid} 0 IN IP4 {self.local_ip}\r\n"
            "s=iTunes\r\n"
            f"c=IN IP4 {r.host}\r\n"
            "t=0 0\r\n"
            "m=audio 0 RTP/AVP 96\r\n"
            "a=rtpmap:96 AppleLossless\r\n"
            f"a=fmtp:96 {FRAMES_PER_PACKET} 0 16 40 10 14 2 255 0 0 {SAMPLE_RATE}\r\n"
        ).encode()
        self._check(self._request("ANNOUNCE", body=sdp, content_type="application/sdp"), "ANNOUNCE")

        self.control_sock = self._udp()
        self.timing_sock = self._udp()
        self.audio_sock = self._udp()
        cport = self.control_sock.getsockname()[1]
        tport = self.timing_sock.getsockname()[1]
        resp = self._request("SETUP", headers={
            "Transport": f"RTP/AVP/UDP;unicast;interleaved=0-1;mode=record;"
                         f"control_port={cport};timing_port={tport}"})
        self._check(resp, "SETUP")
        self.session_id = (resp.header("session") or "1").split(";")[0]
        transport = resp.header("transport", "")
        ports = dict(re.findall(r"(\w+)=(\d+)", transport))
        self.server_port = int(ports.get("server_port", 6000))
        self.control_port = int(ports.get("control_port", 6001))
        self.timing_port = int(ports.get("timing_port", 6002))

        self._spawn(self._timing_loop)
        self._spawn(self._control_loop)

        resp = self._request("RECORD", headers={
            "Range": "npt=0-",
            "RTP-Info": f"seq={seq};rtptime={rtptime}"})
        self._check(resp, "RECORD")
        self.connected = True
        self.set_volume(volume)
        self._spawn(self._keepalive_loop)
        log.info("connected to %s (%s:%s) audio=%s ctrl=%s timing=%s",
                 r.name, r.host, r.port, self.server_port, self.control_port, self.timing_port)

    def _udp(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", 0))
        s.settimeout(0.5)
        return s

    def _spawn(self, fn):
        t = threading.Thread(target=fn, daemon=True, name=f"raop-{fn.__name__}")
        t.start()
        self._threads.append(t)

    # -------------------------------------------------------- UDP services
    def _timing_loop(self):
        """Answer the receiver's NTP-style timing queries with our clock."""
        while not self._stop.is_set():
            try:
                data, addr = self.timing_sock.recvfrom(128)
            except socket.timeout:
                continue
            except OSError:
                break
            received = ntp_bytes(now_seconds())
            if len(data) < 32 or data[1] & 0x7F != 0x52:
                continue
            reply = b"\x80\xd3\x00\x07\x00\x00\x00\x00" + data[24:32] + received + ntp_bytes(now_seconds())
            try:
                self.timing_sock.sendto(reply, addr)
            except OSError:
                pass

    def _control_loop(self):
        """Serve retransmit requests (0x55) from our packet history."""
        while not self._stop.is_set():
            try:
                data, addr = self.control_sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) >= 8 and data[1] & 0x7F == 0x55:
                first, count = struct.unpack(">HH", data[4:8])
                for i in range(count):
                    pkt = self.history.get((first + i) & 0xFFFF)
                    if pkt:
                        try:
                            self.control_sock.sendto(b"\x80\xd6\x00\x01" + pkt, (self.receiver.host, self.control_port))
                        except OSError:
                            pass

    def _keepalive_loop(self):
        # Receivers drop idle sessions; a periodic OPTIONS keeps them alive and
        # tells us quickly when the receiver went away.
        while not self._stop.wait(15):
            try:
                self._request("OPTIONS", "*")
            except Exception as e:
                log.warning("%s: keepalive failed: %s", self.receiver.name, e)
                self._lost()
                return

    def _lost(self):
        was = self.connected
        self.close(teardown=False)
        if was and self.on_closed:
            self.on_closed(self)

    # ------------------------------------------------------------ streaming
    def send_audio(self, seq: int, rtptime: int, payload: bytes):
        if not self.connected:
            return
        marker = 0xE0 if self.first_packet else 0x60
        pkt = struct.pack(">BBHII", 0x80, marker, seq, rtptime, self.ssrc) + payload
        self.history[seq] = pkt
        if len(self.history) > self.HISTORY:
            self.history.popitem(last=False)
        try:
            self.audio_sock.sendto(pkt, (self.receiver.host, self.server_port))
        except OSError as e:
            log.debug("audio send failed: %s", e)
        if self.first_packet:
            self.first_packet = False

    def send_sync(self, rtptime: int, at: float, first: bool = False):
        """Tell the receiver that `rtptime - latency` is playing at wall time `at`."""
        if not self.connected:
            return
        header = b"\x90\xd4\x00\x07" if first else b"\x80\xd4\x00\x07"
        pkt = header + struct.pack(">I", (rtptime - self.latency) & 0xFFFFFFFF) + ntp_bytes(at) + \
            struct.pack(">I", rtptime & 0xFFFFFFFF)
        try:
            self.control_sock.sendto(pkt, (self.receiver.host, self.control_port))
        except OSError:
            pass

    def set_volume(self, volume: float):
        """volume 0..100 -> AirPlay dB scale (-30..0, -144 = mute)."""
        if not self.connected:
            return
        db = -144.0 if volume <= 0 else -30.0 + 30.0 * (min(volume, 100.0) / 100.0)
        body = f"volume: {db:.6f}\r\n".encode()
        try:
            self._request("SET_PARAMETER", body=body, content_type="text/parameters")
        except Exception as e:
            log.warning("%s: volume failed: %s", self.receiver.name, e)

    def set_metadata(self, title: str, artist: str = "", album: str = "", rtptime: int = 0):
        if not self.connected:
            return

        def item(tag: bytes, value: str) -> bytes:
            v = value.encode("utf-8")
            return tag + struct.pack(">I", len(v)) + v

        inner = item(b"minm", title) + item(b"asar", artist) + item(b"asal", album)
        body = b"mlit" + struct.pack(">I", len(inner)) + inner
        try:
            self._request("SET_PARAMETER", headers={"RTP-Info": f"rtptime={rtptime & 0xFFFFFFFF}"},
                          body=body, content_type="application/x-dmap-tagged")
        except Exception:
            pass

    def flush(self, seq: int, rtptime: int):
        if not self.connected:
            return
        try:
            self._request("FLUSH", headers={"RTP-Info": f"seq={seq};rtptime={rtptime & 0xFFFFFFFF}"})
        except Exception:
            pass
        self.first_packet = True

    def close(self, teardown: bool = True):
        if teardown and self.connected:
            try:
                self.sock.settimeout(2)
                self._request("TEARDOWN")
            except Exception:
                pass
        self.connected = False
        self._stop.set()
        for s in (self.sock, self.audio_sock, self.control_sock, self.timing_sock):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
