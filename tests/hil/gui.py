"""WCB Bench — the GUI for the hardware-in-the-loop tests.

    python tests/hil/gui.py

Devices  find which COM port is which board, and check each one
Wiring   what to connect; auto-detect the wires, or click a WCB port then a probe header
Tests    run one test (double-click), an area, everything, or only what failed
Log      every serial line in and out

All bench work runs on one worker thread, in order, so two actions never fight over a COM
port. The window only reads results from a queue.
"""
import ctypes
import importlib
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

from hil import runner, wiring  # noqa: E402
from hil.navicore import NaviCore  # noqa: E402
from hil.runner import DEVICE_KINDS, describe_device, fmt_duration  # noqa: E402
from hil.probe import HEADERS  # noqa: E402
from hil.serialdev import ExpectTimeout, SerialDevice  # noqa: E402
from hil.wcb import WCB  # noqa: E402
import suites  # noqa: E402

for _mod in pkgutil.iter_modules(suites.__path__):
    importlib.import_module(f"suites.{_mod.name}")

BENCH_PATH = os.path.join(HERE, "bench.json")
RESULTS = os.path.join(HERE, "results")
ESP_VIDS = {0x10C4, 0x1A86, 0x303A, 0x0403, 0x067B}   # CP210x, CH34x/CH9102, Espressif native, FTDI, PL2303

FONT = ("Segoe UI", 10)
BOLD = ("Segoe UI", 10, "bold")
MONO = ("Consolas", 9)
GREEN, AMBER, RED, BLUE, GREY = "#1a7f37", "#bf8700", "#cf222e", "#0969da", "#8c959f"
STATUS_COLOR = {"PASS": GREEN, "FAIL": RED, "ERROR": RED, "SKIP": GREY, "RUNNING": BLUE}


# ---------------------------------------------------------------------------- identification
def identify_port(port):
    """What is on this COM port? -> dict(kind=..., ...) or None. Never resets a board (DTR/RTS low).

    115200 first: a WCB and NaviCore both answer ?VERSION (NaviCore's version starts with 'v');
    the SBUS controller treats '?' as its status key and prints '[SBUS] Mode:'. Only then 921600,
    where a probe answers HELLO. No free text is sent anywhere: an unprefixed line on a WCB would
    be broadcast to the whole mesh."""
    try:
        with SerialDevice("scan", port, 115200) as d:
            time.sleep(0.3)
            m = d.mark()
            d.send("?VERSION")
            try:
                got = d.expect(r"Software Version: (\S+)|\[SBUS\] (Mode:|Ready)", timeout=2.0, since=m)
            except ExpectTimeout:
                got = None
            if got:
                if got.group(0).startswith("[SBUS]"):
                    return {"kind": "sbus"}
                ver = got.group(1)
                if ver.startswith("v"):
                    return {"kind": "navicore", "version": ver}
                num = None
                for line in WCB(d).run("?config"):
                    hit = re.search(r"Configuration: Wireless Communication Board (\d+)", line)
                    if hit:
                        num = int(hit.group(1))
                return {"kind": "wcb", "version": ver, "wcb": num}
        with SerialDevice("scan", port, 921600) as d:
            time.sleep(0.2)
            m = d.mark()
            d.send("HELLO")
            try:
                got = d.expect(r"^HELLO wcb_probe (\S+) mac=(\S+)", timeout=2.0, since=m)
                return {"kind": "probe", "version": got.group(1), "mac": got.group(2)}
            except ExpectTimeout:
                return None
    except Exception as e:   # port busy, vanished, access denied
        return {"kind": "error", "error": str(e).splitlines()[0]}


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
                self.app.emit("error", f"{name} failed:\n{traceback.format_exc()}")
            finally:
                self.app.emit("idle", name)


