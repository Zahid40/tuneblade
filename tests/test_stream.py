"""End-to-end: stream a sine wave through the real sender into FakeReceiver."""

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from fake_receiver import FakeReceiver, decode_alac_uncompressed  # noqa: E402
from tuneblade import alac  # noqa: E402
from tuneblade.discovery import Receiver  # noqa: E402
from tuneblade.streamer import Streamer  # noqa: E402


class SineCapture:
    """Stands in for LoopbackCapture with a known, continuous signal."""

    def __init__(self):
        self.pos = 0
        self.device_name = "sine"
        self.level = 0.5

    def start(self):
        pass

    def stop(self):
        pass

    def available(self):
        return 4096

    def read(self, frames):
        t = np.arange(self.pos, self.pos + frames)
        self.pos += frames
        left = (np.sin(2 * np.pi * 440 * t / 44100) * 12000).astype(np.int16)
        right = (np.sin(2 * np.pi * 660 * t / 44100) * 12000).astype(np.int16)
        return np.column_stack((left, right))


def expected(n):
    c = SineCapture()
    return c.read(n)


def test_alac_roundtrip():
    pcm = (np.random.default_rng(1).integers(-32768, 32767, (352, 2))).astype(np.int16)
    frame = alac.encode(pcm)
    assert len(frame) == 1412
    assert np.array_equal(decode_alac_uncompressed(frame), pcm)


def run_stream(n_receivers=2, seconds=3.0):
    rxs = [FakeReceiver() for _ in range(n_receivers)]
    s = Streamer(latency_ms=2000)
    s.capture = SineCapture()
    s.start()
    recs = [Receiver(id=f"rx{i}", name=f"rx{i}", host="127.0.0.1", port=rx.port) for i, rx in enumerate(rxs)]
    rxs[0].drop_seq = {(s._seq + 40) & 0xFFFF}  # force one retransmit
    for r in recs:
        s.set_volume(r.id, 50)
        s.connect(r)
    deadline = time.time() + 5
    while time.time() < deadline and not all(s.is_connected(r.id) for r in recs):
        time.sleep(0.05)
    assert all(s.is_connected(r.id) for r in recs), s.errors
    time.sleep(seconds)
    s.set_master_volume(0)
    time.sleep(0.3)
    s.stop()
    time.sleep(0.5)
    return s, rxs


def test_end_to_end_multiroom():
    s, rxs = run_stream()
    try:
        for rx in rxs:
            assert rx.methods[:4] == ["OPTIONS", "ANNOUNCE", "SETUP", "RECORD"], rx.methods
            assert "TEARDOWN" in rx.methods
            assert rx.volumes[0] == -15.0 and rx.volumes[-1] == -144.0, rx.volumes
            assert len(rx.packets) > 3 * 44100 / 352 * 0.8, len(rx.packets)
            assert rx.timing_replies >= 2
            assert len(rx.syncs) >= 2
            # Sync packets: rtp_now - latency, consistent clock (1 s of audio per 1 s of NTP).
            (a_rtp, a_t, a_now), (b_rtp, b_t, b_now) = rx.syncs[0], rx.syncs[-1]
            assert (a_now - a_rtp) & 0xFFFFFFFF == 88200
            assert abs(((b_now - a_now) & 0xFFFFFFFF) / 44100 - (b_t - a_t)) < 1e-3

            # Audio arrives gap-free and bit-exact relative to the source.
            seqs = sorted(rx.packets, key=lambda q: rx.packets[q][0])
            ts = [rx.packets[q][0] for q in seqs]
            assert all((b - a) & 0xFFFFFFFF == 352 for a, b in zip(ts, ts[1:]))
            pcm = rx.pcm()
            ref = expected(SineCapture().pos + 2_000_000)
            nz = np.flatnonzero(np.any(pcm != 0, axis=1))
            start = nz[0]
            chunk = pcm[start:start + 44100]
            # find offset of this chunk in the reference signal
            idx = next(i for i in range(0, 2_000_000 - 44100)
                       if np.array_equal(ref[i:i + 64], chunk[:64]))
            assert np.array_equal(ref[idx:idx + len(chunk)], chunk)
        # Retransmit path exercised.
        assert rxs[0].resent, "expected a resend"
        # Multi-room: both receivers got identical rtptime -> sample mapping.
        common = set(rxs[0].packets) & set(rxs[1].packets)
        assert len(common) > 200
        for q in list(common)[:50]:
            assert rxs[0].packets[q][0] == rxs[1].packets[q][0]
            assert np.array_equal(rxs[0].packets[q][1], rxs[1].packets[q][1])
    finally:
        for rx in rxs:
            rx.close()


if __name__ == "__main__":
    test_alac_roundtrip()
    test_end_to_end_multiroom()
    print("all tests passed")
