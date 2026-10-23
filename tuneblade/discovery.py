"""Find output devices on the LAN.

AirPlay and Google Cast advertise over mDNS; DLNA renderers answer SSDP.
"""

import socket
import threading
from dataclasses import dataclass, field

from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf

from .cast import CAST_TYPE
from .dlna import SsdpBrowser

RAOP_TYPE = "_raop._tcp.local."
AIRPLAY_TYPE = "_airplay._tcp.local."


@dataclass
class Receiver:
    id: str            # stable key: the mDNS service name / UPnP UDN
    name: str          # human readable
    host: str          # IPv4 address
    port: int
    txt: dict = field(default_factory=dict)
    kind: str = "airplay"  # "airplay" | "cast" | "dlna"

    KIND_LABELS = {"airplay": "AirPlay", "cast": "Chromecast", "dlna": "DLNA"}

    @property
    def kind_label(self) -> str:
        return self.KIND_LABELS.get(self.kind, self.kind)

    @property
    def model(self) -> str:
        return self.txt.get({"airplay": "am", "cast": "md", "dlna": "model"}[self.kind], "")

    @property
    def needs_password(self) -> bool:
        return self.txt.get("pw", "false").lower() == "true"

    @property
    def encryption_types(self) -> list[str]:
        return self.txt.get("et", "0").split(",")


def _decode_txt(props: dict) -> dict:
    out = {}
    for k, v in (props or {}).items():
        key = k.decode("utf-8", "replace") if isinstance(k, bytes) else str(k)
        val = v.decode("utf-8", "replace") if isinstance(v, bytes) else ("" if v is None else str(v))
        out[key] = val
    return out


def _pretty_name(service_name: str) -> str:
    # "8AA99FF8FCFA@Kitchen._raop._tcp.local." -> "Kitchen"
    base = service_name[: -len(RAOP_TYPE) - 1] if service_name.endswith("." + RAOP_TYPE) else service_name
    return base.split("@", 1)[1] if "@" in base else base


class Discovery:
    """Keeps a live dict of receivers; calls on_change() whenever it updates."""

    def __init__(self, on_change=None):
        self.on_change = on_change
        self.receivers: dict[str, Receiver] = {}
        self._lock = threading.Lock()
        self._zc = None
        self._browser = None
        self._ssdp = SsdpBrowser(self._dlna_found, self._dlna_lost)

    def start(self):
        self._zc = Zeroconf()
        # Browse _airplay too: some receivers (e.g. macOS) only answer the
        # _raop query reliably when the sender asks for both, like Apple's do.
        self._browser = ServiceBrowser(self._zc, [RAOP_TYPE, AIRPLAY_TYPE, CAST_TYPE],
                                       handlers=[self._handler])
        self._ssdp.start()

    def stop(self):
        self._ssdp.stop()
        if self._zc:
            self._zc.close()
            self._zc = None

    def snapshot(self) -> list[Receiver]:
        with self._lock:
            return sorted(self.receivers.values(), key=lambda r: r.name.lower())

    def _handler(self, zeroconf, service_type, name, state_change):
        if service_type not in (RAOP_TYPE, CAST_TYPE):
            return
        if state_change is ServiceStateChange.Removed:
            with self._lock:
                self.receivers.pop(name, None)
            self._notify()
            return
        # Resolve off the zeroconf thread; get_service_info blocks.
        threading.Thread(target=self._resolve, args=(zeroconf, service_type, name), daemon=True).start()

    def _resolve(self, zeroconf, service_type, name):
        # Sleepy or busy devices can take several seconds to answer.
        info = None
        for _ in range(4):
            info = zeroconf.get_service_info(service_type, name, timeout=5000)
            if info or self._zc is None:
                break
        if not info:
            return
        v4 = [a for a in info.parsed_addresses() if ":" not in a]
        if not v4:
            return
        txt = _decode_txt(info.properties)
        if service_type == CAST_TYPE:
            if txt.get("ca") and not int(txt["ca"]) & 4:
                return  # no audio capability (e.g. some displays)
            rec = Receiver(id=name, name=txt.get("fn") or name.split(".")[0], host=v4[0], port=info.port,
                           txt=txt, kind="cast")
        else:
            rec = Receiver(id=name, name=_pretty_name(name), host=v4[0], port=info.port, txt=txt)
        with self._lock:
            self.receivers[name] = rec
        self._notify()

    def _dlna_found(self, desc: dict):
        rec = Receiver(id=desc["udn"], name=desc["name"], host=desc["host"], port=desc["port"],
                       txt=desc, kind="dlna")
        with self._lock:
            self.receivers[rec.id] = rec
        self._notify()

    def _dlna_lost(self, desc: dict):
        with self._lock:
            self.receivers.pop(desc["udn"], None)
        self._notify()

    def _notify(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass


def add_manual(discovery: Discovery, host: str, port: int = 5000, name: str | None = None) -> Receiver:
    """Register a receiver that doesn't advertise over mDNS."""
    host = socket.gethostbyname(host)
    rec = Receiver(id=f"manual:{host}:{port}", name=name or host, host=host, port=port, txt={"et": "0"})
    with discovery._lock:
        discovery.receivers[rec.id] = rec
    discovery._notify()
    return rec
