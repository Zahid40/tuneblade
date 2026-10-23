"""DLNA / UPnP MediaRenderer output (smart TVs, AV receivers, Kodi, Android apps).

Discovery is SSDP; playback is SOAP: SetAVTransportURI(our MP3 URL) + Play.
"""

import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)

SSDP_ADDR = ("239.255.255.250", 1900)
RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"
NS = {"d": "urn:schemas-upnp-org:device-1-0"}


class DlnaError(Exception):
    pass


# ----------------------------------------------------------------- discovery
class SsdpBrowser:
    """Periodically M-SEARCHes for MediaRenderers and reports them via on_found."""

    def __init__(self, on_found, on_lost=None, interval: float = 30):
        self.on_found = on_found
        self.on_lost = on_lost
        self.interval = interval
        self._stop = threading.Event()
        self._known: dict[str, float] = {}  # location -> last seen
        self._described: dict[str, dict] = {}

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="ssdp").start()

    def stop(self):
        self._stop.set()

    def _run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(1)
        msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
               f"MX: 2\r\nST: {RENDERER}\r\n\r\n").encode()
        next_search = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_search:
                for _ in range(2):
                    try:
                        sock.sendto(msg, SSDP_ADDR)
                    except OSError:
                        pass
                next_search = now + self.interval
                self._expire(now)
            try:
                data, _ = sock.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if not m:
                continue
            loc = m.group(1).decode("utf-8", "replace")
            first = loc not in self._known
            self._known[loc] = time.monotonic()
            if first:
                threading.Thread(target=self._describe, args=(loc,), daemon=True).start()

    def _expire(self, now):
        for loc, seen in list(self._known.items()):
            if now - seen > self.interval * 3:
                del self._known[loc]
                desc = self._described.pop(loc, None)
                if desc and self.on_lost:
                    self.on_lost(desc)

    def _describe(self, location):
        try:
            desc = describe(location)
        except Exception as e:
            log.debug("ssdp describe %s failed: %s", location, e)
            self._known.pop(location, None)
            return
        if desc:
            self._described[location] = desc
            self.on_found(desc)


def describe(location: str) -> dict | None:
    with urllib.request.urlopen(location, timeout=4) as resp:
        root = ET.fromstring(resp.read())
    base = root.findtext("d:URLBase", default=location, namespaces=NS) or location
    for dev in root.iter("{urn:schemas-upnp-org:device-1-0}device"):
        controls = {}
        for svc in dev.findall("d:serviceList/d:service", NS):
            stype = svc.findtext("d:serviceType", default="", namespaces=NS)
            url = svc.findtext("d:controlURL", default="", namespaces=NS)
            for want in (AVT, RC):
                if stype.split(":")[:-1] == want.split(":")[:-1]:
                    controls[want] = (stype, urljoin(base, url))
        if AVT in controls:
            return {
                "udn": dev.findtext("d:UDN", default=location, namespaces=NS),
                "name": dev.findtext("d:friendlyName", default="DLNA renderer", namespaces=NS),
                "model": dev.findtext("d:modelName", default="", namespaces=NS),
                "host": urlparse(location).hostname,
                "port": urlparse(location).port or 80,
                "avt_type": controls[AVT][0], "avt_url": controls[AVT][1],
                "rc_type": controls.get(RC, ("", ""))[0], "rc_url": controls.get(RC, ("", ""))[1],
            }
    return None


# -------------------------------------------------------------------- SOAP
def soap(url: str, service: str, action: str, args: dict, timeout: float = 6) -> str:
    body = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args.items())
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{service}">{body}</u:{action}>'
        "</s:Body></s:Envelope>").encode("utf-8")
    req = urllib.request.Request(url, data=envelope, method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"{service}#{action}"',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        code = re.search(r"<errorDescription>(.*?)</errorDescription>", detail)
        raise DlnaError(f"{action} failed: {code.group(1) if code else e.code}") from None


def didl(url: str) -> str:
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="tuneblade" parentID="0" restricted="1">'
        "<dc:title>TuneBlade</dc:title><dc:creator>PC audio</dc:creator>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        '<res protocolInfo="http-get:*:audio/mpeg:DLNA.ORG_PN=MP3;DLNA.ORG_OP=00;DLNA.ORG_CI=0;'
        f'DLNA.ORG_FLAGS=01700000000000000000000000000000">{escape(url)}</res>'
        "</item></DIDL-Lite>")


class DlnaSession:
    def __init__(self, receiver, stream_url: str, on_closed=None):
        self.receiver = receiver
        self.stream_url = stream_url
        self.on_closed = on_closed
        self.connected = False
        self._stop = threading.Event()

    @property
    def _t(self):
        return self.receiver.txt

    def connect(self, seq: int, rtptime: int, volume: float):
        t = self._t
        try:
            soap(t["avt_url"], t["avt_type"], "Stop", {"InstanceID": 0})
        except Exception:
            pass  # many renderers error on Stop when idle
        soap(t["avt_url"], t["avt_type"], "SetAVTransportURI",
             {"InstanceID": 0, "CurrentURI": self.stream_url, "CurrentURIMetaData": didl(self.stream_url)})
        soap(t["avt_url"], t["avt_type"], "Play", {"InstanceID": 0, "Speed": 1})
        self.connected = True
        self.set_volume(volume)
        threading.Thread(target=self._watch, daemon=True).start()
        log.info("DLNA playing on %s (%s)", self.receiver.name, self.stream_url)

    def _watch(self):
        """Notice if the renderer stops (user pressed stop on the TV, etc.)."""
        misses = 0
        while not self._stop.wait(5):
            try:
                xml = soap(self._t["avt_url"], self._t["avt_type"], "GetTransportInfo", {"InstanceID": 0}, timeout=4)
                state = re.search(r"<CurrentTransportState>(\w+)<", xml)
                misses = misses + 1 if state and state.group(1) in ("STOPPED", "NO_MEDIA_PRESENT") else 0
            except Exception:
                misses += 1
            if misses >= 3:
                was = self.connected
                self.close(teardown=False)
                if was and self.on_closed:
                    self.on_closed(self)
                return

    def send_audio(self, seq, rtptime, payload):
        pass

    def send_sync(self, rtptime, at, first=False):
        pass

    def set_metadata(self, *a, **kw):
        pass

    def set_volume(self, volume: float):
        t = self._t
        if not (self.connected and t.get("rc_url")):
            return
        try:
            soap(t["rc_url"], t["rc_type"], "SetVolume",
                 {"InstanceID": 0, "Channel": "Master", "DesiredVolume": int(round(max(0, min(volume, 100))))})
        except Exception as e:
            log.debug("%s: volume failed: %s", self.receiver.name, e)

    def close(self, teardown: bool = True):
        self._stop.set()
        if teardown and self.connected:
            try:
                soap(self._t["avt_url"], self._t["avt_type"], "Stop", {"InstanceID": 0}, timeout=3)
            except Exception:
                pass
        self.connected = False
