"""Google Cast (Chromecast, Android TV, Nest/Google speakers) output.

The device's Default Media Receiver pulls our live MP3 from the web server;
we only handle start/stop and volume over the Cast control channel.
"""

import logging
import threading
import uuid

log = logging.getLogger(__name__)

CAST_TYPE = "_googlecast._tcp.local."


class CastError(Exception):
    pass


class CastSession:
    def __init__(self, receiver, stream_url: str, on_closed=None):
        self.receiver = receiver
        self.stream_url = stream_url
        self.on_closed = on_closed
        self.connected = False
        self._cast = None

    def connect(self, seq: int, rtptime: int, volume: float):
        import pychromecast
        from pychromecast.models import CastInfo, HostServiceInfo

        r = self.receiver
        try:
            uid = uuid.UUID(r.txt.get("id", ""))
        except ValueError:
            uid = uuid.uuid4()
        info = CastInfo({HostServiceInfo(r.host, r.port)}, uid, r.model or None, r.name,
                        r.host, r.port, None, None)
        cast = pychromecast.get_chromecast_from_cast_info(info, None, tries=1, timeout=8)
        try:
            cast.wait(timeout=10)
        except Exception:
            pass
        if not cast.socket_client.is_connected:
            cast.disconnect(timeout=2)
            raise CastError("could not reach the Cast device")
        self._cast = cast
        cast.set_volume(max(0.0, min(volume, 100.0)) / 100.0)
        mc = cast.media_controller
        mc.play_media(self.stream_url, "audio/mpeg", title="TuneBlade",
                      metadata={"metadataType": 3, "title": "TuneBlade", "artist": "PC audio"},
                      stream_type="LIVE")
        if not mc.block_until_active(timeout=15):
            raise CastError("Cast device did not start playback")
        self.connected = True
        cast.media_controller.register_status_listener(self)
        log.info("casting to %s (%s)", r.name, self.stream_url)

    # pychromecast MediaStatusListener hooks
    def new_media_status(self, status):
        if self.connected and status.player_state == "IDLE" and status.idle_reason in ("ERROR", "INTERRUPTED"):
            log.warning("%s stopped: %s", self.receiver.name, status.idle_reason)
            threading.Thread(target=self._lost, daemon=True).start()

    def load_media_failed(self, item, error_code):
        threading.Thread(target=self._lost, daemon=True).start()

    def _lost(self):
        was = self.connected
        self.close(teardown=False)
        if was and self.on_closed:
            self.on_closed(self)

    # Push-style hooks are no-ops: the device pulls the stream itself.
    def send_audio(self, seq, rtptime, payload):
        pass

    def send_sync(self, rtptime, at, first=False):
        pass

    def set_metadata(self, *a, **kw):
        pass

    def set_volume(self, volume: float):
        if self._cast and self.connected:
            try:
                self._cast.set_volume(max(0.0, min(volume, 100.0)) / 100.0)
            except Exception as e:
                log.warning("%s: volume failed: %s", self.receiver.name, e)

    def close(self, teardown: bool = True):
        cast, self._cast = self._cast, None
        self.connected = False
        if not cast:
            return
        try:
            if teardown:
                cast.quit_app()
        except Exception:
            pass
        try:
            cast.disconnect(timeout=2)
        except Exception:
            pass
