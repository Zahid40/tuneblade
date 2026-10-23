"""A tiny AirPlay 1 receiver used to test the sender end to end.

Speaks just enough RTSP, decodes uncompressed ALAC frames back to PCM, sends
timing requests and records sync/volume so tests can assert on them.
Run standalone (`python tests/fake_receiver.py`) to advertise "Test Speaker"
over mDNS and dump received audio to received.wav.
"""

import re
import socket
import struct
import threading
import time
import wave

import numpy as np


def decode_alac_uncompressed(payload: bytes, frames: int = 352) -> np.ndarray:
    value = int.from_bytes(payload, "big")
    total = len(payload) * 8
    header = value >> (total - 23)
    assert header == (1 << 20) | 1, f"bad ALAC header {header:#x}"
    sample_bits = frames * 32
    samples = (value >> (total - 23 - sample_bits)) & ((1 << sample_bits) - 1)
    end_tag = (value >> (total - 23 - sample_bits - 3)) & 7
    assert end_tag == 7, "missing ALAC end tag"
    raw = samples.to_bytes(sample_bits // 8, "big")
    return np.frombuffer(raw, dtype=">i2").reshape(-1, 2).astype(np.int16)


class FakeReceiver:
    def __init__(self, port: int = 0):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("0.0.0.0", port))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.audio.bind(("0.0.0.0", 0))
        self.control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.control.bind(("0.0.0.0", 0))
        self.timing = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.timing.bind(("0.0.0.0", 0))
        for s in (self.audio, self.control, self.timing):
            s.settimeout(0.3)
        self.methods: list[str] = []
        self.volumes: list[float] = []
        self.packets: dict[int, tuple[int, np.ndarray]] = {}  # seq -> (rtptime, pcm)
        self.syncs: list[tuple[int, float, int]] = []
        self.timing_replies = 0
        self.record_info = None
        self.sender_timing_port = None
        self.sender_control_port = None
        self.sender_ip = None
        self.stop = threading.Event()
        self.drop_seq: set[int] = set()  # simulate loss: drop these seqs once
        self.resent: set[int] = set()
        for fn in (self._accept, self._audio_loop, self._control_loop, self._timing_loop):
            threading.Thread(target=fn, daemon=True).start()

    def close(self):
        self.stop.set()
        for s in (self.srv, self.audio, self.control, self.timing):
            s.close()

    # -------------------------------------------------------------- RTSP
    def _accept(self):
        while not self.stop.is_set():
            try:
                conn, addr = self.srv.accept()
            except OSError:
                return
            self.sender_ip = addr[0]
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        buf = b""
        while not self.stop.is_set():
            while b"\r\n\r\n" not in buf:
                d = conn.recv(4096)
                if not d:
                    return
                buf += d
            head, buf = buf.split(b"\r\n\r\n", 1)
            lines = head.decode().split("\r\n")
            method = lines[0].split(" ")[0]
            hdr = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
            n = int(hdr.get("content-length", 0))
            while len(buf) < n:
                buf += conn.recv(4096)
            body, buf = buf[:n], buf[n:]
            self.methods.append(method)
            extra = ""
            if method == "SETUP":
                t = hdr["transport"]
                self.sender_control_port = int(re.search(r"control_port=(\d+)", t).group(1))
                self.sender_timing_port = int(re.search(r"timing_port=(\d+)", t).group(1))
                extra = (f"Session: DEADBEEF\r\nTransport: RTP/AVP/UDP;unicast;mode=record;"
                         f"server_port={self.audio.getsockname()[1]};"
                         f"control_port={self.control.getsockname()[1]};"
                         f"timing_port={self.timing.getsockname()[1]}\r\n")
                threading.Thread(target=self._send_timing_requests, daemon=True).start()
            elif method == "RECORD":
                self.record_info = hdr.get("rtp-info")
                extra = "Audio-Latency: 11025\r\n"
            elif method == "SET_PARAMETER" and body.startswith(b"volume:"):
                self.volumes.append(float(body.split(b":")[1]))
            conn.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {hdr.get('cseq')}\r\nServer: AirTunes/105.1\r\n{extra}\r\n".encode())
            if method == "TEARDOWN":
                conn.close()
                return

    # --------------------------------------------------------------- UDP
    def _audio_loop(self):
        while not self.stop.is_set():
            try:
                d, _ = self.audio.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            seq, ts = struct.unpack(">HI", d[2:8])
            if seq in self.drop_seq:
                self.drop_seq.discard(seq)
                # ask for it again like a real receiver would
                self.control.sendto(b"\x80\xd5\x00\x01" + struct.pack(">HH", seq, 1),
                                    (self.sender_ip, self.sender_control_port))
                continue
            self.packets[seq] = (ts, decode_alac_uncompressed(d[12:]))

    def _control_loop(self):
        while not self.stop.is_set():
            try:
                d, _ = self.control.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            kind = d[1] & 0x7F
            if kind == 0x54:
                rtp_minus_lat, sec, frac, rtp_now = struct.unpack(">IIII", d[4:20])
                self.syncs.append((rtp_minus_lat, sec + frac / 2**32, rtp_now))
            elif kind == 0x56:
                inner = d[4:]
                seq, ts = struct.unpack(">HI", inner[2:8])
                self.resent.add(seq)
                self.packets[seq] = (ts, decode_alac_uncompressed(inner[12:]))

    def _send_timing_requests(self):
        while not self.stop.is_set() and self.sender_timing_port:
            req = b"\x80\xd2\x00\x07" + b"\x00" * 20 + struct.pack(">II", 1234, 5678)
            try:
                self.timing.sendto(req, (self.sender_ip, self.sender_timing_port))
            except OSError:
                return
            time.sleep(1)

    def _timing_loop(self):
        while not self.stop.is_set():
            try:
                d, _ = self.timing.recvfrom(128)
            except (socket.timeout, OSError):
                continue
            if d[1] & 0x7F == 0x53 and d[8:16] == struct.pack(">II", 1234, 5678):
                self.timing_replies += 1

    def pcm(self) -> np.ndarray:
        seqs = sorted(self.packets, key=lambda s: self.packets[s][0])
        return np.concatenate([self.packets[s][1] for s in seqs]) if seqs else np.zeros((0, 2), np.int16)


if __name__ == "__main__":
    from zeroconf import ServiceInfo, Zeroconf

    rx = FakeReceiver(5000)
    ip = socket.gethostbyname(socket.gethostname())
    zc = Zeroconf()
    zc.register_service(ServiceInfo(
        "_raop._tcp.local.", "0011223344AA@Test Speaker._raop._tcp.local.",
        addresses=[socket.inet_aton(ip)], port=rx.port,
        properties={"txtvers": "1", "ch": "2", "cn": "0,1", "et": "0", "sr": "44100", "ss": "16",
                    "tp": "UDP", "am": "FakeReceiver", "pw": "false"}))
    print(f"Test Speaker listening on {ip}:{rx.port} — Ctrl+C to stop and write received.wav")
    try:
        while True:
            time.sleep(1)
            print(f"\rpackets={len(rx.packets)} syncs={len(rx.syncs)} volume={rx.volumes[-1:]}", end="")
    except KeyboardInterrupt:
        pass
    zc.close()
    with wave.open("received.wav", "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(44100)
        w.writeframes(rx.pcm().tobytes())
    rx.close()
