"""Tk desktop UI with a system tray icon."""

import logging
import os
import sys
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from PIL import Image, ImageDraw, ImageTk

from . import config
from .discovery import Discovery, add_manual
from .streamer import Streamer

log = logging.getLogger(__name__)

BG = "#1e1f24"
PANEL = "#2a2c33"
FG = "#e8e8ea"
DIM = "#9a9ca5"
ACCENT = "#2f9cf4"
OK = "#3ecf6e"
ERR = "#ff6b6b"


def make_icon(size: int = 64, color=ACCENT) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, size - 2, size - 2), fill=color)
    bars = [0.35, 0.6, 0.85, 0.6, 0.35]
    w = size / 11
    for i, h in enumerate(bars):
        x = size * 0.22 + i * w * 1.5
        hh = size * 0.5 * h
        d.rounded_rectangle((x, size / 2 - hh / 2, x + w, size / 2 + hh / 2), radius=w / 2, fill="white")
    return img


class ReceiverRow:
    def __init__(self, app, parent, receiver):
        self.app = app
        self.receiver = receiver
        self.frame = tk.Frame(parent, bg=PANEL, padx=12, pady=8)
        self.frame.columnconfigure(1, weight=1)

        self.dot = tk.Canvas(self.frame, width=12, height=12, bg=PANEL, highlightthickness=0)
        self.dot.grid(row=0, column=0, rowspan=2, padx=(0, 10))
        self.dot_id = self.dot.create_oval(2, 2, 11, 11, fill=DIM, outline="")

        self.name = tk.Label(self.frame, text=receiver.name, bg=PANEL, fg=FG,
                             font=("Segoe UI Semibold", 11), anchor="w")
        self.name.grid(row=0, column=1, sticky="w")
        self.status = tk.Label(self.frame, text="", bg=PANEL, fg=DIM, font=("Segoe UI", 9), anchor="w")
        self.status.grid(row=1, column=1, sticky="w")

        vol = app.cfg["volumes"].get(receiver.id, 70)
        self.vol_var = tk.DoubleVar(value=vol)
        self.vol = ttk.Scale(self.frame, from_=0, to=100, variable=self.vol_var, length=140,
                             command=self._on_volume, style="Dark.Horizontal.TScale")
        self.vol.grid(row=0, column=2, rowspan=2, padx=12)

        self.button = tk.Button(self.frame, text="Connect", width=11, relief="flat", bd=0,
                                font=("Segoe UI", 9, "bold"), cursor="hand2", command=self._toggle)
        self.button.grid(row=0, column=3, rowspan=2)
        self._vol_job = None

    def _toggle(self):
        self.app.toggle(self.receiver)

    def _on_volume(self, _=None):
        # Debounce: the slider fires continuously while dragging.
        if self._vol_job:
            self.frame.after_cancel(self._vol_job)
        self._vol_job = self.frame.after(120, self._apply_volume)

    def _apply_volume(self):
        v = round(self.vol_var.get())
        self.app.cfg["volumes"][self.receiver.id] = v
        self.app.streamer.set_volume(self.receiver.id, v)
        self.app.save_soon()

    def refresh(self):
        s = self.app.streamer
        rid = self.receiver.id
        r = self.receiver
        detail = " · ".join(x for x in (r.kind_label, r.model, r.host) if x)
        if s.is_connected(rid):
            color, text, btn, bbg = OK, f"Streaming · {detail}", "Disconnect", "#3a3d46"
        elif rid in s.connecting:
            color, text, btn, bbg = ACCENT, "Connecting…", "Cancel", "#3a3d46"
        elif rid in s.errors:
            color, text, btn, bbg = ERR, s.errors[rid], "Connect", ACCENT
        else:
            color, text, btn, bbg = DIM, detail + ("  🔒" if r.needs_password else ""), "Connect", ACCENT
        self.dot.itemconfig(self.dot_id, fill=color)
        self.status.config(text=text, fg=ERR if color == ERR else DIM)
        self.button.config(text=btn, bg=bbg, fg="white", activebackground=bbg, activeforeground="white")


