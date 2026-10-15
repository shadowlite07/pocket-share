#!/usr/bin/env python3
"""
pocket-share - Windows app
==========================

A small window that starts and stops the pocket-share server and shows the
addresses you need. Standard library only (tkinter ships with Python).

    python pocket_share.py

The server itself lives in teleport_share.py and is not duplicated here - this
file only drives it.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))

import teleport_share as ts  # noqa: E402  (needs the path set first)

APP_TITLE = "pocket-share"


class LogBridge:
    """
    Send the server's console output into the window.

    Without this the app would look broken: uploads would happen silently and
    you would have no idea whether anything reached the phone.
    """

    def __init__(self, sink: queue.Queue) -> None:
        self.sink = sink

    def write(self, text: str) -> None:
        for line in text.splitlines():
            if line.strip():
                self.sink.put(line)

    def flush(self) -> None:  # tkinter has nothing to flush to
        pass


class PocketShareApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.minsize(560, 620)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.server = None
        self.thread = None
        self.local_ip = ""
        self.port = ts.DEFAULT_PORT
        self.lines: queue.Queue = queue.Queue()
        self._saved_stdout = None
        self._closing = False

        self._build_style()
        self._build_ui()
        self._pump_log()

        self.root.after(120, self.start)

    # ---------------- layout ----------------

    def _build_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Head.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Sub.TLabel", foreground="#666666")
        style.configure("Url.TLabel", font=("Consolas", 12))
        style.configure("Pill.TLabel", font=("Segoe UI", 9, "bold"),
                        foreground="#1f7a3f")
        style.configure("PillOff.TLabel", font=("Segoe UI", 9, "bold"),
                        foreground="#c0392b")
        style.configure("Log.TTextview", font=("Consolas", 9))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        pad = ttk.Frame(self.root, padding=18)
        pad.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        pad.columnconfigure(0, weight=1)

        header = ttk.Frame(pad)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_TITLE,
                  style="Head.TLabel").grid(row=0, column=0, sticky="w")
        self.status = ttk.Label(header, text="starting...", style="PillOff.TLabel")
        self.status.grid(row=0, column=1, sticky="e")

        self.start_btn = ttk.Button(pad, text="Start", style="Accent.TButton",
                                    command=self.start)
        self.start_btn.grid(row=1, column=0, sticky="w", pady=(0, 4))
        self.stop_btn = ttk.Button(pad, text="Stop", command=self.stop,
                                   state="disabled")
        self.stop_btn.grid(row=1, column=0, sticky="w")

        # ---- addresses ----
        self.local_url = self._url_row(pad, 2, "ON THIS LAPTOP",
                                       "http://localhost:")
        self.phone_url = self._url_row(pad, 3, "ON THE PHONE (same Wi-Fi)", "")

        # ---- details ----
        details = ttk.Frame(pad)
        details.grid(row=4, column=0, sticky="ew", pady=(16, 0))
        details.columnconfigure(1, weight=1)

        self.pin_var = tk.StringVar()
        ttk.Label(details, text="PIN", style="Sub.TLabel").grid(
            row=0, column=0, sticky="w")
        pin_box = ttk.Frame(details)
        pin_box.grid(row=0, column=1, sticky="w")
        self.pin_label = ttk.Label(pin_box, textvariable=self.pin_var,
                                   style="Url.TLabel")
        self.pin_label.grid(row=0, column=0, sticky="w")
        ttk.Button(pin_box, text="Show", width=6,
                   command=self.toggle_pin).grid(row=0, column=1, sticky="w",
                                                 padx=(8, 0))

        ttk.Label(details, text="Files", style="Sub.TLabel").grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        self.folder_var = tk.StringVar()
        ttk.Label(details, textvariable=self.folder_var,
                  style="Sub.TLabel").grid(row=1, column=1, sticky="w",
                                           pady=(6, 0))
        ttk.Button(details, text="Open", width=6,
                   command=self.open_folder).grid(row=1, column=2, sticky="e",
                                                  pady=(6, 0), padx=(8, 0))

        # ---- log ----
        ttk.Label(pad, text="Activity", style="Sub.TLabel").grid(
            row=5, column=0, sticky="w", pady=(16, 6))
        log_frame = ttk.Frame(pad)
        log_frame.grid(row=6, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        pad.rowconfigure(6, weight=1)

        self.log = tk.Text(log_frame, height=12, wrap="none", state="disabled",
                           bg="#fbfbfc", relief="flat", padx=8, pady=6)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, orient="vertical",
                               command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

    def _url_row(self, parent, row: int, caption: str, url: str) -> ttk.Label:
        box = ttk.Frame(parent)
        box.grid(row=row, column=0, sticky="ew", pady=6)
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text=caption, style="Sub.TLabel").grid(
            row=0, column=0, sticky="w")
        label = ttk.Label(box, text=url, style="Url.TLabel")
        label.grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Button(box, text="Open", width=6,
                   command=lambda: self.open_page(label.cget("text"))
                   ).grid(row=1, column=1, sticky="e", padx=(8, 0))
        ttk.Button(box, text="Copy", width=6,
                   command=lambda: self.copy(label.cget("text"))
                   ).grid(row=1, column=2, sticky="e")
        return label

    # ---------------- server control ----------------

    def start(self) -> None:
        if self.server:
            return
        info = ts.load_settings()
        self.pin_hidden = "•" * len(ts.PIN) if ts.PIN else "(switched off)"
        self.pin_showing = False
        self.pin_var.set(self.pin_hidden)
        self.folder_var.set(str(ts.DATA_DIR))

        bridge = LogBridge(self.lines)
        # Capture the real console only once. After a Stop/Start cycle sys.stdout
        # is already the previous bridge, and saving that would mean the bridge
        # was restored on exit instead of the console.
        if self._saved_stdout is None:
            self._saved_stdout = sys.stdout
        sys.stdout = bridge
        try:
            server, detected_ip, _store, _stale = ts.build_server(
                ts.BIND_ADDRESS, ts.DEFAULT_PORT)
        except OSError as exc:
            # Nothing is running, so give the console back and say so plainly.
            sys.stdout = self._saved_stdout
            self._set_status("could not start", running=False)
            self.lines.put(f"ERROR: {exc}")
            self.start_btn.config(state="normal")
            return

        self.server = server
        self.local_ip = detected_ip
        self.port = server.server_address[1]

        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()

        self.local_url.config(text=f"http://localhost:{self.port}")
        if detected_ip:
            self.phone_url.config(text=f"http://{detected_ip}:{self.port}")
        else:
            self.phone_url.config(text="could not detect - try --host")

        self._set_status("running", running=True)
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.lines.put(f"Started on port {self.port}  (PIN from {info['source']})")

    def stop(self) -> None:
        """Stop button handler - runs on the main thread, so it may touch widgets."""
        if not self.server:
            return
        self.stop_btn.config(state="disabled")
        self._set_status("stopping", running=False)
        self.stop_server()
        self._set_status("stopped", running=False)
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.local_url.config(text=f"http://localhost:{self.port}")
        self.phone_url.config(text="")

    def stop_server(self) -> None:
        """
        Shut the server down without touching any widget.

        tkinter may only be driven from the thread that created the window, so
        closing the app has to do this inline rather than on a worker thread.
        """
        if not self.server:
            return
        self.lines.put("Stopping...")
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()
            self.server = None

    def quit(self) -> None:
        self._closing = True
        self.stop_server()
        saved = getattr(self, "_saved_stdout", None)
        if saved is not None:
            sys.stdout = saved
        self.root.destroy()

    def _set_status(self, text: str, running: bool) -> None:
        self.status.config(text=text,
                           style="Pill.TLabel" if running else "PillOff.TLabel")

    # ---------------- small helpers ----------------

    def toggle_pin(self) -> None:
        self.pin_showing = not self.pin_showing
        self.pin_var.set(ts.PIN if self.pin_showing else self.pin_hidden)

    def copy(self, text: str) -> None:
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.lines.put(f"Copied {text}")

    def open_page(self, url: str) -> None:
        if not url or not url.startswith("http"):
            return
        webbrowser.open(url)

    def open_folder(self) -> None:
        try:
            path = ts.DATA_DIR
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)  # noqa: S606 - Windows-only, by design
        except (OSError, AttributeError) as exc:
            self.lines.put(f"Could not open the folder: {exc}")

    def _pump_log(self) -> None:
        """Move anything the server printed into the text box."""
        if getattr(self, "_closing", False):
            return
        try:
            while True:
                line = self.lines.get_nowait()
                self.log.config(state="normal")
                self.log.insert("end", line.rstrip() + "\n")
                self.log.see("end")
                self.log.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(250, self._pump_log)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    PocketShareApp().run()