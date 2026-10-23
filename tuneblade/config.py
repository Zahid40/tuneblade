"""Persisted settings (%APPDATA%\\TuneBlade\\config.json)."""

import json
import os
from pathlib import Path

DIR = Path(os.environ.get("APPDATA", Path.home())) / "TuneBlade"
PATH = DIR / "config.json"

DEFAULTS = {
    "latency_ms": 2000,
    "master_volume": 100,
    "volumes": {},        # receiver id -> 0..100
    "auto_connect": [],   # receiver ids connected at last exit
    "passwords": {},      # receiver id -> password
    "manual": [],         # [{"host":..., "port":..., "name":...}]
    "start_minimized": False,
    "web_enabled": True,
    "web_port": 8000,
}


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        cfg.update(json.loads(PATH.read_text("utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict):
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), "utf-8")
        tmp.replace(PATH)
    except OSError:
        pass
