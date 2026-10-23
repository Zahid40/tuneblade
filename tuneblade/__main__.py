"""python -m tuneblade            -> desktop app
python -m tuneblade --list     -> print receivers found on the network
python -m tuneblade --connect "Living Room" [--connect 192.168.1.20:5000 ...]
                               -> headless: stream system audio until Ctrl+C
"""

import argparse
import logging
import sys
import time


def _find(discovery, target):
    from .discovery import add_manual
    for r in discovery.snapshot():
        if target.lower() in (r.name.lower(), r.host, f"{r.host}:{r.port}", r.id.lower()):
            return r
    for r in discovery.snapshot():
        if target.lower() in r.name.lower():
            return r
    if target[0].isdigit():
        host, _, port = target.partition(":")
        return add_manual(discovery, host, int(port) if port else 5000)
    return None


def headless(args):
    from .discovery import Discovery
    from .streamer import Streamer

    disc = Discovery()
    disc.start()
    time.sleep(6)
    if args.list:
        for r in disc.snapshot():
            print(f"{r.name:30} {r.host}:{r.port:<6} {r.model:18} et={r.txt.get('et', '?')} "
                  f"{'password' if r.needs_password else ''}")
        disc.stop()
        return

    streamer = Streamer(latency_ms=args.latency)
    streamer.start()
    for target in args.connect:
        r = _find(disc, target)
        if not r:
            print(f"receiver not found: {target}")
            continue
        streamer.set_volume(r.id, args.volume)
        streamer.connect(r, args.password)
    try:
        last = None
        while True:
            time.sleep(1)
            state = ({streamer.sessions[k].receiver.name for k in streamer.sessions}, dict(streamer.errors))
            if state != last:
                print("streaming to:", ", ".join(sorted(state[0])) or "-", "| errors:", state[1] or "-")
                last = state
    except KeyboardInterrupt:
        pass
    finally:
        streamer.stop()
        disc.stop()
        time.sleep(0.5)


def _attach_console():
    """A windowed exe has no stdout; borrow the console it was launched from."""
    if sys.stdout is not None:
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = sys.stdout
            print()
    except (AttributeError, OSError):
        pass


def _log_handlers():
    from . import config
    if getattr(sys, "frozen", False) or sys.stderr is None:
        config.DIR.mkdir(parents=True, exist_ok=True)
        return [logging.FileHandler(config.DIR / "tuneblade.log", mode="w", encoding="utf-8")]
    return [logging.StreamHandler()]


def main():
    if len(sys.argv) > 1:
        _attach_console()
    p = argparse.ArgumentParser(prog="tuneblade", description="Stream Windows audio to AirPlay receivers")
    p.add_argument("--list", action="store_true", help="list receivers and exit")
    p.add_argument("--connect", action="append", default=[], metavar="NAME|IP[:PORT]")
    p.add_argument("--password")
    p.add_argument("--volume", type=float, default=70)
    p.add_argument("--latency", type=int, default=2000, help="buffer in ms (default 2000)")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    handlers = [logging.StreamHandler(sys.stdout)] if args.list or args.connect else _log_handlers()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("zeroconf").setLevel(logging.WARNING)
    if args.list or args.connect:
        headless(args)
    else:
        from .gui import main as gui_main
        gui_main()


if __name__ == "__main__":
    main()
