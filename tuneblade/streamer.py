"""Drive one shared audio stream into every output: AirPlay, Cast, DLNA, browsers."""

import logging
import random
import threading
import time

import numpy as np

from . import alac
from .alac import FRAMES_PER_PACKET
from .capture import LoopbackCapture
from .cast import CastSession
from .dlna import DlnaSession
from .raop import SAMPLE_RATE, RaopSession, now_seconds
from .webserver import WebServer

log = logging.getLogger(__name__)

PACKET_SECONDS = FRAMES_PER_PACKET / SAMPLE_RATE
SYNC_EVERY = SAMPLE_RATE // FRAMES_PER_PACKET  # ~1 sync packet per second
PRIME_FRAMES = 2048  # how much capture to buffer before we start draining it
SILENCE = np.zeros((FRAMES_PER_PACKET, 2), dtype=np.int16)


class Streamer:
    """Owns the capture, the sender clock, and the set of live sessions.

    Every receiver gets identical seq/rtptime values and sync packets derived
    from one clock, so they all play the same sample at the same moment.
    """

    def __init__(self, latency_ms: int = 2000, on_change=None, web_port: int | None = 8000):
        self.latency_frames = int(SAMPLE_RATE * latency_ms / 1000)
        self.on_change = on_change
        self.capture = LoopbackCapture()
        # Browsers, Chromecast and DLNA all pull audio from this server.
        self.web = WebServer(web_port, on_change=self._notify) if web_port else None
        self.sessions: dict[str, object] = {}
        self.errors: dict[str, str] = {}
        self.connecting: set[str] = set()
        self.volumes: dict[str, float] = {}
        self.master_volume = 100.0
        self._lock = threading.Lock()
        self._seq = random.getrandbits(16)
        self._rtptime = random.getrandbits(32)
        self._thread = None
        self._stop = threading.Event()
        self._primed = False

    # ------------------------------------------------------------ control
    def start(self):
        self.capture.start()
        if self.web:
            self.web.start()
        self._stop.clear()
        self._anchor_wall = now_seconds()
        self._anchor_ts = self._rtptime
        self._thread = threading.Thread(target=self._run, daemon=True, name="streamer")
        self._thread.start()

    def stop(self):
        self._stop.set()
        for rid in list(self.sessions):
            self.disconnect(rid)
        self.capture.stop()
        if self.web:
            self.web.stop()

    def is_connected(self, rid: str) -> bool:
        s = self.sessions.get(rid)
        return bool(s and s.connected)

    def connect(self, receiver, password: str | None = None):
        """Connect in the background; on_change fires when done."""
        if receiver.id in self.sessions or receiver.id in self.connecting:
            return
        self.connecting.add(receiver.id)
        self.errors.pop(receiver.id, None)
        self._notify()
        threading.Thread(target=self._connect, args=(receiver, password), daemon=True).start()

    def _connect(self, receiver, password):
        try:
            session = self._make_session(receiver, password)
        except RuntimeError as e:
            self.errors[receiver.id] = str(e)
            self.connecting.discard(receiver.id)
            self._notify()
            return
        try:
            with self._lock:
                seq, ts = (self._seq + 1) & 0xFFFF, self._rtptime
            session.connect(seq, ts, self._effective_volume(receiver.id))
            session.send_sync(ts, self._play_time(ts), first=True)
            with self._lock:
                self.sessions[receiver.id] = session
            session.set_metadata("TuneBlade", "Windows audio", "", ts)
        except Exception as e:  # each protocol library raises its own errors
            log.warning("connect %s failed: %s", receiver.name, e)
            session.close(teardown=False)
            self.errors[receiver.id] = str(e) or e.__class__.__name__
        finally:
            self.connecting.discard(receiver.id)
            self._notify()

    def _make_session(self, receiver, password):
        if receiver.kind == "airplay":
            return RaopSession(receiver, password=password, latency_frames=self.latency_frames,
                               on_closed=self._on_session_lost)
        if not (self.web and self.web.port and self.web.error is None):
            raise RuntimeError("web streaming is off; it's needed for Chromecast/DLNA")
        url = self.web.url("/stream.mp3", remote=receiver.host)
        cls = CastSession if receiver.kind == "cast" else DlnaSession
        return cls(receiver, url, on_closed=self._on_session_lost)

    def disconnect(self, rid: str):
        with self._lock:
            session = self.sessions.pop(rid, None)
        if session:
            threading.Thread(target=session.close, daemon=True).start()
        self._notify()

    def _on_session_lost(self, session):
        with self._lock:
            if self.sessions.get(session.receiver.id) is session:
                del self.sessions[session.receiver.id]
        self.errors[session.receiver.id] = "connection lost"
        self._notify()

    def set_volume(self, rid: str, volume: float):
        self.volumes[rid] = volume
        s = self.sessions.get(rid)
        if s:
            threading.Thread(target=s.set_volume, args=(self._effective_volume(rid),), daemon=True).start()

    def set_master_volume(self, volume: float):
        self.master_volume = volume
        for rid, s in list(self.sessions.items()):
            threading.Thread(target=s.set_volume, args=(self._effective_volume(rid),), daemon=True).start()

    def _effective_volume(self, rid: str) -> float:
        return self.volumes.get(rid, 70.0) * self.master_volume / 100.0

    def _notify(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    # -------------------------------------------------------------- clock
    def _play_time(self, rtptime: int) -> float:
        """Wall time at which `rtptime - latency` is due, per our stream clock."""
        return self._anchor_wall + ((rtptime - self._anchor_ts) & 0xFFFFFFFF) / SAMPLE_RATE

    def _run(self):
        start = time.monotonic()
        n = 0
        while not self._stop.is_set():
            pcm = self._next_pcm()
            payload = alac.encode(pcm)
            with self._lock:
                self._seq = (self._seq + 1) & 0xFFFF
                seq, ts = self._seq, self._rtptime
                self._rtptime = (self._rtptime + FRAMES_PER_PACKET) & 0xFFFFFFFF
                sessions = list(self.sessions.values())
            if n % SYNC_EVERY == 0:
                at = self._play_time(ts)
                for s in sessions:
                    s.send_sync(ts, at)
            for s in sessions:
                s.send_audio(seq, ts, payload)
            if self.web:
                self.web.feed(pcm.tobytes())
            n += 1
            # Pace against the ideal schedule, not the previous wakeup, so
            # sleep jitter never accumulates into drift.
            delay = start + n * PACKET_SECONDS - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:
                # We fell badly behind (system stall): shift the whole stream
                # clock forward so play times stay ahead of what we send.
                start -= delay
                self._anchor_wall -= delay

    def _next_pcm(self) -> np.ndarray:
        cap = self.capture
        if not self._primed:
            if cap.available() < PRIME_FRAMES:
                return SILENCE
            self._primed = True
        pcm = cap.read(FRAMES_PER_PACKET)
        if pcm is None:
            self._primed = False  # underrun: rebuild a cushion before resuming
            return SILENCE
        # Keep latency bounded if the sound card clock runs faster than ours.
        if cap.available() > PRIME_FRAMES * 4:
            cap.read(cap.available() - PRIME_FRAMES)
        return pcm
