"""Capture whatever Windows is playing (WASAPI loopback of the default output)."""

import logging
import threading
import time
import warnings

import numpy as np
import soundcard as sc

log = logging.getLogger(__name__)

SAMPLE_RATE = 44100
CHUNK = 441  # 10 ms reads


class LoopbackCapture:
    """Background thread filling a FIFO with int16 stereo frames.

    The streamer drains it at its own pace; the FIFO absorbs jitter and the
    small clock drift between the sound card and our sender clock.
    """

    def __init__(self, max_buffer_frames: int = SAMPLE_RATE // 2):
        self.max_buffer = max_buffer_frames
        self._buf = np.zeros((0, 2), dtype=np.int16)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.device_name = ""
        self.level = 0.0  # peak of the last chunk, 0..1, for the UI meter

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="capture")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def available(self) -> int:
        with self._lock:
            return len(self._buf)

    def read(self, frames: int) -> np.ndarray | None:
        with self._lock:
            if len(self._buf) < frames:
                return None
            out, self._buf = self._buf[:frames], self._buf[frames:]
            return out

    def clear(self):
        with self._lock:
            self._buf = self._buf[:0]

    def _push(self, data: np.ndarray):
        with self._lock:
            self._buf = np.concatenate((self._buf, data))
            if len(self._buf) > self.max_buffer:
                self._buf = self._buf[-self.max_buffer // 4:]

    def _run(self):
        warnings.filterwarnings("ignore", category=sc.SoundcardRuntimeWarning)
        while not self._stop.is_set():
            try:
                speaker = sc.default_speaker()
                mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
                self.device_name = speaker.name
                log.info("capturing from %s", speaker.name)
                with mic.recorder(samplerate=SAMPLE_RATE, channels=2, blocksize=CHUNK) as rec:
                    last_check = time.monotonic()
                    while not self._stop.is_set():
                        data = rec.record(numframes=CHUNK)
                        if data.ndim == 1:
                            data = np.column_stack((data, data))
                        data = data[:, :2]
                        self.level = float(np.abs(data).max()) if len(data) else 0.0
                        self._push((np.clip(data, -1.0, 1.0) * 32767).astype(np.int16))
                        # Follow the user switching the default output device.
                        if time.monotonic() - last_check > 2:
                            last_check = time.monotonic()
                            if sc.default_speaker().name != speaker.name:
                                log.info("default output changed, reopening")
                                break
            except Exception as e:
                log.warning("capture error: %s; retrying", e)
                time.sleep(1)
