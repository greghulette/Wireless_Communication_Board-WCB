"""WCB Bench — the GUI for the hardware-in-the-loop tests.

    python tests/hil/gui.py

Devices  find which COM port is which board, and check each one
Wiring   what to connect; auto-detect the wires, or click a WCB port then a probe header
Tests    run one test (double-click), an area, everything, or only what failed; tick the opt-in tests;
         each test's expected time, and the time left in a run
Log      every serial line in and out

All bench work runs on one worker thread, in order, so two actions never fight over a COM
port. The window only reads results from a queue. Past runs' durations (hil/durations.py) are read on a
thread of their own, since they touch no port and must not wait behind a run.
"""
import ctypes
import importlib
import json
import os
import pkgutil
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from serial.tools import list_ports  # noqa: E402
except ImportError:
    # VS Code can pick a different interpreter (e.g. a uv-managed Python) than the PATH one.
    msg = (f"pyserial is not installed for this Python:\n  {sys.executable}\n\n"
           f"Either run the GUI with the Python that has it (double-click 'WCB Bench.cmd'), or install it:\n"
           f"  \"{sys.executable}\" -m pip install pyserial")
    try:
        import tkinter.messagebox as _mb
        _root = tk.Tk()
        _root.withdraw()
        _mb.showerror("WCB Bench", msg)
    except Exception:
        pass
    print(msg)
    sys.exit(1)

from hil import checkpoint, durations, optin, runner, wiring, wizard  # noqa: E402
from hil.checkpoint import CheckpointError, RunBusy  # noqa: E402
from hil.identify import ESP_VIDS, identify_port, usb_fingerprint  # noqa: E402
from hil.navicore import NaviCore  # noqa: E402
from hil.resume import ResumeAborted, ResumeBlocked  # noqa: E402
from hil.runner import DEVICE_KINDS, describe_device, fmt_duration  # noqa: E402
from hil.probe import HEADERS  # noqa: E402
from hil.serialdev import ExpectTimeout  # noqa: E402
from hil.wcb import WCB  # noqa: E402
import suites  # noqa: E402

for _mod in pkgutil.iter_modules(suites.__path__):
    importlib.import_module(f"suites.{_mod.name}")
checkpoint.freeze_harness(HERE)   # the sources this process runs, for the resume's "test code changed" check

BENCH_PATH = os.path.join(HERE, "bench.json")
RESULTS = os.path.join(HERE, "results")

FONT = ("Segoe UI", 10)
BOLD = ("Segoe UI", 10, "bold")
MONO = ("Consolas", 9)

# Two palettes, same keys. DARK is the default — the bench GUI sits open for hour-long runs and the
# white one is tiring; `gui.py --light` restores the original. Status colours are picked per palette
# so they stay legible on their own background (the light greens/reds vanish on dark).
LIGHT = dict(bg="#f6f8fa", panel="#ffffff", card="#f6f8fa", edge="#d0d7de", fg="#1f2328", mute="#57606a",
             sel="#dbeafe", entry="#ffffff", logbg="#0d1117", logfg="#c9d1d9", probe="#fff8f0",
             green="#1a7f37", amber="#bf8700", red="#cf222e", blue="#0969da", grey="#8c959f")
DARK = dict(bg="#11161d", panel="#161b22", card="#1c232c", edge="#30363d", fg="#d7dee6", mute="#8b949e",
            sel="#243044", entry="#0d1117", logbg="#0d1117", logfg="#c9d1d9", probe="#241f16",
            green="#3fb950", amber="#d29922", red="#f85149", blue="#58a6ff", grey="#8b949e")
THEME = DARK   # main() swaps in LIGHT for --light, before any widget is built

GREEN, AMBER, RED, BLUE, GREY = THEME["green"], THEME["amber"], THEME["red"], THEME["blue"], THEME["grey"]
STATUS_COLOR = {"PASS": GREEN, "FAIL": RED, "ERROR": RED, "SKIP": GREY, "RUNNING": BLUE, "RETRY": AMBER}


def _dark_titlebar(win):
    """The Windows title bar is OS chrome, not Tk: it stays white unless the window asks for the dark
    one. DWMWA_USE_IMMERSIVE_DARK_MODE is 20 on current Windows 10/11 and 19 on early 1809-1903
    builds; both are set and failures ignored (older Windows, or a non-DWM session)."""
    if THEME is not DARK:
        return
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        flag = ctypes.c_int(1)
        for attr in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(flag), ctypes.sizeof(flag))
    except Exception:
        pass


