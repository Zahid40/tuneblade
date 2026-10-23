"""Web streaming endpoints, a fake DLNA renderer, and Cast error handling."""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_stream import SineCapture  # noqa: E402
from tuneblade import dlna  # noqa: E402
from tuneblade.discovery import Receiver  # noqa: E402
from tuneblade.streamer import Streamer  # noqa: E402


def make_streamer(port=18000):
    s = Streamer(latency_ms=2000, web_port=port)
    s.capture = SineCapture()
    s.start()
    time.sleep(0.3)
    return s


def read_stream(url, seconds):
    out = b""
    with urllib.request.urlopen(url, timeout=5) as resp:
        headers = resp.headers
        end = time.time() + seconds
        while time.time() < end:
            out += resp.read1(65536)
    return headers, out


def test_web_endpoints():
    s = make_streamer()
    try:
        base = f"http://127.0.0.1:{s.web.port}"
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "<title>TuneBlade</title>" in page

        h, pcm = read_stream(base + "/pcm", 2.0)
        assert h["Content-Type"] == "application/octet-stream"
        frames = np.frombuffer(pcm[: len(pcm) - len(pcm) % 4], "<i2").reshape(-1, 2)
        assert len(frames) > 44100 * 1.5, len(frames)
        ref = SineCapture().read(200000)
        idx = next(i for i in range(200000 - 64) if np.array_equal(ref[i:i + 64], frames[1000:1064]))
        assert np.array_equal(ref[idx:idx + 20000], frames[1000:21000]), "PCM not bit-exact"

        h, wav = read_stream(base + "/stream.wav", 1.0)
        assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE" and h["Content-Type"] == "audio/wav"

        h, mp3 = read_stream(base + "/stream.mp3", 2.0)
        assert h["Content-Type"] == "audio/mpeg"
        assert h["contentFeatures.dlna.org"].startswith("DLNA.ORG_PN=MP3")
        assert len(mp3) > 320000 / 8 * 1.2, len(mp3)  # ~320 kbps
        if shutil.which("ffmpeg"):
            # decode the MP3 and check the dominant frequency is our 440 Hz left channel
            dec = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "2", "-ar", "44100", "pipe:1"],
                                 input=mp3, capture_output=True, check=True).stdout
            left = np.frombuffer(dec, "<i2").reshape(-1, 2)[:, 0].astype(float)
            spec = np.abs(np.fft.rfft(left[5000:5000 + 44100]))
            assert abs(np.argmax(spec) - 440) <= 2, np.argmax(spec)

        req = urllib.request.Request(base + "/stream.mp3", method="HEAD")
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.headers["transferMode.dlna.org"] == "Streaming"
        time.sleep(0.3)
        assert s.web.listeners() == []
    finally:
        s.stop()


# ------------------------------------------------------------- fake DLNA TV
class FakeRenderer:
    """Serves a device description, accepts SOAP, and pulls the stream URL."""

    def __init__(self):
        self.actions = []
        self.volume = None
        self.uri = None
        self.bytes_pulled = 0
        self.state = "NO_MEDIA_PRESENT"
        rnd = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
<friendlyName>Fake TV</friendlyName><modelName>FakeTV 9000</modelName><UDN>uuid:fake-tv</UDN>
<serviceList>
<service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><controlURL>/avt</controlURL></service>
<service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType><controlURL>/rc</controlURL></service>
</serviceList></device></root>""".encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                data = self.rfile.read(int(self.headers["Content-Length"])).decode()
                action = self.headers["SOAPACTION"].strip('"').split("#")[1]
                rnd.actions.append(action)
                if action == "SetAVTransportURI":
                    rnd.uri = re.search(r"<CurrentURI>(.*?)</CurrentURI>", data).group(1)
                    assert "DIDL-Lite" in data
                elif action == "Play":
                    rnd.state = "PLAYING"
                    threading.Thread(target=rnd._pull, daemon=True).start()
                elif action == "Stop":
                    rnd.state = "STOPPED"
                elif action == "SetVolume":
                    rnd.volume = int(re.search(r"<DesiredVolume>(\d+)<", data).group(1))
                extra = f"<CurrentTransportState>{rnd.state}</CurrentTransportState>" if action == "GetTransportInfo" else ""
                body = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                        f"<s:Body><u:{action}Response>{extra}</u:{action}Response></s:Body></s:Envelope>").encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _pull(self):
        with urllib.request.urlopen(self.uri, timeout=5) as resp:
            while self.state == "PLAYING":
                chunk = resp.read1(8192)
                if not chunk:
                    break
                self.bytes_pulled += len(chunk)

    def close(self):
        self.httpd.shutdown()


def test_dlna_renderer():
    tv = FakeRenderer()
    s = make_streamer(18100)
    try:
        desc = dlna.describe(f"http://127.0.0.1:{tv.port}/desc.xml")
        assert desc["name"] == "Fake TV" and desc["avt_url"].endswith("/avt")
        r = Receiver(id=desc["udn"], name=desc["name"], host="127.0.0.1", port=tv.port, txt=desc, kind="dlna")
        assert r.model == "FakeTV 9000"
        s.set_volume(r.id, 40)
        s.connect(r)
        deadline = time.time() + 5
        while time.time() < deadline and not s.is_connected(r.id):
            time.sleep(0.05)
        assert s.is_connected(r.id), s.errors
        assert tv.uri.endswith("/stream.mp3"), tv.uri
        assert tv.volume == 40
        time.sleep(2)
        assert tv.bytes_pulled > 40000, tv.bytes_pulled
        assert any(c.kind == "mp3" for c in s.web.listeners())
        s.set_volume(r.id, 10)
        time.sleep(0.5)
        assert tv.volume == 10
        s.disconnect(r.id)
        time.sleep(0.5)
        assert tv.actions[-1] == "Stop" and tv.state == "STOPPED"
    finally:
        s.stop()
        tv.close()


def test_cast_unreachable_reports_error():
    s = make_streamer(18200)
    try:
        r = Receiver(id="cast1", name="Nowhere", host="127.0.0.1", port=1, kind="cast",
                     txt={"fn": "Nowhere", "md": "Chromecast", "id": "0123456789abcdef0123456789abcdef"})
        s.connect(r)
        deadline = time.time() + 30
        while time.time() < deadline and r.id not in s.errors:
            time.sleep(0.1)
        assert r.id in s.errors and not s.is_connected(r.id), s.errors
    finally:
        s.stop()


if __name__ == "__main__":
    test_web_endpoints()
    print("web ok")
    test_dlna_renderer()
    print("dlna ok")
    test_cast_unreachable_reports_error()
    print("cast error path ok")
    print("all tests passed")