class App:
    def __init__(self):
        self.cfg = config.load()
        self._dirty = False
        self._save_job = None

        self.root = tk.Tk()
        self.root.title("TuneBlade")
        self.root.configure(bg=BG)
        scale = self.root.winfo_fpixels("1i") / 96  # Windows display scaling
        self.root.geometry(f"{int(620 * scale)}x{int(480 * scale)}")
        self.root.minsize(int(520 * scale), int(300 * scale))
        self._icon_img = ImageTk.PhotoImage(make_icon(64))
        self.root.iconphoto(True, self._icon_img)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        # In the clam theme "background" paints the slider thumb.
        for name, bg in (("Dark.Horizontal.TScale", PANEL), ("Master.Horizontal.TScale", BG)):
            style.configure(name, background=ACCENT, troughcolor="#41444d", bordercolor=bg,
                            lightcolor=ACCENT, darkcolor=ACCENT, sliderthickness=14, troughrelief="flat")
            style.map(name, background=[("active", "#5ab4ff")])
        style.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG, bordercolor=BG,
                        arrowcolor=DIM)

        self.streamer = Streamer(latency_ms=self.cfg["latency_ms"], on_change=self._mark_dirty,
                                 web_port=self.cfg["web_port"] if self.cfg["web_enabled"] else None)
        self.streamer.volumes.update(self.cfg["volumes"])
        self.streamer.master_volume = self.cfg["master_volume"]
        self.discovery = Discovery(on_change=self._mark_dirty)
        self.rows: dict[str, ReceiverRow] = {}
        self._auto_pending = set(self.cfg["auto_connect"])

        self._build()
        self.streamer.start()
        self.discovery.start()
        for m in self.cfg["manual"]:
            try:
                add_manual(self.discovery, m["host"], m.get("port", 5000), m.get("name"))
            except OSError as e:
                log.warning("manual receiver %s: %s", m, e)

        self.tray = None
        self._start_tray()
        if self.cfg.get("start_minimized") and self.tray:
            self.root.withdraw()
        self.root.after(200, self._tick)

    # ---------------------------------------------------------------- layout
    def _build(self):
        top = tk.Frame(self.root, bg=BG, padx=16, pady=12)
        top.pack(fill="x")
        tk.Label(top, text="TuneBlade", bg=BG, fg=FG, font=("Segoe UI Semibold", 16)).grid(row=0, column=0, sticky="w")
        self.source = tk.Label(top, text="", bg=BG, fg=DIM, font=("Segoe UI", 9))
        self.source.grid(row=1, column=0, sticky="w")
        top.columnconfigure(0, weight=1)

        self.meter = tk.Canvas(top, width=120, height=6, bg="#41444d", highlightthickness=0)
        self.meter.grid(row=1, column=1, padx=(8, 0))
        self.meter_bar = self.meter.create_rectangle(0, 0, 0, 6, fill=OK, outline="")

        mv = tk.Frame(self.root, bg=BG, padx=16)
        mv.pack(fill="x")
        tk.Label(mv, text="Master volume", bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(side="left")
        self.master_var = tk.DoubleVar(value=self.cfg["master_volume"])
        ttk.Scale(mv, from_=0, to=100, variable=self.master_var, style="Master.Horizontal.TScale",
                  command=self._on_master).pack(side="left", fill="x", expand=True, padx=10)
        self._master_job = None

        hdr = tk.Frame(self.root, bg=BG, padx=16, pady=(8))
        hdr.pack(fill="x")
        tk.Label(hdr, text="DEVICES", bg=BG, fg=DIM, font=("Segoe UI", 8, "bold")).pack(side="left")
        for text, cmd in (("Settings", self._settings), ("+ Add by IP", self._add_manual)):
            tk.Button(hdr, text=text, command=cmd, bg=BG, fg=ACCENT, activebackground=BG,
                      activeforeground=FG, relief="flat", bd=0, cursor="hand2",
                      font=("Segoe UI", 9)).pack(side="right", padx=(10, 0))

        self._build_web_card()

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=(16, 6), pady=(0, 12))
        self.canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.list = tk.Frame(self.canvas, bg=BG)
        self.list.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._list_win = self.canvas.create_window((0, 0), window=self.list, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self._list_win, width=e.width))
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

        self.empty = tk.Label(self.list, text="Searching for AirPlay, Chromecast and DLNA devices…",
                              bg=BG, fg=DIM, font=("Segoe UI", 10), pady=40)

    def _build_web_card(self):
        """Phones, tablets and PCs listen by opening a link in any browser."""
        card = tk.Frame(self.root, bg=PANEL, padx=12, pady=8)
        card.pack(fill="x", padx=(16, 22), pady=(0, 6))
        card.columnconfigure(1, weight=1)
        self.web_dot = tk.Canvas(card, width=12, height=12, bg=PANEL, highlightthickness=0)
        self.web_dot.grid(row=0, column=0, rowspan=2, padx=(0, 10))
        self.web_dot_id = self.web_dot.create_oval(2, 2, 11, 11, fill=DIM, outline="")
        tk.Label(card, text="Web browsers  (Android, Windows, iPhone, TV…)", bg=PANEL, fg=FG,
                 font=("Segoe UI Semibold", 11), anchor="w").grid(row=0, column=1, sticky="w")
        self.web_status = tk.Label(card, text="", bg=PANEL, fg=DIM, font=("Segoe UI", 9), anchor="w", cursor="hand2")
        self.web_status.grid(row=1, column=1, sticky="w")
        self.web_status.bind("<Button-1>", lambda e: self._copy_web_url())
        self.web_btn = tk.Button(card, text="QR code", width=11, relief="flat", bd=0, bg=ACCENT, fg="white",
                                 activebackground=ACCENT, activeforeground="white", cursor="hand2",
                                 font=("Segoe UI", 9, "bold"), command=self._show_qr)
        self.web_btn.grid(row=0, column=2, rowspan=2)
        self._web_card_text = None

    def _web_url(self) -> str | None:
        web = self.streamer.web
        return web.url() if web and web._httpd else None

    def _refresh_web_card(self):
        web = self.streamer.web
        url = self._web_url()
        if not web:
            text, color = "Off — enable in Settings", DIM
        elif not url:
            text, color = web.error or "starting…", ERR if web.error else DIM
        else:
            n = len(web.listeners())
            text = f"Open {url} · " + (f"{n} listening" if n else "nobody listening")
            color = OK if n else DIM
        if text != self._web_card_text:
            self._web_card_text = text
            self.web_status.config(text=text, fg=ERR if color == ERR else DIM)
            self.web_dot.itemconfig(self.web_dot_id, fill=color)
            self.web_btn.config(state="normal" if url else "disabled")

    def _copy_web_url(self):
        url = self._web_url()
        if url:
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            self.web_status.config(text=f"Copied {url}")
            self._web_card_text = None
            self.root.after(1500, self._refresh_web_card)

    def _show_qr(self):
        url = self._web_url()
        if not url:
            return
        import qrcode
        qr = qrcode.QRCode(border=2, box_size=8)
        qr.add_data(url)
        img = qr.make_image(fill_color="black", back_color="white").get_image().convert("RGB")
        win = tk.Toplevel(self.root, bg=BG, padx=20, pady=16)
        win.title("Listen on another device")
        win.resizable(False, False)
        win.transient(self.root)
        win._img = ImageTk.PhotoImage(img)
        tk.Label(win, text="Scan with your phone's camera", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 12)).pack(pady=(0, 10))
        tk.Label(win, image=win._img, bg=BG).pack()
        tk.Label(win, text=url, bg=BG, fg=ACCENT, font=("Segoe UI", 11)).pack(pady=(10, 2))
        tk.Label(win, text="Same Wi-Fi network required. If it doesn't load, allow TuneBlade\n"
                           "through Windows Firewall (Private networks).",
                 bg=BG, fg=DIM, font=("Segoe UI", 9), justify="center").pack()

    # ----------------------------------------------------------------- state
    def _mark_dirty(self):
        self._dirty = True  # called from worker threads; the Tk loop picks it up

    def _tick(self):
        self._ticks = getattr(self, "_ticks", 0) + 1
        if self._dirty:
            self._dirty = False
            self._sync_rows()
            self._refresh_web_card()
        elif self._ticks % 20 == 1:
            self._refresh_web_card()  # picks up server start and IP changes
        self.source.config(text=f"Source: {self.streamer.capture.device_name or '…'}")
        lvl = min(1.0, self.streamer.capture.level)
        self.meter.coords(self.meter_bar, 0, 0, 120 * lvl, 6)
        self.root.after(100, self._tick)

    def _sync_rows(self):
        receivers = self.discovery.snapshot()
        ids = [r.id for r in receivers]
        for rid in list(self.rows):
            if rid not in ids and not self.streamer.is_connected(rid):
                self.rows.pop(rid).frame.destroy()
        for r in receivers:
            row = self.rows.get(r.id)
            if row is None:
                row = self.rows[r.id] = ReceiverRow(self, self.list, r)
            row.receiver = r
        for row in self.rows.values():
            row.frame.pack_forget()
        if self.rows:
            self.empty.pack_forget()
            for rid in sorted(self.rows, key=lambda k: self.rows[k].receiver.name.lower()):
                self.rows[rid].frame.pack(fill="x", pady=(0, 6))
                self.rows[rid].refresh()
        else:
            self.empty.pack(fill="x")
        # Reconnect receivers that were streaming when we last exited.
        for r in receivers:
            if r.id in self._auto_pending:
                self._auto_pending.discard(r.id)
                self.streamer.connect(r, self.cfg["passwords"].get(r.id))
        self._update_tray()
        self._remember_connected()

    def _remember_connected(self):
        live = sorted(set(self.streamer.sessions) | self.streamer.connecting | self._auto_pending)
        if live != sorted(self.cfg["auto_connect"]):
            self.cfg["auto_connect"] = live
            self.save_soon()

    def toggle(self, receiver):
        s = self.streamer
        if s.is_connected(receiver.id) or receiver.id in s.connecting:
            s.disconnect(receiver.id)
            return
        pw = self.cfg["passwords"].get(receiver.id)
        if receiver.needs_password or receiver.id in s.errors and "password" in s.errors[receiver.id]:
            pw = simpledialog.askstring("Password", f"Password for {receiver.name}:", show="•",
                                        parent=self.root, initialvalue=pw or "")
            if pw is None:
                return
            self.cfg["passwords"][receiver.id] = pw
            self.save_soon()
        s.connect(receiver, pw)

    def _on_master(self, _=None):
        if self._master_job:
            self.root.after_cancel(self._master_job)
        self._master_job = self.root.after(120, self._apply_master)

    def _apply_master(self):
        v = round(self.master_var.get())
        self.cfg["master_volume"] = v
        self.streamer.set_master_volume(v)
        self.save_soon()

    def _add_manual(self):
        host = simpledialog.askstring("Add receiver", "IP address or hostname (optionally host:port):",
                                      parent=self.root)
        if not host:
            return
        host, _, port = host.strip().partition(":")
        port = int(port) if port.isdigit() else 5000
        try:
            add_manual(self.discovery, host, port)
        except OSError as e:
            messagebox.showerror("Add receiver", f"Can't resolve {host}: {e}", parent=self.root)
            return
        self.cfg["manual"].append({"host": host, "port": port})
        self.save_soon()

    def _settings(self):
        win = tk.Toplevel(self.root, bg=BG, padx=16, pady=16)
        win.title("Settings")
        win.resizable(False, False)
        win.transient(self.root)
        tk.Label(win, text="Buffer / latency (ms)", bg=BG, fg=FG, font=("Segoe UI", 10)).grid(row=0, column=0, sticky="w")
        lat = tk.IntVar(value=self.cfg["latency_ms"])
        tk.Spinbox(win, from_=500, to=5000, increment=250, textvariable=lat, width=8).grid(row=0, column=1, padx=8)
        tk.Label(win, text="Higher = more robust on weak Wi-Fi. Applies to new connections.",
                 bg=BG, fg=DIM, font=("Segoe UI", 8)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))
        web_on = tk.BooleanVar(value=self.cfg["web_enabled"])
        tk.Checkbutton(win, text="Web browser / Chromecast / DLNA streaming on port", variable=web_on, bg=BG, fg=FG,
                       selectcolor=PANEL, activebackground=BG, activeforeground=FG).grid(row=5, column=0, sticky="w")
        port = tk.IntVar(value=self.cfg["web_port"])
        tk.Spinbox(win, from_=1024, to=65535, textvariable=port, width=8).grid(row=5, column=1, padx=8)
        tk.Label(win, text="Web changes apply after restarting TuneBlade.", bg=BG, fg=DIM,
                 font=("Segoe UI", 8)).grid(row=6, column=0, columnspan=2, sticky="w")
        mini = tk.BooleanVar(value=self.cfg.get("start_minimized", False))
        tk.Checkbutton(win, text="Start minimized to tray", variable=mini, bg=BG, fg=FG, selectcolor=PANEL,
                       activebackground=BG, activeforeground=FG).grid(row=2, column=0, columnspan=2, sticky="w")
        auto = tk.BooleanVar(value=get_autostart())
        tk.Checkbutton(win, text="Start with Windows", variable=auto, bg=BG, fg=FG, selectcolor=PANEL,
                       activebackground=BG, activeforeground=FG).grid(row=3, column=0, columnspan=2, sticky="w")

        def ok():
            if bool(auto.get()) != get_autostart():
                set_autostart(bool(auto.get()))
            try:
                ms = max(500, min(5000, int(lat.get())))
            except (tk.TclError, ValueError):
                ms = self.cfg["latency_ms"]
            self.cfg["latency_ms"] = ms
            self.cfg["start_minimized"] = bool(mini.get())
            self.cfg["web_enabled"] = bool(web_on.get())
            try:
                self.cfg["web_port"] = max(1024, min(65535, int(port.get())))
            except (tk.TclError, ValueError):
                pass
            self.streamer.latency_frames = int(44100 * ms / 1000)
            self.save_soon()
            win.destroy()

        tk.Button(win, text="Save", command=ok, bg=ACCENT, fg="white", relief="flat", width=10).grid(
            row=7, column=1, sticky="e", pady=(12, 0))

    def save_soon(self):
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(500, lambda: config.save(self.cfg))

    # ------------------------------------------------------------------ tray
    def _start_tray(self):
        try:
            import pystray
        except ImportError:
            return
        self._pystray = pystray
        menu = pystray.Menu(
            pystray.MenuItem("Show TuneBlade", lambda: self.root.after(0, self.show), default=True),
            pystray.MenuItem("Disconnect all", lambda: self.root.after(0, self._disconnect_all)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.root.after(0, self.quit)),
        )
        self.tray = pystray.Icon("TuneBlade", make_icon(64, DIM), "TuneBlade", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _update_tray(self):
        if not self.tray:
            return
        n = len(self.streamer.sessions)
        self.tray.icon = make_icon(64, ACCENT if n else DIM)
        self.tray.title = f"TuneBlade — streaming to {n} receiver(s)" if n else "TuneBlade — idle"

    def _disconnect_all(self):
        for rid in list(self.streamer.sessions):
            self.streamer.disconnect(rid)

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        if self.tray:
            self.root.withdraw()
        else:
            self.quit()

    def quit(self):
        self._remember_connected()
        config.save(self.cfg)
        self.discovery.stop()
        self.streamer.stop()
        if self.tray:
            self.tray.stop()
        self.root.after(300, self.root.destroy)

    def run(self):
        self.root.mainloop()


def enable_dpi_awareness():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw if os.path.exists(pyw) else sys.executable}" -m tuneblade'


def get_autostart() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "TuneBlade")
            return True
    except OSError:
        return False


def set_autostart(enabled: bool):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, "TuneBlade", 0, winreg.REG_SZ, _launch_command())
            else:
                try:
                    winreg.DeleteValue(k, "TuneBlade")
                except FileNotFoundError:
                    pass
    except OSError as e:
        log.warning("autostart: %s", e)


def _already_running() -> bool:
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        _already_running.handle = kernel32.CreateMutexW(None, False, "Local\\TuneBladePyMutex")
        return kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except (AttributeError, OSError):
        return False


def main():
    enable_dpi_awareness()
    if _already_running():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("TuneBlade", "TuneBlade is already running — look for its icon in the system tray.")
        root.destroy()
        return
    App().run()