# ---------------------------------------------------------------------------- the app
class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.stop_flag = threading.Event()
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
        self.worker = Worker(self)
        self.worker.start()
        self.build_ui()
        self.root.after(100, self.pump)
        self.root.after(500, self.tick)
        self.refresh_wiring()
        self.refresh_tests()

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
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure(".", font=FONT)
        style.configure("Treeview", rowheight=24)

        top = ttk.Frame(self.root, padding=(12, 8))
        top.pack(fill="x")
        ttk.Button(top, text="Open results folder", command=self.open_results).pack(side="right")
        ttk.Button(top, text="Close all ports",
                   command=lambda: self.submit("close ports", self.job_close_ports)).pack(side="right", padx=6)
        ttk.Button(top, text="Stop after this test", command=self.stop).pack(side="right")
        self.timer_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.timer_var, font=BOLD, foreground=BLUE).pack(side="top", anchor="w")
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
        ttk.Button(bar, text="Find devices", command=lambda: self.submit("find devices", self.job_find_devices)).pack(side="left")
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
        ports = sorted(p.device for p in list_ports.comports())
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
            cb = ttk.Combobox(self.dev_grid, textvariable=var, values=ports, width=10)
            cb.grid(row=r, column=2, sticky="w", padx=8)
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
        self.submit(f"add device on {port}", self.job_add_device, port)

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

    def job_add_device(self, port):
        b = self.bench
        for name, d in list(b.cfg["devices"].items()):
            if d.get("port") == port:
                b.close_device(name)
        info = identify_port(port)
        if not info:
            self.emit("status", f"{port}: nothing answered — is it an ESP32 board running WCB, probe, NaviCore or SBUS firmware?")
            return
        text = self.assign_device(port, info)
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
        self.canvas = tk.Canvas(left, width=600, height=600, bg="white", highlightthickness=1, highlightbackground="#d0d7de")
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

        self.plan_text = tk.Text(right, height=8, width=48, wrap="word", font=FONT, relief="flat", bg="#f6f8fa")
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
            c.create_rectangle(x, y, x + 210, y + box_h, fill="#f6f8fa", outline="#d0d7de", width=2)
            c.create_text(x + 12, y + 16, anchor="w", font=BOLD, text=f"WCB{w}  " + ("(USB)" if w in usb else "(mesh)"))
            labels = self.port_labels(w)
            for k, port in enumerate(HEADERS):
                py = y + 46 + k * 36
                px = x + 210
                key = f"W{w}{port}"
                hl = self.pending_port == key or self.selected_key == key
                c.create_oval(px - 9, py - 9, px + 9, py + 9, fill=BLUE if hl else "white", outline="#57606a",
                              width=2, tags=("wport", key))
                dev = self.bench.port_devices().get(key)
                if dev:
                    c.create_text(x + 14, py, anchor="w", text=f"{port}  {describe_device(dev)}"[:28], fill=BLUE)
                else:
                    c.create_text(x + 14, py, anchor="w", text=f"{port}  {labels.get(port, '')}"[:26])
                self.xy[("W", key)] = (px, py)
        for j, pn in enumerate(probes):
            x, y = 460, 16 + j * (box_h + pitch)
            c.create_rectangle(x, y, x + 210, y + box_h, fill="#fff8f0", outline="#d0d7de", width=2)
            port = self.bench.cfg["devices"][pn].get("port", "?")
            c.create_text(x + 24, y + 16, anchor="w", font=BOLD, text=f"{pn}  ({port})")
            for k, header in enumerate(HEADERS):
                py = y + 46 + k * 36
                px = x
                c.create_oval(px - 9, py - 9, px + 9, py + 9, fill="white", outline="#57606a", width=2,
                              tags=("pport", f"{pn}:{header}"))
                c.create_text(x + 24, py, anchor="w", text=f"header {header}")
                self.xy[("P", f"{pn}:{header}")] = (px, py)
        for r in wiring.plan(self.bench):
            if r["status"] == "to do" and r["probe"] in probes:
                a, b = self.xy[("W", r["key"])], self.xy[("P", f"{r['probe']}:{r['header']}")]
                c.create_line(*a, *b, fill="#d0d7de", width=2, dash=(2, 4))
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
        ttk.Button(bar, text="Run selected", command=self.run_selected).pack(side="left")
        ttk.Button(bar, text="Run everything", command=lambda: self.run_tests(list(runner.REGISTRY), "everything")).pack(side="left", padx=6)
        ttk.Button(bar, text="Run what failed", command=self.run_failed).pack(side="left")
        ttk.Button(bar, text="Open last report", command=self.open_report).pack(side="left", padx=6)
        self.summary_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.summary_var, font=BOLD).pack(side="right")
        ttk.Label(f, foreground=GREY, text="Double-click a test to run just that one. Select an area row to run the whole area. "
                                           "Tests whose wire or device is missing are skipped and say what they need.").pack(anchor="w", pady=(6, 0))

        pane = ttk.Panedwindow(f, orient="vertical") if hasattr(ttk, "Panedwindow") else ttk.Frame(f)
        pane = ttk.Frame(f)
        pane.pack(fill="both", expand=True, pady=(8, 0))
        self.test_tree = ttk.Treeview(pane, columns=("status", "time", "needs", "title"), show="tree headings")
        for col, text, width in (("#0", "Test", 230), ("status", "Result", 80), ("time", "Time", 70),
                                 ("needs", "Needs", 260), ("title", "What it checks", 560)):
            self.test_tree.heading(col, text=text)
            self.test_tree.column(col, width=width, anchor="w")
        for s, col in STATUS_COLOR.items():
            self.test_tree.tag_configure(s, foreground=col)
        self.test_tree.tag_configure("missing", foreground=GREY)
        ys = ttk.Scrollbar(pane, orient="vertical", command=self.test_tree.yview)
        self.test_tree.configure(yscrollcommand=ys.set)
        self.test_tree.pack(side="top", fill="both", expand=True)
        self.test_tree.bind("<Double-1>", self.on_test_double)
        self.test_tree.bind("<<TreeviewSelect>>", self.on_test_select)
        self.test_detail = tk.Text(f, height=10, wrap="word", font=MONO, relief="flat", bg="#f6f8fa")
        self.test_detail.pack(fill="x", pady=(8, 0))

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
        for t in runner.REGISTRY:
            miss = runner.missing(self.bench, t)
            needs = ", ".join(t["needs"] + runner.links_of(t)) or "—"
            status, detail, dur = self.results.get(t["id"], ("", "", None))
            tag = status or ("missing" if miss else "")
            if miss and not status:
                needs = "missing: " + ", ".join(miss)
            self.test_tree.insert(f"area:{runner.area_of(t)}", "end", iid=t["id"], text=t["id"],
                                  values=(status, f"{dur:.1f}s" if dur else "", needs, t["title"]), tags=(tag,))
            if status:
                counts[status] = counts.get(status, 0) + 1
        self.summary_var.set("   ".join(f"{k} {v}" for k, v in sorted(counts.items())))

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
        self.run_tests(tests, f"{len(tests)} test(s)")

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
        sel = self.test_tree.selection()
        if not sel or sel[0].startswith("area:"):
            return
        t = self.tests_for(sel[0])[0]
        status, detail, dur = self.results.get(t["id"], ("not run", "", None))
        miss = runner.missing(self.bench, t)
        text = f"{t['id']} — {t['title']}\nResult: {status}" + (f" in {dur:.1f}s" if dur else "") + "\n"
        text += f"Devices: {', '.join(t['needs']) or '—'}   Wires: {', '.join(runner.links_of(t)) or '— (uses whatever is wired)'}\n"
        if miss:
            text += f"Missing: {', '.join(miss)}\n"
        if detail:
            text += "\n" + detail
        self.test_detail.delete("1.0", "end")
        self.test_detail.insert("1.0", text)

    def run_tests(self, tests, label):
        # The stop flag is cleared when the run starts on the worker (job_run), not here: clearing it while an
        # earlier run is still going would cancel the Stop pressed for that run.
        for t in tests:
            self.results.pop(t["id"], None)
        self.refresh_tests()
        self.submit(f"run {label}", self.job_run, tests)

    def stop(self):
        self.stop_flag.set()
        self.status("Stopping after the current test…")

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
        self.log_text = tk.Text(frame, wrap="none", font=MONO, bg="#0d1117", fg="#c9d1d9", insertbackground="white")
        ys = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)

    # ------------------------------------------------------------------ jobs (worker thread)
    def job_find_devices(self):
        b = self.bench
        b.close()
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

    def job_run(self, tests):
        b = self.bench
        self.stop_flag.clear()
        out_dir = b.new_session()
        self.emit("run_start", len(tests))
        start = time.monotonic()
        results = runner.run_tests(
            b, tests,
            on_start=lambda t: self.emit("test_start", t["id"]),
            on_result=lambda t, s, d, dur: self.emit("test_result", t["id"], s, d, dur),
            should_stop=self.stop_flag.is_set)
        elapsed = time.monotonic() - start
        runner.write_report(out_dir, results, elapsed)
        counts = {}
        for _, s, _, _ in results:
            counts[s] = counts.get(s, 0) + 1
        self.emit("run_done", os.path.join(out_dir, "report.md"),
                  (", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "nothing ran") + f" in {fmt_duration(elapsed)}")

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
                    self.run_total, self.run_done_n = data[0], 0
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
                    self.last_report = data[0]
                    self.refresh_tests()
                    self.status(f"Run finished: {data[1]}")
                elif kind == "error":
                    self.status(data[0].splitlines()[0])
                    log_lines.append(data[0])
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
            text = f"Elapsed {fmt_duration(now - start)}   {name}"
            if self.run_total:
                text += f"   ·   {self.run_done_n} of {self.run_total} done"
                if self.test_now:
                    text += f"   ·   now {self.test_now[0]} ({fmt_duration(now - self.test_now[1])})"
            self.timer_var.set(text)
        elif self.last_job:
            self.timer_var.set(f"Last: {self.last_job[0]} took {fmt_duration(self.last_job[1])}")
        self.root.after(500, self.tick)

    def set_test_row(self, tid):
        if not self.test_tree.exists(tid):
            return
        status, detail, dur = self.results[tid]
        vals = list(self.test_tree.item(tid, "values"))
        vals[0] = status
        vals[1] = f"{dur:.1f}s" if dur else ""
        self.test_tree.item(tid, values=vals, tags=(status,))
        if status == "RUNNING":
            self.test_tree.see(tid)
        sel = self.test_tree.selection()
        if sel and sel[0] == tid:
            self.on_test_select(None)
        counts = {}
        for s, _, _ in self.results.values():
            if s != "RUNNING":
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
        self.stop_flag.set()
        try:
            self.bench.close()
        except Exception:
            pass
        self.root.destroy()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