class Worker(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.jobs = queue.Queue()

    def submit(self, name, fn, *args):
        self.jobs.put((name, fn, args))

    def run(self):
        while True:
            name, fn, args = self.jobs.get()
            self.app.emit("busy", name)
            try:
                fn(*args)
            except Exception:
                self.app.run_active = False   # a crashed run must not lock the button out
                self.app.emit("error", f"{name} failed:\n{traceback.format_exc()}")
            finally:
                self.app.emit("idle", name)


# ---------------------------------------------------------------------------- the app
class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        # Stop and Pause are one pending request under a lock, so the last one pressed wins even if the worker reads
        # between the two presses. Cross-thread state is only this, the event queue, run_active and closing.
        self.control = runner.RunControl()
        self.closing = False             # the window is going away: ask_user answers No, nothing waits on the user
        self.current_ckpt = None         # the checkpoint of the run on the worker (closing "now" freezes it)
        # Tk-thread only:
        self.resumable = None            # summary of the run the banner offers
        self.resumable_all = []          # every paused / interrupted / stopped run, for the picker
        self.run_base = 0.0              # active time before this segment, for the elapsed timer
        self.close_after_pause = False
        self.bench = runner.Bench(BENCH_PATH, RESULTS, log_sink=lambda line: self.emit("log", line))
        self.results = {}       # test id -> (status, detail, dur)
        self.identities = {}    # device name -> text
        self.pending_port = None
        self.selected_key = None
        self.last_report = None
        self.job = None          # (name, start) of the job the worker is running now
        self.last_job = None     # (name, seconds) of the last job that finished
        self.run_total = self.run_done_n = 0
        self.test_now = None     # (test id, start) while a test runs
        self.run_active = False  # a run is on the worker right now - see run_tests()
        self.run_ids = []        # the test ids of the run on the worker, in order, for the time-left estimate
        self.history = {}        # test id -> (median seconds, samples) of past runs, from hil/durations.py
        self.durations_busy = self.durations_again = False
        self.optin_vars = {}     # opt-in key -> BooleanVar of its checkbox
        self.skip_now = {}       # test id -> skipped at once for a missing wire or device (set by refresh_tests)
        self.worker = Worker(self)
        self.worker.start()
        self.build_ui()
        self.root.after(100, self.pump)
        self.root.after(500, self.tick)
        self.refresh_wiring()
        self.refresh_tests()
        self.scan_resumable()
        self.load_durations()
        if self.resumable:
            self.status("A paused run can be resumed — see the Tests tab")

    def emit(self, kind, *data):
        self.events.put((kind, data))

    def submit(self, name, fn, *args):
        self.status(f"Queued: {name}")
        self.worker.submit(name, fn, *args)

    def status(self, text):
        self.status_var.set(text)

    # ------------------------------------------------------------------ layout
    def build_ui(self):
        self.root.title("WCB Bench")
        self.root.geometry("1320x860")
        style = ttk.Style()
        # "clam" is the only built-in theme whose element colours are fully settable — "vista" ignores
        # most of them, so a dark palette needs it.
        try:
            style.theme_use("clam" if THEME is DARK else "vista")
        except tk.TclError:
            pass
        t = THEME
        self.root.configure(bg=t["bg"])
        style.configure(".", font=FONT, background=t["bg"], foreground=t["fg"],
                        fieldbackground=t["entry"], bordercolor=t["edge"],
                        lightcolor=t["edge"], darkcolor=t["edge"])
        style.configure("TFrame", background=t["bg"])
        style.configure("TLabel", background=t["bg"], foreground=t["fg"])
        style.configure("TLabelframe", background=t["bg"], foreground=t["fg"])
        style.configure("TLabelframe.Label", background=t["bg"], foreground=t["fg"])
        style.configure("TCheckbutton", background=t["bg"], foreground=t["fg"], indicatorbackground=t["entry"],
                        indicatorforeground=t["fg"])
        # clam lightens a hovered checkbox's background to its own grey; keep it on the palette
        style.map("TCheckbutton", background=[("active", t["bg"])], foreground=[("disabled", t["mute"])],
                  indicatorbackground=[("disabled", t["card"]), ("pressed", t["sel"])])
        style.configure("TButton", background=t["card"], foreground=t["fg"], padding=4)
        style.map("TButton", background=[("active", t["sel"]), ("pressed", t["sel"])])
        style.configure("TNotebook", background=t["bg"], bordercolor=t["edge"])
        style.configure("TNotebook.Tab", background=t["card"], foreground=t["mute"], padding=(14, 6))
        style.map("TNotebook.Tab", background=[("selected", t["panel"])], foreground=[("selected", t["fg"])])
        style.configure("TCombobox", fieldbackground=t["entry"], background=t["card"], foreground=t["fg"],
                        arrowcolor=t["fg"])
        style.configure("TEntry", fieldbackground=t["entry"], foreground=t["fg"], insertcolor=t["fg"])
        style.configure("Vertical.TScrollbar", background=t["card"], troughcolor=t["bg"], arrowcolor=t["fg"])
        style.configure("Horizontal.TScrollbar", background=t["card"], troughcolor=t["bg"], arrowcolor=t["fg"])
        style.configure("Treeview", rowheight=24, background=t["panel"], fieldbackground=t["panel"],
                        foreground=t["fg"])
        style.configure("Treeview.Heading", background=t["card"], foreground=t["fg"])
        style.map("Treeview", background=[("selected", t["sel"])], foreground=[("selected", t["fg"])])

        top = ttk.Frame(self.root, padding=(12, 8))
        top.pack(fill="x")
        ttk.Button(top, text="Open results folder", command=self.open_results).pack(side="right")
        ttk.Button(top, text="Close all ports",
                   command=lambda: self.submit("close ports", self.job_close_ports)).pack(side="right", padx=6)
        ttk.Button(top, text="Stop after this test", command=self.stop).pack(side="right")
        # Pause is only for a run in progress; a paused run is resumed from the Tests tab's banner, never from here,
        # so a click as a run ends cannot resume some other paused run.
        self.pause_btn = ttk.Button(top, text="Pause after this test", width=22, command=self.on_pause_button)
        self.pause_btn.pack(side="right", padx=(0, 6))
        self.pause_btn.state(["disabled"])
        self.timer_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.timer_var, font=BOLD, foreground=BLUE).pack(side="top", anchor="w")
        # The time left and finish clock get a line of their own: appended to the timer they were clipped off it at
        # the default width (the four buttons on the right leave the timer ~630 px).
        self.eta_var = tk.StringVar(value="")
        self.eta_label = ttk.Label(top, textvariable=self.eta_var, foreground=BLUE)
        self.eta_label.pack(side="top", anchor="w")
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(top, textvariable=self.status_var).pack(side="top", anchor="w")

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.build_devices()
        self.build_wiring()
        self.build_tests()
        self.build_log()

    # ------------------------------------------------------------------ Devices tab
    def build_devices(self):
        f = ttk.Frame(self.nb, padding=14)
        self.nb.add(f, text="  Devices  ")
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Button(bar, text="Find devices", command=lambda: self.submit("find devices", self.job_find_devices,
                                                                         self.paused_path())).pack(side="left")
        ttk.Label(bar, text="   Scans the ESP32 USB ports and works out which board is on each one. "
                            "Pick a port by hand below if you prefer.").pack(side="left")
        add = ttk.Frame(f)
        add.pack(fill="x", pady=(10, 0))
        ttk.Label(add, text="Add a USB device:").pack(side="left")
        self.add_port = tk.StringVar()
        self.add_port_cb = ttk.Combobox(add, textvariable=self.add_port, width=10)
        self.add_port_cb.pack(side="left", padx=6)
        ttk.Button(add, text="Add", command=self.add_device).pack(side="left")
        ttk.Label(add, text="   Pick the COM port you just plugged in — it works out whether it is a WCB (and which "
                            "number), a probe, NaviCore or the SBUS controller.").pack(side="left")
        self.dev_grid = ttk.Frame(f)
        self.dev_grid.pack(fill="x", pady=14)
        ttk.Label(f, foreground=GREY, wraplength=1100, justify="left",
                  text="Close the Arduino IDE serial monitor and any Wizard / NaviCore browser tabs first — "
                       "a COM port can only have one owner. 'Close all ports' releases them again.").pack(anchor="w")
        self.render_devices()

    def render_devices(self):
        for w in self.dev_grid.winfo_children():
            w.destroy()
        # USB serial ports only. Every bench device is a USB board; a Bluetooth SPP port (no VID) blocks writes
        # indefinitely when nothing is paired, and on 2026-09-22 sbus ended up on COM19 that way and hung a run.
        ports = sorted(p.device for p in list_ports.comports() if p.vid is not None)
        self.add_port_cb.configure(values=ports)
        for col, h in enumerate(("Device", "Kind", "COM port", "Status", "", "")):
            ttk.Label(self.dev_grid, text=h, font=BOLD).grid(row=0, column=col, sticky="w", padx=8, pady=4)
        self.dev_status = {}
        for r, (name, d) in enumerate(self.bench.cfg["devices"].items(), start=1):
            ttk.Label(self.dev_grid, text=name).grid(row=r, column=0, sticky="w", padx=8, pady=3)
            kind = d["kind"]
            if d["kind"] == "wcb":
                kind += f"  (WCB{d.get('wcb', '?')}, on USB" + (", main console)" if name == "wcb1" else ")")
            if name != "wcb1":
                ttk.Button(self.dev_grid, text="Remove",
                           command=lambda n=name: self.submit(f"remove {n}", self.job_remove_device, n)).grid(row=r, column=5, padx=4)
            ttk.Label(self.dev_grid, text=kind).grid(row=r, column=1, sticky="w", padx=8)
            var = tk.StringVar(value=d.get("port", ""))
            cb = ttk.Combobox(self.dev_grid, textvariable=var, values=ports, width=10, state="readonly")
            cb.grid(row=r, column=2, sticky="w", padx=8)
            # A Tk combobox steps through its values on the mouse wheel and fires <<ComboboxSelected>>, which SAVES
            # the port - so scrolling the tab with the pointer over a row silently re-assigned a board. Wheel off.
            cb.bind("<MouseWheel>", lambda e: "break")
            cb.bind("<<ComboboxSelected>>",
                    lambda e, n=name, v=var: self.submit(f"set {n} port", self.job_set_port, n, v.get()))
            sv = tk.StringVar(value=self.identities.get(name, "not checked yet"))
            ttk.Label(self.dev_grid, textvariable=sv, width=70).grid(row=r, column=3, sticky="w", padx=8)
            self.dev_status[name] = sv
            ttk.Button(self.dev_grid, text="Check",
                       command=lambda n=name: self.submit(f"check {n}", self.job_check_device, n)).grid(row=r, column=4, padx=8)
        usb = self.bench.usb_wcbs()
        mesh = ", ".join(f"WCB{w}" for w in self.bench.cfg.get("mesh_only_wcbs", []) if w not in usb) or "none"
        ttk.Label(self.dev_grid, text=f"Reached over the mesh only (no USB): {mesh}", foreground=GREY).grid(
            row=len(self.bench.cfg["devices"]) + 1, column=0, columnspan=6, sticky="w", padx=8, pady=(10, 0))

    def add_device(self):
        port = self.add_port.get().strip()
        if not port:
            self.status("Pick the COM port to add first")
            return
        self.submit(f"add device on {port}", self.job_add_device, port, self.paused_path())

    def paused_path(self):
        """Tk thread: the run the banner offers, handed to a job at submit time."""
        return self.resumable["path"] if self.resumable else None

    # Called on the worker thread by Find devices and Add, before they save bench.json.
    def paused_serial_ok(self, before, paused_path):
        """A paused run pins each board by the USB serial it recorded. Moving a device name onto a board with another
        serial is asked about here, before it is saved - the resume would ask again anyway."""
        if not paused_path:
            return True
        try:
            rec = checkpoint.Checkpoint.load(paused_path).data.get("devices") or {}
        except CheckpointError:
            return True
        warn = []
        for name, d in self.bench.cfg["devices"].items():
            was = (rec.get(name) or {}).get("serial")
            if not was or before.get(name) == d.get("port"):
                continue
            now = usb_fingerprint(d.get("port"))["serial"]
            if now != was:
                warn.append(f"{name} -> {d.get('port')}: USB serial {now}, but the paused run recorded {was}")
        if not warn:
            return True
        return self.ask_user("Not the paused run's boards",
                             "These boards are not the ones the paused run " + os.path.basename(paused_path) +
                             " recorded:\n\n" + "\n".join(warn) + "\n\nSave them to bench.json anyway? "
                             "(Resuming that run asks about them again.)")

    # Called on the worker thread by Find devices and Add.
    def assign_device(self, port, info):
        b = self.bench
        devices = b.cfg["devices"]
        kind = info["kind"]
        if kind == "wcb":
            n = info.get("wcb")
            if n is None:
                return f"{port}: a WCB that did not report its board number"
            name = next((k for k, d in devices.items() if d["kind"] == "wcb" and d.get("wcb") == n), None)
            if name is None:
                name = "wcb1" if "wcb1" not in devices else f"wcb{n}"
                devices[name] = {"port": port, "kind": "wcb", "wcb": n}
            devices[name]["port"] = port
            b.cfg["mesh_only_wcbs"] = [w for w in b.cfg.get("mesh_only_wcbs", []) if w != n]
            return f"{name} = {port} (WCB{n}, firmware {info['version']})"
        if kind == "probe":
            probes = [k for k, d in devices.items() if d["kind"] == "probe"]
            name = next((k for k in probes if devices[k].get("mac") == info["mac"]), None)
            if name is None:
                name = next((k for k in probes if "mac" not in devices[k]), None)
            if name is None:
                i = 1
                while f"probe{i}" in devices:
                    i += 1
                name = f"probe{i}"
                devices[name] = {"kind": "probe"}
            devices[name]["port"] = port
            devices[name]["mac"] = info["mac"]
            return f"{name} = {port} (probe mac {info['mac']}, v{info['version']})"
        if kind in ("navicore", "sbus"):
            name = next((k for k, d in devices.items() if d["kind"] == kind), kind)
            devices.setdefault(name, {"kind": kind})["port"] = port
            return f"{name} = {port}"
        return f"{port}: could not open — {info.get('error', 'unknown')}"

    def job_add_device(self, port, paused_path=None):
        b = self.bench
        for name, d in list(b.cfg["devices"].items()):
            if d.get("port") == port:
                b.close_device(name)
        info = identify_port(port)
        if not info:
            self.emit("status", f"{port}: nothing answered — is it an ESP32 board running WCB, probe, NaviCore or SBUS firmware?")
            return
        before = {n: d.get("port") for n, d in b.cfg["devices"].items()}
        text = self.assign_device(port, info)
        if not self.paused_serial_ok(before, paused_path):
            b.reload_config()
            self.emit("devices_changed")
            self.emit("status", f"Add {port}: not saved")
            return
        b.save_config()
        self.emit("devices_changed")
        self.emit("status", "Added: " + text)

    def job_remove_device(self, name):
        b = self.bench
        d = b.cfg["devices"].get(name)
        if not d:
            return
        b.close_device(name)
        if d["kind"] == "wcb" and d.get("wcb") is not None:
            mesh = b.cfg.setdefault("mesh_only_wcbs", [])
            if d["wcb"] not in mesh:
                mesh.append(d["wcb"])
        del b.cfg["devices"][name]
        b.save_config()
        self.emit("devices_changed")
        self.emit("status", f"Removed {name}" + (f" — WCB{d['wcb']} is reached over the mesh again" if d["kind"] == "wcb" else ""))

    # ------------------------------------------------------------------ Wiring tab
    def build_wiring(self):
        f = ttk.Frame(self.nb, padding=14)
        self.nb.add(f, text="  Wiring  ")
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Button(bar, text="Auto-detect wires", command=lambda: self.submit("auto-detect wires", self.job_discover)).pack(side="left")
        ttk.Label(bar, text="   Each WCB port transmits in turn and the probes report which pin moved. "
                            "Or click a WCB port, then a probe header, to add a wire yourself.").pack(side="left")

        body = ttk.Frame(f)
        body.pack(fill="both", expand=True, pady=(10, 0))
        # The right-hand pane is packed first so it keeps its width, and it scrolls: at Windows display scaling
        # its forms are taller than the window.
        side = ttk.Frame(body, padding=(14, 0, 0, 0))
        side.pack(side="right", fill="y")
        rcanvas = tk.Canvas(side, highlightthickness=0, borderwidth=0)
        rscroll = ttk.Scrollbar(side, orient="vertical", command=rcanvas.yview)
        rcanvas.configure(yscrollcommand=rscroll.set)
        rscroll.pack(side="right", fill="y")
        rcanvas.pack(side="left", fill="y", expand=True)
        right = ttk.Frame(rcanvas)
        rcanvas.create_window((0, 0), window=right, anchor="nw")
        right.bind("<Configure>", lambda e: rcanvas.configure(scrollregion=rcanvas.bbox("all"), width=right.winfo_reqwidth()))

        def wheel(ev):
            w = self.root.winfo_containing(ev.x_root, ev.y_root)
            while w is not None and w is not side:
                w = w.master
            if w is side:
                rcanvas.yview_scroll(int(-ev.delta / 120), "units")
        self.root.bind_all("<MouseWheel>", wheel, add="+")

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(left, width=600, height=600, bg=THEME["panel"], highlightthickness=1,
                                highlightbackground=THEME["edge"])
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        ttk.Label(left, foreground=GREY, wraplength=600, justify="left",
                  text="green = verified   amber = found but not verified   dashed = listen-only tap   "
                       "dotted grey = planned, not wired yet   blue port = a real device").pack(anchor="w", pady=(6, 0))

        ttk.Label(right, text="What to connect", font=BOLD).pack(anchor="w")
        self.plan_tree = ttk.Treeview(right, columns=("status", "wire", "unlocks"), show="tree headings", height=10)
        self.plan_tree.heading("#0", text="WCB port")
        self.plan_tree.heading("status", text="Status")
        self.plan_tree.heading("wire", text="Wire to")
        self.plan_tree.heading("unlocks", text="Tests")
        self.plan_tree.column("#0", width=90)
        self.plan_tree.column("status", width=130)
        self.plan_tree.column("wire", width=150)
        self.plan_tree.column("unlocks", width=50, anchor="center")
        for s, c in (("connected", GREEN), ("found, not verified", AMBER), ("to do", RED), ("device", BLUE)):
            self.plan_tree.tag_configure(s, foreground=c)
        self.plan_tree.pack(fill="x")
        self.plan_tree.bind("<<TreeviewSelect>>", self.on_plan_select)

        self.plan_text = tk.Text(right, height=8, width=48, wrap="word", font=FONT, relief="flat",
                                 bg=THEME["card"], fg=THEME["fg"], insertbackground=THEME["fg"])
        self.plan_text.pack(fill="x", pady=8)
        row = ttk.Frame(right)
        row.pack(fill="x")
        ttk.Button(row, text="Verify this wire", command=self.verify_selected).pack(side="left")
        ttk.Button(row, text="Remove this wire", command=self.remove_selected).pack(side="left", padx=6)
        ttk.Button(row, text="Listen-only tap on/off", command=self.toggle_tap_selected).pack(side="left")

        ttk.Separator(right).pack(fill="x", pady=12)
        ttk.Label(right, text="Add a wire by hand", font=BOLD).pack(anchor="w")
        form = ttk.Frame(right)
        form.pack(fill="x", pady=4)
        self.man_port = tk.StringVar()
        self.man_probe = tk.StringVar()
        self.man_header = tk.StringVar(value="S1")
        ttk.Label(form, text="WCB port").grid(row=0, column=0, sticky="w")
        self.man_port_cb = ttk.Combobox(form, textvariable=self.man_port, width=8, state="readonly")
        self.man_port_cb.grid(row=1, column=0, padx=(0, 8))
        ttk.Label(form, text="Probe").grid(row=0, column=1, sticky="w")
        self.man_probe_cb = ttk.Combobox(form, textvariable=self.man_probe, width=10, state="readonly")
        self.man_probe_cb.grid(row=1, column=1, padx=(0, 8))
        ttk.Label(form, text="Header").grid(row=0, column=2, sticky="w")
        ttk.Combobox(form, textvariable=self.man_header, values=HEADERS, width=5, state="readonly").grid(row=1, column=2, padx=(0, 8))
        ttk.Button(form, text="Connect + verify", command=self.connect_manual).grid(row=1, column=3)
        ttk.Label(right, foreground=GREY, wraplength=440, justify="left",
                  text="Verify tries both cable orientations, so crossed or straight-through does not matter.").pack(anchor="w", pady=(6, 0))

        ttk.Separator(right).pack(fill="x", pady=12)
        ttk.Label(right, text="A real device on a WCB port", font=BOLD).pack(anchor="w")
        dform = ttk.Frame(right)
        dform.pack(fill="x", pady=4)
        self.dev_port = tk.StringVar()
        self.dev_kind = tk.StringVar(value=DEVICE_KINDS["maestro"][0])
        self.dev_detail = tk.StringVar()
        self.dev_hint = tk.StringVar()
        ttk.Label(dform, text="WCB port").grid(row=0, column=0, sticky="w")
        ttk.Label(dform, text="Device").grid(row=0, column=1, sticky="w")
        self.dev_port_cb = ttk.Combobox(dform, textvariable=self.dev_port, width=8, state="readonly")
        self.dev_port_cb.grid(row=1, column=0, padx=(0, 8), sticky="w")
        self.dev_port_cb.bind("<<ComboboxSelected>>", lambda e: self.fill_device_form(self.dev_port.get()))
        kind_cb = ttk.Combobox(dform, textvariable=self.dev_kind, values=[v[0] for v in DEVICE_KINDS.values()],
                               width=20, state="readonly")
        kind_cb.grid(row=1, column=1, sticky="w")
        kind_cb.bind("<<ComboboxSelected>>", lambda e: self.dev_hint.set(self.kind_hint()))
        ttk.Label(dform, textvariable=self.dev_hint).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Entry(dform, textvariable=self.dev_detail, width=10).grid(row=3, column=0, padx=(0, 8), sticky="w")
        btns = ttk.Frame(dform)
        btns.grid(row=3, column=1, sticky="w")
        ttk.Button(btns, text="Set", command=self.set_port_device).pack(side="left")
        ttk.Button(btns, text="Clear", command=self.clear_port_device).pack(side="left", padx=6)
        self.dev_hint.set(self.kind_hint())
        ttk.Label(right, foreground=GREY, wraplength=400, justify="left",
                  text="For a Maestro, NaviCore port or anything else that is not a probe. A probe wire there stays "
                       "listen-only. Unless bench.json has a port_stimulus for the port, tests never send traffic to "
                       "it and skip naming the device, auto-detect does not sweep it, and a wire added there by hand "
                       "is kept.").pack(anchor="w", pady=(4, 0))

    def refresh_wiring(self):
        wcbs = self.bench.wcb_numbers()
        probes = self.bench.probe_names()
        self.man_port_cb.configure(values=[f"W{w}{p}" for w in wcbs for p in HEADERS])
        self.dev_port_cb.configure(values=[f"W{w}{p}" for w in wcbs for p in HEADERS])
        self.man_probe_cb.configure(values=probes)
        if not self.man_probe.get() and probes:
            self.man_probe.set(probes[0])
        self.draw_wiring()
        sel = self.selected_key
        self.plan_tree.delete(*self.plan_tree.get_children())
        self.plan_rows = {r["key"]: r for r in wiring.plan(self.bench)}
        for key, r in self.plan_rows.items():
            link = self.bench.links.get(r["wcb"], r["port"], raw=True)
            if link:
                wire_to = f"{link.probe_name} {link.header}"
            elif r["device"]:
                wire_to = r["device"]
            else:
                wire_to = f"{r['probe']} {r['header']}" if r["probe"] else "—"
            label = key + ("  (tap)" if r["tap"] and not r["device"] else "")
            self.plan_tree.insert("", "end", iid=key, text=label, values=(r["status"], wire_to, len(r["unlocks"])),
                                  tags=(r["status"],))
        if sel in self.plan_rows:
            self.plan_tree.selection_set(sel)
            self.show_plan(sel)

    def draw_wiring(self):
        c = self.canvas
        c.delete("all")
        self.xy = {}
        wcbs = self.bench.wcb_numbers()
        probes = self.bench.probe_names()
        box_h, pitch = 40 + 5 * 36, 30
        usb = self.bench.usb_wcbs()
        for i, w in enumerate(wcbs):
            x, y = 24, 16 + i * (box_h + pitch)
            c.create_rectangle(x, y, x + 210, y + box_h, fill=THEME["card"], outline=THEME["edge"], width=2)
            c.create_text(x + 12, y + 16, anchor="w", font=BOLD, text=f"WCB{w}  " + ("(USB)" if w in usb else "(mesh)"), fill=THEME["fg"])
            labels = self.port_labels(w)
            for k, port in enumerate(HEADERS):
                py = y + 46 + k * 36
                px = x + 210
                key = f"W{w}{port}"
                hl = self.pending_port == key or self.selected_key == key
                c.create_oval(px - 9, py - 9, px + 9, py + 9, fill=BLUE if hl else THEME["panel"], outline=THEME["mute"],
                              width=2, tags=("wport", key))
                dev = self.bench.port_devices().get(key)
                if dev:
                    c.create_text(x + 14, py, anchor="w", text=f"{port}  {describe_device(dev)}"[:28], fill=BLUE)
                else:
                    c.create_text(x + 14, py, anchor="w", text=f"{port}  {labels.get(port, '')}"[:26], fill=THEME["fg"])
                self.xy[("W", key)] = (px, py)
        for j, pn in enumerate(probes):
            x, y = 460, 16 + j * (box_h + pitch)
            c.create_rectangle(x, y, x + 210, y + box_h, fill=THEME["probe"], outline=THEME["edge"], width=2)
            port = self.bench.cfg["devices"][pn].get("port", "?")
            c.create_text(x + 24, y + 16, anchor="w", font=BOLD, text=f"{pn}  ({port})", fill=THEME["fg"])
            for k, header in enumerate(HEADERS):
                py = y + 46 + k * 36
                px = x
                c.create_oval(px - 9, py - 9, px + 9, py + 9, fill=THEME["panel"], outline=THEME["mute"], width=2,
                              tags=("pport", f"{pn}:{header}"))
                c.create_text(x + 24, py, anchor="w", text=f"header {header}", fill=THEME["fg"])
                self.xy[("P", f"{pn}:{header}")] = (px, py)
        for r in wiring.plan(self.bench):
            if r["status"] == "to do" and r["probe"] in probes:
                a, b = self.xy[("W", r["key"])], self.xy[("P", f"{r['probe']}:{r['header']}")]
                c.create_line(*a, *b, fill=THEME["edge"], width=2, dash=(2, 4))
        for link in self.bench.links.all():
            a = self.xy.get(("W", link.key))
            b = self.xy.get(("P", f"{link.probe_name}:{link.header}"))
            if not a or not b:
                continue
            color = GREEN if link.verified else AMBER
            width = 5 if self.selected_key == link.key else 3
            c.create_line(*a, *b, fill=color, width=width, dash=(8, 5) if link.tap else None, tags=("link", link.key))
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            if link.swap:
                c.create_text(mx, my - 9, text="straight-through", fill=GREY, font=("Segoe UI", 8))

    def port_labels(self, wcb):
        labels = {}
        for t in self.bench.cache.get(wcb, []):
            m = re.match(r"^\?LABEL,(S[1-5]),(.*)$", t, re.I)
            if m:
                labels[m.group(1).upper()] = m.group(2)
        return labels

    def on_canvas_click(self, event):
        items = self.canvas.find_overlapping(event.x - 4, event.y - 4, event.x + 4, event.y + 4)
        for item in reversed(items):
            tags = self.canvas.gettags(item)
            if "wport" in tags:
                key = tags[1]
                self.pending_port = key
                self.select_key(key)
                self.status(f"{key} selected — now click a probe header to wire it there")
                return
            if "pport" in tags:
                pn, header = tags[1].split(":")
                if not self.pending_port:
                    self.status("Click a WCB port first, then the probe header")
                    return
                key = self.pending_port
                self.pending_port = None
                w, port = runner.parse_key(key)
                self.submit(f"wire {key} -> {pn} {header}", self.job_add_link, w, port, pn, header)
                return
            if "link" in tags:
                self.select_key(tags[1])
                return
        self.pending_port = None
        self.draw_wiring()

    def select_key(self, key):
        self.selected_key = key
        if key in self.plan_rows:
            self.plan_tree.selection_set(key)
            self.plan_tree.see(key)
            self.show_plan(key)
        self.man_port.set(key)
        self.dev_port.set(key)
        self.fill_device_form(key)
        self.draw_wiring()

    def on_plan_select(self, _e):
        sel = self.plan_tree.selection()
        if sel and sel[0] != self.selected_key:
            self.select_key(sel[0])

    def show_plan(self, key):
        r = self.plan_rows[key]
        link = self.bench.links.get(r["wcb"], r["port"], raw=True)
        text = f"{key} — {r['status']}\n\n{r['how']}\n\n{wiring.PAD_ORDER}\n"
        if link:
            text += f"\nFound: {link}\n"
        text += f"\nUnlocks {len(r['unlocks'])} test(s): " + (", ".join(r["unlocks"]) or "—")
        self.plan_text.delete("1.0", "end")
        self.plan_text.insert("1.0", text)

    def selected_link(self):
        if not self.selected_key:
            self.status("Select a wire first")
            return None
        w, port = runner.parse_key(self.selected_key)
        link = self.bench.links.get(w, port, raw=True)
        if not link:
            self.status(f"{self.selected_key} has no wire")
        return link

    def verify_selected(self):
        link = self.selected_link()
        if link:
            self.submit(f"verify {link.key}", self.job_verify, link.wcb, link.port)

    def remove_selected(self):
        link = self.selected_link()
        if link:
            self.submit(f"remove {link.key}", self.job_remove_link, link.wcb, link.port)

    def toggle_tap_selected(self):
        if self.selected_key:
            w, port = runner.parse_key(self.selected_key)
            self.submit(f"tap {self.selected_key}", self.job_toggle_tap, w, port)

    def kind_key(self):
        label = self.dev_kind.get()
        return next((k for k, v in DEVICE_KINDS.items() if v[0] == label), "other")

    def kind_hint(self):
        hint = DEVICE_KINDS[self.kind_key()][1]
        return f"Which: {hint}" if hint else "Which: optional"

    def fill_device_form(self, key):
        d = self.bench.port_devices().get(key)
        if d:
            self.dev_kind.set(DEVICE_KINDS.get(d.get("kind"), DEVICE_KINDS["other"])[0])
            self.dev_detail.set(d.get("detail", ""))
        else:
            self.dev_detail.set("")
        self.dev_hint.set(self.kind_hint())

    def set_port_device(self):
        key = self.dev_port.get()
        if not key:
            self.status("Pick the WCB port the device is wired to")
            return
        self.submit(f"device on {key}", self.job_set_port_device, key, self.kind_key(), self.dev_detail.get().strip())

    def clear_port_device(self):
        key = self.dev_port.get()
        if not key:
            self.status("Pick the WCB port to clear")
            return
        self.submit(f"clear device on {key}", self.job_set_port_device, key, None, "")

    def connect_manual(self):
        key, pn, header = self.man_port.get(), self.man_probe.get(), self.man_header.get()
        if not (key and pn and header):
            self.status("Pick a WCB port, a probe and a header")
            return
        w, port = runner.parse_key(key)
        self.submit(f"wire {key} -> {pn} {header}", self.job_add_link, w, port, pn, header)

    # ------------------------------------------------------------------ Tests tab
    def build_tests(self):
        f = ttk.Frame(self.nb, padding=14)
        self.nb.add(f, text="  Tests  ")
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        # The run buttons carry the selection's expected time (update_estimates), so it is known before starting.
        self.run_sel_btn = ttk.Button(bar, text="Run selected", command=self.run_selected)
        self.run_sel_btn.pack(side="left")
        self.run_all_btn = ttk.Button(bar, text="Run everything", command=lambda: self.run_tests(
            list(runner.REGISTRY), "everything", selectors=[]))
        self.run_all_btn.pack(side="left", padx=6)
        ttk.Button(bar, text="Run what failed", command=self.run_failed).pack(side="left")
        ttk.Button(bar, text="Open last report", command=self.open_report).pack(side="left", padx=6)
        self.summary_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.summary_var, font=BOLD).pack(side="right")
        ttk.Label(f, foreground=GREY, text="Double-click a test to run just that one. Select an area row to run the whole area. "
                                           "Tests whose wire or device is missing are skipped and say what they need. "
                                           "Expected: the median of recent runs, ~ an estimate, ? not known yet.").pack(anchor="w", pady=(6, 0))
        self.build_optins(f)

        pane = ttk.Panedwindow(f, orient="vertical") if hasattr(ttk, "Panedwindow") else ttk.Frame(f)
        pane = ttk.Frame(f)
        pane.pack(fill="both", expand=True, pady=(8, 0))
        self.tests_pane = pane
        # The paused-run banner: packed above the table only while a run is on offer (render_resume_bar).
        self.resume_bar = ttk.Frame(f, padding=(0, 8, 0, 0))
        self.resume_var = tk.StringVar(value="")
        ttk.Label(self.resume_bar, textvariable=self.resume_var, font=BOLD, foreground=AMBER, wraplength=1200,
                  justify="left").pack(side="top", anchor="w")
        rb = ttk.Frame(self.resume_bar)
        rb.pack(side="top", anchor="w", pady=(4, 0))
        self.rb_resume = ttk.Button(rb, text="Resume",
                                    command=lambda: self.resume(self.resumable["path"]) if self.resumable else None)
        self.rb_resume.pack(side="left")
        self.other_runs_btn = ttk.Button(rb, text="Other runs…", command=self.open_picker)
        self.other_runs_btn.pack(side="left", padx=6)
        self.rb_report = ttk.Button(rb, text="Open its report", command=lambda: self.open_run_report(self.resumable))
        self.rb_report.pack(side="left")
        self.rb_discard = ttk.Button(rb, text="Discard", command=lambda: self.discard(self.resumable))
        self.rb_discard.pack(side="left", padx=6)
        self.resume_bar_shown = False
        self.test_tree = ttk.Treeview(pane, columns=("status", "time", "expected", "needs", "title"),
                                      show="tree headings")
        for col, text, width in (("#0", "Test", 230), ("status", "Result", 80), ("time", "Time", 70),
                                 ("expected", "Expected", 100), ("needs", "Needs", 240), ("title", "What it checks", 520)):
            self.test_tree.heading(col, text=text)
            self.test_tree.column(col, width=width, anchor="w")
        for s, col in STATUS_COLOR.items():
            self.test_tree.tag_configure(s, foreground=col)
        self.test_tree.tag_configure("missing", foreground=GREY)
        self.test_tree.tag_configure("optoff", foreground=GREY)
        ys = ttk.Scrollbar(pane, orient="vertical", command=self.test_tree.yview)
        self.test_tree.configure(yscrollcommand=ys.set)
        self.test_tree.pack(side="top", fill="both", expand=True)
        self.test_tree.bind("<Double-1>", self.on_test_double)
        self.test_tree.bind("<<TreeviewSelect>>", self.on_test_select)
        self.test_detail = tk.Text(f, height=10, wrap="word", font=MONO, relief="flat",
                                   bg=THEME["card"], fg=THEME["fg"], insertbackground=THEME["fg"])
        self.test_detail.pack(fill="x", pady=(8, 0))

    def build_optins(self, parent):
        """One checkbox per hil/optin.py entry: its title, what it adds to a full run, and what it does. Ticking one
        writes bench.json "opt_in" on the worker (job_set_opt_in); all of them are disabled while a run is going."""
        box = ttk.Frame(parent, padding=(0, 8, 0, 0))
        box.pack(fill="x")
        head = ttk.Frame(box)
        head.pack(fill="x")
        # Folded by default: open, the panel takes most of the table's height at 1320x860 and pushes the detail pane
        # off the tab. The header line below still says how many are on and what they add; Show unfolds it.
        self.optin_open = False
        self.optin_toggle_btn = ttk.Button(head, text="Show", width=6, command=self.toggle_optins)
        self.optin_toggle_btn.pack(side="left")
        self.optin_head_var = tk.StringVar(value="Opt-in tests")
        ttk.Label(head, textvariable=self.optin_head_var, font=BOLD).pack(side="left", padx=(8, 0))
        self.optin_body = ttk.Frame(box, padding=(4, 4, 0, 0))
        self.optin_checks, self.optin_cost_vars = {}, {}
        on = optin.enabled(self.bench.cfg)
        # Two columns, each entry a checkbox and its cost over a small grey line saying what it does: ten full-width
        # rows took a third of the tab from the test table.
        for c in (0, 1):
            self.optin_body.columnconfigure(c, weight=1, uniform="optin")
        for i, (key, o) in enumerate(optin.OPT_INS.items()):
            cell = ttk.Frame(self.optin_body, padding=(0, 0, 16, 4))
            cell.grid(row=i // 2, column=i % 2, sticky="nw")
            var = tk.BooleanVar(value=key in on)
            self.optin_vars[key] = var
            cb = ttk.Checkbutton(cell, text=o["title"], variable=var, command=lambda k=key: self.on_optin_toggle(k))
            cb.grid(row=0, column=0, sticky="w")
            self.optin_checks[key] = cb
            cv = tk.StringVar(value="")
            self.optin_cost_vars[key] = cv
            ttk.Label(cell, textvariable=cv, foreground=BLUE).grid(row=0, column=1, sticky="w", padx=(10, 0))
            ttk.Label(cell, text=f"{key}: {o['what']}", foreground=GREY, font=("Segoe UI", 9), wraplength=600,
                      justify="left").grid(row=1, column=0, columnspan=2, sticky="w", padx=(22, 0))

    def toggle_optins(self):
        self.optin_open = not self.optin_open
        if self.optin_open:
            self.optin_body.pack(fill="x")
        else:
            self.optin_body.pack_forget()
        self.optin_toggle_btn.configure(text="Hide" if self.optin_open else "Show")

    def on_optin_toggle(self, key):
        var = self.optin_vars[key]
        if self.run_active:        # the checkboxes are disabled during a run; this covers a click that raced it
            var.set(key in optin.enabled(self.bench.cfg))
            self.status("Opt-ins cannot change while a run is going")
            return
        self.submit(f"opt-in {key} {'on' if var.get() else 'off'}", self.job_set_opt_in, key, bool(var.get()))

    # ---- expected durations (Tk thread; the history itself is read by _durations_job)
    def load_durations(self):
        """Re-read past runs' durations on a thread of its own; the pump applies them."""
        if self.durations_busy:
            self.durations_again = True
            return
        self.durations_busy, self.durations_again = True, False
        threading.Thread(target=self._durations_job, name="durations", daemon=True).start()

    def _durations_job(self):
        try:
            history = durations.load(RESULTS)
        except Exception:  # noqa: BLE001 - no history is no worse than before
            history = None
        self.emit("durations", history)

    def opted_off(self, t):
        key = t.get("opt_in")
        return bool(key) and key not in optin.enabled(self.bench.cfg)

    def expected_of(self, t):
        return durations.expected(t, self.history, self.bench.cfg)

    def expected_text(self, t):
        return "opt-in off" if self.opted_off(t) else durations.fmt_expected(*self.expected_of(t))

    def cost(self, t):
        """(seconds this test adds to a run, kind): 0 for one the runner skips at once (a missing wire or device, or
        its opt-in off); (None, None) when nothing is known."""
        if self.skip_now.get(t["id"]) or self.opted_off(t):
            return 0.0, "skip"
        return self.expected_of(t)

    def estimate_tests(self, tests):
        """(seconds, any unknown) for running `tests` now."""
        total, unknown = 0.0, False
        for t in tests:
            sec, _ = self.cost(t)
            if sec is None:
                unknown = True
            else:
                total += sec
        return total, unknown

    @staticmethod
    def fmt_total(sec, unknown):
        return "~" + durations.fmt_span(sec) + ("+?" if unknown else "")

    def update_estimates(self):
        """Every expected time on the Tests tab: each row's Expected cell and grey mark, the area sums, the opt-in
        costs and the run buttons. The running row's cell belongs to tick()."""
        running = self.test_now[0] if self.test_now else None
        areas = {}
        for t in runner.REGISTRY:
            tid = t["id"]
            areas.setdefault(runner.area_of(t), []).append(t)
            if not self.test_tree.exists(tid) or tid == running:
                continue
            self.test_tree.set(tid, "expected", self.expected_text(t))
            if not self.results.get(tid, ("",))[0]:
                tag = "missing" if self.skip_now.get(tid) else "optoff" if self.opted_off(t) else ""
                self.test_tree.item(tid, tags=(tag,))
        for a, tests in areas.items():
            if self.test_tree.exists(f"area:{a}"):
                sec, unknown = self.estimate_tests(tests)
                self.test_tree.set(f"area:{a}", "expected", self.fmt_total(sec, unknown) if sec or unknown else "—")
        on = optin.enabled(self.bench.cfg)
        adds = 0.0
        for key, cv in self.optin_cost_vars.items():
            tests = [t for t in runner.REGISTRY if t.get("opt_in") == key]
            sec, est = 0.0, False
            for t in tests:
                s, kind = self.expected_of(t)
                sec += s or 0.0
                est = est or kind != "history"
            cv.set(f"+{'~' if est else ''}{checkpoint.fmt_duration(round(sec))}, {len(tests)} test"
                   f"{'' if len(tests) == 1 else 's'}")
            if key in on:
                adds += sum(self.cost(t)[0] or 0.0 for t in tests)   # as Run everything counts it: 0 for a missing wire
            if self.optin_vars[key].get() != (key in on):
                self.optin_vars[key].set(key in on)
        self.optin_head_var.set(f"Opt-in tests: {len(on)} of {len(optin.OPT_INS)} on"
                                + (f", adding ~{durations.fmt_span(adds)} to Run everything" if on else ""))
        sec, unknown = self.estimate_tests(runner.REGISTRY)
        self.run_all_btn.configure(text=f"Run everything ({self.fmt_total(sec, unknown)})")
        self.update_selection_estimate()

    def update_selection_estimate(self):
        tests = []
        for iid in self.test_tree.selection():
            tests += [t for t in self.tests_for(iid) if t not in tests]
        if tests:
            sec, unknown = self.estimate_tests(tests)
            self.run_sel_btn.configure(text=f"Run selected ({self.fmt_total(sec, unknown)})")
        else:
            self.run_sel_btn.configure(text="Run selected")

    def refresh_tests(self):
        open_areas = {i for i in self.test_tree.get_children() if self.test_tree.item(i, "open")}
        first = not self.test_tree.get_children()
        self.test_tree.delete(*self.test_tree.get_children())
        areas = []
        for t in runner.REGISTRY:
            a = runner.area_of(t)
            if a not in areas:
                areas.append(a)
                self.test_tree.insert("", "end", iid=f"area:{a}", text=a, open=first or f"area:{a}" in open_areas)
        counts = {}
        self.skip_now = {}      # test id -> the runner would skip it for a missing wire or device (for the estimates)
        for t in runner.REGISTRY:
            miss = runner.missing(self.bench, t)
            self.skip_now[t["id"]] = bool(miss)
            needs = ", ".join(t["needs"] + runner.links_of(t)) or "—"
            status, detail, dur = self.results.get(t["id"], ("", "", None))
            tag = status or ("missing" if miss else "optoff" if self.opted_off(t) else "")
            if miss and not status:
                needs = "missing: " + ", ".join(miss)
            self.test_tree.insert(f"area:{runner.area_of(t)}", "end", iid=t["id"], text=t["id"],
                                  values=(status, f"{dur:.1f}s" if dur else "", self.expected_text(t), needs,
                                          t["title"]), tags=(tag,))
            if status:
                counts[status] = counts.get(status, 0) + 1
        self.summary_var.set("   ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        self.update_estimates()

    def tests_for(self, iid):
        if iid.startswith("area:"):
            a = iid[5:]
            return [t for t in runner.REGISTRY if runner.area_of(t) == a]
        return [t for t in runner.REGISTRY if t["id"] == iid]

    def run_selected(self):
        tests = []
        for iid in self.test_tree.selection():
            tests += [t for t in self.tests_for(iid) if t not in tests]
        if not tests:
            self.status("Select a test or an area first")
            return
        # an area row is '<area>.*' (area_of splits the id at its first '.'); a test row is its exact id
        sels = [iid[5:] + ".*" if iid.startswith("area:") else iid for iid in self.test_tree.selection()]
        self.run_tests(tests, f"{len(tests)} test(s)", selectors=sels)

    def run_failed(self):
        tests = [t for t in runner.REGISTRY if self.results.get(t["id"], ("",))[0] in ("FAIL", "ERROR")]
        if not tests:
            self.status("Nothing has failed")
            return
        self.run_tests(tests, "failed tests")

    def on_test_double(self, event):
        iid = self.test_tree.identify_row(event.y)
        if iid and not iid.startswith("area:"):
            self.run_tests(self.tests_for(iid), iid)

    def on_test_select(self, _e):
        if _e is not None:
            self.update_selection_estimate()
        sel = self.test_tree.selection()
        if not sel or sel[0].startswith("area:"):
            return
        t = self.tests_for(sel[0])[0]
        status, detail, dur = self.results.get(t["id"], ("not run", "", None))
        miss = runner.missing(self.bench, t)
        text = f"{t['id']} — {t['title']}\nResult: {status}" + (f" in {dur:.1f}s" if dur else "") + "\n"
        sec, kind = self.expected_of(t)
        h = self.history.get(t["id"])
        exp = durations.fmt_expected(sec, kind)
        if kind == "history" and h:
            exp += f" (median of the last {h[1]} real result{'' if h[1] == 1 else 's'})"
        elif kind == "estimate":
            follows = t.get("opt_in") and optin.OPT_INS[t["opt_in"]].get("minutes_key")
            exp += (f" (estimated from bench.json {follows})" if follows else " (an estimate: no real result yet)")
        else:
            exp += " (no real result yet)"
        text += f"Expected: {exp}\n"
        if t.get("opt_in"):
            key = t["opt_in"]
            text += (f"Opt-in: {key} ({'on' if key in optin.enabled(self.bench.cfg) else 'OFF - it is skipped'}) — "
                     f"{optin.OPT_INS[key]['what']}\n")
        text += f"Devices: {', '.join(t['needs']) or '—'}   Wires: {', '.join(runner.links_of(t)) or '— (uses whatever is wired)'}\n"
        if miss:
            text += f"Missing: {', '.join(miss)}\n"
        if detail:
            text += "\n" + detail
        self.test_detail.delete("1.0", "end")
        self.test_detail.insert("1.0", text)

    def run_tests(self, tests, label, confirm=True, selectors=None):
        """selectors: the globs the selection came from ([] = everything), recorded in the checkpoint so a resumed
        run's report can list tests added to the suite since; None when no glob describes it (failed tests, one
        double-clicked test)."""
        # One run at a time. A double-click on a test row lands here, and while a run was going it used to queue a
        # SECOND run - starting when the first ended, in its own results folder - and blank those tests' results in
        # the table on the spot, so a stray double-click quietly threw away what you were reading.
        if self.run_active:
            self.status(f"A run is already going - press Stop first (ignored: run {label})")
            return
        if self.resumable and confirm and not messagebox.askyesno(
                "WCB Bench", f"A paused run exists ({self.resumable['name']}: {self.resumable['done']} of "
                             f"{self.resumable['total']} done).\n\nStart a new run anyway? The paused one stays "
                             f"resumable.", parent=self.root):
            return
        # Cleared here, on the Tk thread: the run_active guard above means no earlier run can still be going, so this
        # cannot cancel a Stop or Pause meant for it.
        self.control.clear()
        for t in tests:
            self.results.pop(t["id"], None)
        self.run_ids = [t["id"] for t in tests]
        self.refresh_tests()
        self.run_active = True
        self.update_run_buttons()
        self.submit(f"run {label}", self.job_run, tests, label, selectors)

    def resume(self, path):
        if self.run_active:
            self.status("A run is already going - press Stop or Pause first")
            return
        self.control.clear()
        self.run_active = True
        self.update_run_buttons()
        self.submit(f"resume {os.path.basename(path)}", self.job_resume, path)

    def stop(self):
        self.control.request_stop()     # replaces a pending pause: the last press wins
        self.status("Stopping after the current test…")
        self.update_run_buttons()

    def on_pause_button(self):
        if not self.run_active:
            return
        if self.control.pausing:
            self.control.cancel_pause()
            # A pause asked for by closing the window takes the close with it: otherwise the run's end, hours later,
            # would still close the window unasked.
            closing = self.close_after_pause
            self.close_after_pause = False
            self.status("Pause cancelled - the run carries on" + (" and the window stays open" if closing else ""))
        else:
            self.control.request_pause("user")   # replaces a pending stop: the last press wins
            now = ""
            if self.test_now:
                now = f" (now {self.test_now[0]}, {fmt_duration(time.monotonic() - self.test_now[1])})"
            self.status(f"Pausing after the current test…{now}")
        self.update_run_buttons()

    def update_run_buttons(self):
        if self.run_active:
            self.pause_btn.configure(text="Cancel pause" if self.control.pausing else "Pause after this test")
            self.pause_btn.state(["!disabled"])
        else:
            self.pause_btn.configure(text="Pause after this test")
            self.pause_btn.state(["disabled"])
        # The runner reads bench.json opt_in before each test, so a tick mid-run would change the rest of the run.
        for cb in getattr(self, "optin_checks", {}).values():
            cb.state(["disabled"] if self.run_active else ["!disabled"])

    # ------------------------------------------------------------------ paused runs (Tk thread)
    def scan_resumable(self, prefer=None):
        """Read the results folder for runs that can be resumed. Files only; no port is touched."""
        try:
            runs = checkpoint.find_resumable(RESULTS, include_stopped=True)
        except Exception:  # noqa: BLE001 - a bad folder must not stop the GUI
            runs = []
        self.resumable_all = runs
        offered = [s for s in runs if s["state"] != "stopped"]
        pick = next((s for s in offered if prefer and os.path.normcase(s["path"]) == os.path.normcase(prefer)), None)
        self.resumable = pick or (offered[0] if offered else None)
        self.render_resume_bar()
        self.update_run_buttons()

    def render_resume_bar(self):
        # Shown for stopped runs too: the picker is the GUI's only way to one, and it lives in this bar. With only
        # stopped runs the bar is a one-line notice and just Other runs… is enabled - the banner never offers a
        # stopped run itself.
        if (self.resumable or self.resumable_all) and not self.run_active:
            if self.resumable:
                self.resume_var.set(checkpoint.summary_text(self.resumable))
                others = len(self.resumable_all) - 1
            else:
                self.resume_var.set(f"{len(self.resumable_all)} stopped run(s) can be resumed - pick one from "
                                    f"Other runs…")
                others = len(self.resumable_all)
            for b in (self.rb_resume, self.rb_report, self.rb_discard):
                b.state(["!disabled"] if self.resumable else ["disabled"])
            self.other_runs_btn.configure(text=f"Other runs… ({others})")
            if others > 0:
                self.other_runs_btn.state(["!disabled"])
            else:
                self.other_runs_btn.state(["disabled"])
            if not self.resume_bar_shown:
                self.resume_bar.pack(fill="x", before=self.tests_pane)
                self.resume_bar_shown = True
        elif self.resume_bar_shown:
            self.resume_bar.pack_forget()
            self.resume_bar_shown = False

    def open_run_report(self, s):
        if not s:
            return
        path = os.path.join(s["path"], "report.md")
        if os.path.exists(path):
            os.startfile(path)
        else:
            self.status(f"{s['name']} has no report yet")

    def discard(self, s, parent=None):
        if not s:
            return
        if not messagebox.askyesno("WCB Bench", f"Discard {s['name']} ({s['done']} of {s['total']} done)?\n\nIts "
                                                f"results and report stay in the folder; it is just no longer offered "
                                                f"for resuming.", parent=parent or self.root):
            return
        try:
            checkpoint.Checkpoint.load(s["path"]).abandon()
            self.status(f"Discarded {s['name']}")
        except (CheckpointError, RunBusy) as e:
            self.status(f"Could not discard {s['name']}: {e}")
        self.scan_resumable()

    def open_picker(self):
        runs = self.resumable_all
        if not runs:
            return
        win = tk.Toplevel(self.root)
        win.title("Paused runs")
        win.configure(bg=THEME["bg"])
        win.transient(self.root)
        tree = ttk.Treeview(win, columns=("what", "done", "when", "why", "state"), show="tree headings", height=10)
        for col, text, width in (("#0", "Run", 170), ("what", "What", 160), ("done", "Done", 80),
                                 ("when", "When", 150), ("why", "Why", 380), ("state", "State", 90)):
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor="w")
        tree.tag_configure("stopped", foreground=GREY)
        for s in runs:
            state = "interrupted" if s["interrupted"] else s["state"]
            why = (f"during {s['in_flight']}" if s["interrupted"] and s.get("in_flight") else
                   s.get("reason_text") or "")
            if s.get("last_resume_error"):
                why += " · blocked: " + s["last_resume_error"].splitlines()[0]
            tree.insert("", "end", iid=s["path"], text=s["name"], values=(
                s.get("label") or "", f"{s['done']} / {s['total']}", (s.get("updated") or "")[:16].replace("T", " "),
                why, state), tags=(s["state"],))
        tree.pack(fill="both", expand=True, padx=12, pady=12)
        bar = ttk.Frame(win, padding=(12, 0, 12, 12))
        bar.pack(fill="x")

        def chosen():
            sel = tree.selection()
            return next((s for s in runs if sel and s["path"] == sel[0]), None)

        def do_resume():
            s = chosen()
            if s:
                win.destroy()
                self.resume(s["path"])

        def do_discard():
            s = chosen()
            if s:
                self.discard(s, parent=win)
                win.destroy()

        ttk.Button(bar, text="Resume", command=do_resume).pack(side="left")
        ttk.Button(bar, text="Discard", command=do_discard).pack(side="left", padx=6)
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")
        _dark_titlebar(win)
        win.grab_set()

    # ------------------------------------------------------------------ Log tab
    def build_log(self):
        f = ttk.Frame(self.nb, padding=14)
        self.nb.add(f, text="  Log  ")
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Button(bar, text="Clear", command=lambda: self.log_text.delete("1.0", "end")).pack(side="left")
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Follow", variable=self.follow).pack(side="left", padx=8)
        frame = ttk.Frame(f)
        frame.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text = tk.Text(frame, wrap="none", font=MONO, bg=THEME["logbg"], fg=THEME["logfg"],
                                insertbackground=THEME["logfg"])
        ys = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)

    # ------------------------------------------------------------------ jobs (worker thread)
    def job_find_devices(self, paused_path=None):
        b = self.bench
        b.close()
        before = {n: d.get("port") for n, d in b.cfg["devices"].items()}
        ports = [p for p in list_ports.comports() if p.vid in ESP_VIDS]
        self.emit("status", f"Scanning {len(ports)} ESP32 port(s)…")
        found = {}
        for p in ports:
            info = identify_port(p.device)
            b.note(f"scan {p.device}: {info}")
            if info:
                found[p.device] = info
        devices = b.cfg["devices"]
        report = []
        probes_seen = []
        for port, info in found.items():
            kind = info["kind"]
            if kind == "wcb":
                report.append(self.assign_device(port, info))
            elif kind in ("navicore", "sbus"):
                for name, d in devices.items():
                    if d["kind"] == kind:
                        d["port"] = port
                        report.append(f"{name} = {port}")
                        break
            elif kind == "probe":
                probes_seen.append((port, info))
        probe_names = [n for n, d in devices.items() if d["kind"] == "probe"]
        unassigned = []
        for port, info in probes_seen:
            match = next((n for n in probe_names if devices[n].get("mac") == info["mac"]), None)
            if match:
                devices[match]["port"] = port
                report.append(f"{match} = {port} (mac {info['mac']}, v{info['version']})")
            else:
                unassigned.append((port, info))
        for port, info in unassigned:
            free = next((n for n in probe_names if "mac" not in devices[n]), None)
            if free:
                devices[free]["port"] = port
                devices[free]["mac"] = info["mac"]
                report.append(f"{free} = {port} (mac {info['mac']}, v{info['version']})")
            else:
                report.append(f"{port}: extra probe (mac {info['mac']}) — add it to bench.json to use it")
        for port, info in found.items():
            if info["kind"] == "error":
                report.append(f"{port}: could not open — {info['error']}")
        if not self.paused_serial_ok(before, paused_path):
            b.reload_config()
            self.emit("devices_changed")
            self.emit("status", "Find devices: nothing saved")
            return
        b.save_config()
        self.emit("devices_changed")
        self.emit("status", "Found: " + ("; ".join(report) if report else "no boards answered"))

    def job_set_port(self, name, port):
        self.bench.close_device(name)
        self.bench.cfg["devices"][name]["port"] = port
        self.bench.save_config()
        self.emit("devices_changed")

    def job_check_device(self, name):
        b = self.bench
        b.close_device(name)
        kind = b.cfg["devices"][name]["kind"]
        try:
            if kind == "probe":
                m = b.probe(name).hello()
                text = f"wcb_probe v{m.group(1)}   mac {m.group(2)}"
            elif kind == "wcb":
                w = WCB(b.dev(name))
                ver = w.version()
                num = next((re.search(r"Board (\d+)", l).group(1) for l in w.run("?config")
                            if "Configuration: Wireless Communication Board" in l), "?")
                text = f"WCB{num}   firmware {ver}"
            elif kind == "navicore":
                text = f"NaviCore   firmware {NaviCore(b.dev(name)).ping()}"
            else:
                d = b.dev(name)
                deadline = time.monotonic() + 60
                while True:
                    m = d.mark()
                    d.send('{"t":"ping"}')
                    try:
                        got = d.expect(r'^\{"t":"pong".*"fwver":"([^"]+)"', timeout=3, since=m)
                        break
                    except ExpectTimeout:
                        if time.monotonic() > deadline:
                            raise
                text = f"SBUS controller   firmware {got.group(1)}"
            self.emit("identity", name, "✔ " + text)
        except Exception as e:
            self.emit("identity", name, "✘ " + str(e).splitlines()[0])

    def job_close_ports(self):
        self.bench.close()
        self.emit("status", "All ports closed — the Arduino IDE / Wizard can use them now")

    def job_discover(self):
        self.emit("status", "Auto-detecting wires — each WCB port transmits in turn…")
        links = self.bench.links.discover(log=lambda s: self.emit("log", s))
        self.emit("links_changed")
        good = sum(1 for l in links if l.verified)
        self.emit("status", f"Auto-detect: {len(links)} wire(s) found, {good} verified")

    def device_note(self, wcb, port):
        dev = self.bench.links.device_on(wcb, port)
        return (f"not verified — {describe_device(dev)} is on this port and bench.json has no port_stimulus for it, "
                f"so nothing is sent to check the wire, and tests never use it")

    def job_add_link(self, wcb, port, probe, header):
        tap = any(t["wcb"] == wcb and t["port"] == port for t in self.bench.cfg.get("taps", []))
        link = self.bench.links.set_link(wcb, port, probe, header, swap=False, tap=tap)
        if self.bench.links.device_only(wcb, port):
            self.emit("links_changed")
            self.emit("status", f"{link.key} -> {probe} {header}: added as a listen-only wire, " + self.device_note(wcb, port))
            return
        ok = self.bench.links.verify(link)
        self.emit("links_changed")
        self.emit("status", f"{link.key} -> {probe} {header}: " +
                  ("verified" + (" (straight-through cable)" if link.swap else "") if ok else
                   "NOT verified — check GND and the TX/RX wires, and that the WCB port is enabled"))

    def job_verify(self, wcb, port):
        link = self.bench.links.get(wcb, port, raw=True)
        if self.bench.links.device_only(wcb, port):
            self.emit("status", f"{link.key}: " + self.device_note(wcb, port))
            return
        ok = self.bench.links.verify(link)
        self.emit("links_changed")
        self.emit("status", f"{link.key}: {'verified' if ok else 'NOT verified'}")

    def job_remove_link(self, wcb, port):
        self.bench.links.remove_link(wcb, port)
        self.emit("links_changed")
        self.emit("status", f"W{wcb}{port}: wire removed")

    def job_toggle_tap(self, wcb, port):
        taps = self.bench.cfg.setdefault("taps", [])
        existing = next((t for t in taps if t["wcb"] == wcb and t["port"] == port), None)
        if existing:
            taps.remove(existing)
        else:
            taps.append({"wcb": wcb, "port": port, "why": "Marked in the GUI: a real device shares this line."})
        self.bench.save_config()
        link = self.bench.links.get(wcb, port, raw=True)
        if link:
            self.bench.links.set_link(wcb, port, link.probe_name, link.header, swap=link.swap, tap=not existing)
            self.bench.links.get(wcb, port, raw=True).verified = link.verified
            self.bench.links.save()
        dev = self.bench.links.device_on(wcb, port)
        self.emit("links_changed")
        self.emit("status", f"W{wcb}{port}: listen-only tap {'off' if existing else 'on'}" +
                  (f" — {describe_device(dev)} is set on this port, so a probe wire there stays listen-only"
                   if existing and dev else ""))

    def job_set_port_device(self, key, kind, detail):
        b = self.bench
        devices = b.cfg.setdefault("port_devices", {})
        w, port = runner.parse_key(key)
        if kind:
            devices[key] = {"kind": kind, "detail": detail} if detail else {"kind": kind}
            text = f"{key}: {describe_device(devices[key])} — the tool will never make this port transmit"
        elif devices.pop(key, None) is None:
            self.emit("status", f"{key} has no device set")
            return
        else:
            text = f"{key}: device cleared"
        if not devices:
            b.cfg.pop("port_devices", None)
        b.save_config()
        link = b.links.get(w, port, raw=True)
        if link:   # a probe wire there follows the device: listen-only while a device owns the line
            tapped = any(t["wcb"] == w and t["port"] == port for t in b.cfg.get("taps", []))
            tap = tapped or bool(kind)
            # it keeps its verified mark only if its role did not change and it can still be checked
            keep = link.verified and tap == link.tap and not b.links.device_only(w, port)
            b.links.set_link(w, port, link.probe_name, link.header, swap=link.swap, tap=tap)
            b.links.get(w, port, raw=True).verified = keep
            b.links.save()
        self.emit("links_changed")
        self.emit("status", text)

    def job_set_opt_in(self, key, on):
        """Tick or untick one opt-in in bench.json "opt_in" (Bench.save_config: atomic, every other key kept in its
        order). On the worker, like every bench.json write, so it never interleaves with Find devices' save."""
        b = self.bench
        # A run live in another process (a second GUI, run.py in a terminal) recorded opt_in in its checkpoint's bench
        # canon; changing it under that run turns its automatic outage recovery into a pause (resume.check_bench).
        # This process's own run cannot get here: run_active disables the boxes and the worker is serial.
        try:
            names = sorted(os.listdir(RESULTS), reverse=True)
        except OSError:
            names = []
        live = next((n for n in names if checkpoint.RunLock.held(os.path.join(RESULTS, n))), None)
        if live:
            self.emit("optin_changed")   # update_estimates puts the checkbox back to match cfg
            self.emit("status", f"A run is live in another window or terminal (results/{live}): opt-ins not changed")
            return
        # Tick onto the file, not the copy loaded at start: bench.json may have been edited since (opt_in and
        # soak_minutes are edited by hand), and saving the stale dict would silently undo that. Only opt_in, and the
        # soak length its estimate reads, are copied back into memory: open ports and devices are left alone.
        try:
            with open(b.bench_path, encoding="utf-8") as f:
                fresh = json.load(f)
            if not isinstance(fresh, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as e:
            self.emit("optin_changed")
            self.emit("status", f"Opt-in {key}: bench.json could not be read ({e}), nothing saved")
            return
        optin.set_enabled(fresh, key, on)
        checkpoint.atomic_write_json(b.bench_path, fresh)            # Bench.save_config's own path; key order kept
        b.cfg["opt_in"] = fresh["opt_in"]                            # memory changes only once the write succeeded
        for o in optin.OPT_INS.values():
            mk = o.get("minutes_key")
            if mk and mk in fresh:
                b.cfg[mk] = fresh[mk]
            elif mk:
                b.cfg.pop(mk, None)
        self.emit("optin_changed")
        self.emit("status", f"Opt-in {key} {'on' if on else 'off'}: {optin.OPT_INS[key]['title']} "
                            f"{'will run' if on else 'is skipped'} (bench.json saved)")

    def job_run(self, tests, label="tests", selectors=None):
        b = self.bench
        self.run_active = True    # also set in run_tests(); the pump clears it on run_done / run_paused
        ckpt = runner.start_run(b, tests, label, selectors=selectors)
        self.current_ckpt = ckpt
        self.emit("run_start", len(tests), 0, 0.0, ckpt.name, os.path.join(ckpt.out_dir, "report.md"))
        self._drive(ckpt, resuming=False)

    def job_resume(self, path):
        self.run_active = True
        try:
            ckpt = checkpoint.Checkpoint.load(path)
        except CheckpointError:
            self.emit("resume_blocked", path, f"The paused run's folder is gone (results/{os.path.basename(path)}) — "
                                              f"nothing to resume")
            return
        if ckpt.state in ("done", "abandoned"):
            self.emit("resume_blocked", path, f"{ckpt.name} is {ckpt.state} — nothing to resume")
            return
        self.current_ckpt = ckpt
        self.emit("run_start", ckpt.total, ckpt.done_count, ckpt.active_s, ckpt.name,
                  os.path.join(ckpt.out_dir, "report.md"))
        self.emit("run_restore", list(ckpt.data["tests"]),
                  [(r["id"], r["status"], r["detail"], r["dur"]) for r in ckpt.data["results"]])
        self._drive(ckpt, resuming=True)

    def _drive(self, ckpt, resuming):
        """continue_run for a new or resumed run, and one terminal event for the pump: run_done, run_paused or
        resume_blocked. A harness error is also reported as run_paused (the run IS paused) before the Worker's own
        error line."""
        report = os.path.join(ckpt.out_dir, "report.md")
        try:
            runner.continue_run(
                self.bench, ckpt, resuming=resuming, ask=self.ask_user,
                on_start=lambda t: self.emit("test_start", t["id"]),
                on_result=lambda t, s, d, dur: self.emit("test_result", t["id"], s, d, dur),
                on_requeue=lambda t, d: self.emit("test_requeued", t["id"], d),
                should_stop=self.control.should_stop, should_pause=self.control.should_pause,
                on_bench_changed=lambda: self.emit("devices_changed"))
        except ResumeAborted as e:
            self.emit("run_paused", ckpt.out_dir, f"Resume cancelled ({e}) — {ckpt.name} is still paused")
            return
        except ResumeBlocked as e:
            self.emit("resume_blocked", ckpt.out_dir, str(e))
            return
        except RunBusy:
            self.emit("resume_blocked", ckpt.out_dir, f"{ckpt.name} is being resumed by another process")
            return
        except Exception as e:
            self.emit("run_paused", ckpt.out_dir, f"Paused by a harness error: {type(e).__name__}: "
                                                  f"{(str(e).splitlines() or [''])[0]} — fix it, restart, then Resume")
            raise
        finally:
            self.current_ckpt = None
        if ckpt.state == "paused":
            self.emit("run_paused", ckpt.out_dir, ckpt.pause_message())
            return
        counts = {}
        for r in ckpt.data["results"]:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        self.emit("run_done", report, (", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "nothing ran")
                  + f" in {fmt_duration(ckpt.active_s)}")

    def ask_user(self, title, text, default=False):
        """Worker thread: ask on the Tk thread and wait for the answer. No (False) once the window is closing."""
        if self.closing:
            return False
        ev, box = threading.Event(), {"yes": False}
        self.emit("ask", title, text, ev, box, default)
        while not ev.wait(0.25):
            if self.closing:
                return False
        return box["yes"]

    def _answer(self, title, text, ev, box, default):
        """Tk thread, from after_idle - never from inside pump(): a modal dialog runs a nested event loop, and inside
        pump() that would start a second pump chain."""
        try:
            box["yes"] = messagebox.askyesno(title, text, default="yes" if default else "no", parent=self.root)
        finally:
            ev.set()

    # ------------------------------------------------------------------ event pump (UI thread)
    def pump(self):
        log_lines = []
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "log":
                    log_lines.append(data[0])
                elif kind == "status":
                    self.status(data[0])
                elif kind == "busy":
                    self.job = (data[0], time.monotonic())
                    self.run_total = self.run_done_n = 0
                    self.test_now = None
                elif kind == "idle":
                    if self.job and self.job[0] == data[0]:
                        self.last_job = (data[0], time.monotonic() - self.job[1])
                        self.job = None
                        self.test_now = None
                elif kind == "run_start":
                    self.run_total, self.run_done_n, self.run_base = data[0], data[1], data[2]
                    self.last_report = data[4]
                    self.render_resume_bar()
                    self.update_run_buttons()
                elif kind == "run_restore":
                    ids, rows = data
                    self.run_ids = list(ids)
                    for tid in ids:
                        self.results.pop(tid, None)
                    for tid, status, detail, dur in rows:
                        self.results[tid] = (status, detail, dur)
                    self.refresh_tests()
                elif kind == "test_requeued":
                    tid, detail = data
                    self.test_now = None
                    self.results[tid] = ("RETRY", detail, None)
                    self.set_test_row(tid)
                elif kind == "ask":
                    self.root.after_idle(self._answer, *data)
                elif kind == "run_paused":
                    self.run_active = False
                    self.control.clear()
                    self.test_now = None
                    self.scan_resumable(prefer=data[0])
                    self.refresh_tests()
                    self.load_durations()
                    self.status(data[1])
                    if self.close_after_pause:
                        self.root.after(200, self._close_now)
                elif kind == "resume_blocked":
                    self.run_active = False
                    self.control.clear()
                    self.scan_resumable(prefer=data[0])
                    self.status("Resume blocked — " + data[1].splitlines()[0])
                    if self.close_after_pause:
                        self.root.after(200, self._close_now)
                    else:
                        msg = data[1]
                        self.root.after_idle(lambda m=msg: messagebox.showwarning(
                            "Resume blocked", m + "\n\nThe run is still paused.", parent=self.root))
                elif kind == "identity":
                    self.identities[data[0]] = data[1]
                    if data[0] in self.dev_status:
                        self.dev_status[data[0]].set(data[1])
                elif kind == "devices_changed":
                    self.render_devices()
                    self.refresh_wiring()
                    self.refresh_tests()
                elif kind == "links_changed":
                    self.refresh_wiring()
                    self.refresh_tests()
                elif kind == "optin_changed":
                    self.update_estimates()
                    sel = self.test_tree.selection()
                    if sel and not sel[0].startswith("area:"):
                        self.on_test_select(None)
                elif kind == "durations":
                    self.durations_busy = False
                    if data[0] is not None:
                        self.history = data[0]
                        self.update_estimates()
                    if self.durations_again:
                        self.load_durations()
                elif kind == "test_start":
                    self.test_now = (data[0], time.monotonic())
                    self.results[data[0]] = ("RUNNING", "", None)
                    self.set_test_row(data[0])
                elif kind == "test_result":
                    tid, status, detail, dur = data
                    self.run_done_n += 1
                    self.test_now = None
                    self.results[tid] = (status, detail, dur)
                    self.set_test_row(tid)
                elif kind == "run_done":
                    self.run_active = False
                    self.control.clear()
                    self.last_report = data[0]
                    self.refresh_tests()
                    self.scan_resumable()
                    self.load_durations()
                    self.status(f"Run finished: {data[1]}")
                    if self.close_after_pause:
                        self.root.after(200, self._close_now)
                elif kind == "error":
                    self.status(data[0].splitlines()[0])
                    log_lines.append(data[0])
                    self.scan_resumable()
                    if self.close_after_pause:
                        self.root.after(200, self._close_now)
        except queue.Empty:
            pass
        if log_lines:
            self.log_text.insert("end", "\n".join(log_lines) + "\n")
            excess = int(self.log_text.index("end-1c").split(".")[0]) - 20000
            if excess > 0:
                self.log_text.delete("1.0", f"{excess}.0")
            if self.follow.get():
                self.log_text.see("end")
        self.root.after(100, self.pump)

    def tick(self):
        """The elapsed timer above the tabs: the running job, and during a run how far it has got."""
        now = time.monotonic()
        if self.job:
            name, start = self.job
            # a resumed run's timer continues from its active time before this segment (run_base is 0 for a new run)
            base = self.run_base if self.run_total else 0.0
            text = f"Elapsed {fmt_duration(base + now - start)}   {name}"
            if self.run_total:
                text += f"   ·   {self.run_done_n} of {self.run_total} done"
                if self.test_now:
                    text += f"   ·   now {self.test_now[0]} ({fmt_duration(now - self.test_now[1])})"
                    self.show_running_expected(now)
            extra = []
            left = self.time_left(now)
            if left is not None:
                sec, unknown = left
                extra.append(f"{self.fmt_total(sec, unknown)} left, done ≈ "
                             f"{time.strftime('%H:%M', time.localtime(time.time() + sec))}")
            if self.run_total and self.control.pausing:
                extra.append("pausing after this test")
            self.timer_var.set(text)
            self.eta_var.set("   ·   ".join(extra))
        elif self.resumable:
            s = self.resumable
            try:
                since = time.time() - checkpoint.iso_ts(s.get("updated"))
            except Exception:  # noqa: BLE001
                since = 0
            self.timer_var.set(f"{'Interrupted' if s['interrupted'] else 'Paused'}: {s['name']}   ·   {s['done']} of "
                               f"{s['total']} done   ·   active {fmt_duration(s.get('active_s') or 0)}   ·   "
                               f"paused for {fmt_duration(max(0, since))}")
            self.eta_var.set("")
        elif self.last_job:
            self.timer_var.set(f"Last: {self.last_job[0]} took {fmt_duration(self.last_job[1])}")
            self.eta_var.set("")
        else:
            self.eta_var.set("")
        self.root.after(500, self.tick)

    def show_running_expected(self, now):
        """The running test's Expected cell: 'elapsed / expected', live."""
        tid, start = self.test_now
        t = self.test_by_id(tid)
        if t and self.test_tree.exists(tid):
            self.test_tree.set(tid, "expected",
                               f"{fmt_duration(now - start)} / {durations.fmt_expected(*self.expected_of(t))}")

    def time_left(self, now):
        """(seconds, any unknown) still to run in this run: the expected time of every test without a result yet (0
        for one the runner will skip at once), less the running test's elapsed time. None outside a run."""
        if not self.run_ids:
            return None
        if self.control.pausing or self.control.should_stop():
            # Pause/Stop after this test: the run ends when the running test does, whatever is left in run_ids.
            # Cancel pause clears the request, and the full estimate comes back on the next tick.
            if not self.test_now:
                return 0.0, False
            t = self.test_by_id(self.test_now[0])
            sec = self.cost(t)[0] if t is not None else None
            if sec is None:
                return 0.0, True
            return max(0.0, sec - (now - self.test_now[1])), False
        total, unknown = 0.0, False
        running = self.test_now[0] if self.test_now else None
        for tid in self.run_ids:
            status = self.results.get(tid, ("",))[0]
            if status not in ("", "RUNNING", "RETRY"):
                continue
            t = self.test_by_id(tid)
            if t is None:
                continue
            sec, _ = self.cost(t)
            if sec is None:
                unknown = True
                continue
            if tid == running:
                sec = max(0.0, sec - (now - self.test_now[1]))
            total += sec
        return total, unknown

    def test_by_id(self, tid):
        if getattr(self, "_by_id_n", None) != len(runner.REGISTRY):
            self._by_id = {t["id"]: t for t in runner.REGISTRY}
            self._by_id_n = len(runner.REGISTRY)
        return self._by_id.get(tid)

    def set_test_row(self, tid):
        if not self.test_tree.exists(tid):
            return
        status, detail, dur = self.results[tid]
        vals = list(self.test_tree.item(tid, "values"))
        vals[0] = status
        vals[1] = f"{dur:.1f}s" if dur else ""
        t = self.test_by_id(tid)
        if t is not None and status != "RUNNING":
            vals[2] = self.expected_text(t)
        self.test_tree.item(tid, values=vals, tags=(status,))
        if status == "RUNNING":
            self.test_tree.see(tid)
        sel = self.test_tree.selection()
        if sel and sel[0] == tid:
            self.on_test_select(None)
        counts = {}
        for s, _, _ in self.results.values():
            if s not in ("RUNNING", "RETRY"):
                counts[s] = counts.get(s, 0) + 1
        self.summary_var.set("   ".join(f"{k} {v}" for k, v in sorted(counts.items())))

    # ------------------------------------------------------------------ misc
    def open_results(self):
        os.makedirs(RESULTS, exist_ok=True)
        os.startfile(RESULTS)

    def open_report(self):
        if self.last_report and os.path.exists(self.last_report):
            os.startfile(self.last_report)
        else:
            self.status("No report yet — run some tests first")

    def on_close(self):
        if self.run_active:
            ans = messagebox.askyesnocancel(
                "WCB Bench", "A run is going.\n\n"
                             "Yes — pause after the current test, then close. The run stays resumable.\n"
                             "No — close now. The current test is cut off; the run stays resumable and that test "
                             "runs again first.\n"
                             "Cancel — keep running.", parent=self.root)
            if ans is None:
                return
            if not self.run_active:
                # The run ended (run_done / run_paused / resume_blocked / a crash) while the dialog was open - its
                # nested event loop keeps the pump going. No terminal event is left to act on the answer, so close now.
                self._close_now()
                return
            if ans:
                self.close_after_pause = True
                self.control.request_pause("closing")
                self.update_run_buttons()
                self.status("Pausing after the current test, then closing…")
                return
            # Close now. Not bench.close() and not Stop: closing ports under the worker could turn the test in flight
            # into a recorded FAIL, and a stop landing now would make the run final. The checkpoint is frozen so
            # nothing more is written; the daemon worker dies with the process and the OS frees the run lock, so the
            # run is left interrupted - resumable, with that test re-run first.
            self.closing = True
            ckpt = self.current_ckpt
            if ckpt is not None:
                ckpt.freeze()
            # A Wizard test's node and Chrome are in their own process group and outlive this process: end them, or
            # they keep W1's COM port and keep driving the board. After the freeze, so the FAIL their exit causes in
            # the worker is never recorded and the test still re-runs first.
            try:
                wizard.kill_live()
            except Exception:
                pass
            self.root.destroy()
            return
        self._close_now()

    def _close_now(self):
        self.closing = True
        self.control.request_stop()
        try:
            self.bench.close()
        except Exception:
            pass
        self.root.destroy()


def main():
    global THEME, GREEN, AMBER, RED, BLUE, GREY, STATUS_COLOR
    if "--light" in sys.argv[1:]:
        THEME = LIGHT
        GREEN, AMBER, RED, BLUE, GREY = (THEME[k] for k in ("green", "amber", "red", "blue", "grey"))
        STATUS_COLOR.update({"PASS": GREEN, "FAIL": RED, "ERROR": RED, "SKIP": GREY, "RUNNING": BLUE, "RETRY": AMBER})
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    app = App(root)
    _dark_titlebar(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    # gui.py --run <glob> [<glob> ...] opens with those tests queued and starts them, so a run launched
    # from a script (or by Claude) can be watched live instead of only read afterwards. Same globs as run.py.
    # It never stops to ask about a paused run (confirm=False); that run stays resumable.
    if "--run" in sys.argv[1:]:
        sels = sys.argv[sys.argv.index("--run") + 1:]
        tests = runner.select(sels)
        if tests:
            root.after(1500, lambda: app.run_tests(tests, f"{len(tests)} test(s) from --run", confirm=False,
                                                   selectors=sels))
    # gui.py --resume [<run>] opens and resumes the newest paused or interrupted run, or the one named.
    elif "--resume" in sys.argv[1:]:
        rest = sys.argv[sys.argv.index("--resume") + 1:]
        name = rest[0] if rest and not rest[0].startswith("--") else None

        def start_resume():
            if name:
                app.resume(name if os.path.isdir(name) else os.path.join(RESULTS, name))
            elif app.resumable:
                app.resume(app.resumable["path"])
            else:
                app.status("--resume: no paused or interrupted run to resume")
        root.after(1500, start_resume)
    root.mainloop()


if __name__ == "__main__":
    main()
