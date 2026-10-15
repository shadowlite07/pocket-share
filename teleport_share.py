#!/usr/bin/env python3
"""
Teleport Share
==============
A tiny file + text bridge between a Windows laptop and a phone on the same
Wi-Fi (e.g. a phone hotspot). Standard library only - nothing to install.

    python teleport_share.py                 # start the server
    python teleport_share.py --port 9000     # use a different port
    python teleport_share.py --host 0.0.0.0  # bind address (see CONFIG)

Then open the page on the laptop and on the phone - same URL both places.

Step 2: file uploads. The browser sends one file per request as a raw body
(no multipart parsing), which keeps memory flat for files of any size:

    POST /api/upload?name=<url-encoded filename>[&from=phone|laptop]
    Content-Type: application/octet-stream
    Content-Length: <bytes>

'from' is optional - when omitted the sender is worked out from the client's
IP address (loopback / the laptop's own address means 'laptop', anything else
means 'phone'). Uploads land in SharedFiles/FromPhone or SharedFiles/FromLaptop.

Stop with Ctrl+C.
"""

from __future__ import annotations

import argparse
import datetime
import http.client
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ============================== CONFIG ==============================
# Everything you are likely to want to change lives in this block.

APP_NAME = "Teleport Share"
VERSION = "0.2.0"

# 0.0.0.0 = reachable from any network the laptop is on (needed for the phone).
# Set to the hotspot IP (e.g. "192.168.181.50") to only listen on that adapter.
BIND_ADDRESS = "0.0.0.0"

DEFAULT_PORT = 8765

# All shared data lives in here (created automatically on first run).
DATA_DIR = Path(__file__).resolve().parent / "SharedFiles"

# Reads/writes the socket in blocks of this size. Never loads a whole file
# into memory, so a 4 GB video costs the same RAM as a 4 KB text file.
CHUNK_SIZE = 256 * 1024

# Longest filename we will store; the extension is preserved when trimming.
MAX_NAME_LEN = 120

# Hard cap on index entries (history). Files on disk are never deleted.
MAX_INDEX_ENTRIES = 5000

# Endpoints the page polls. Kept out of the console log on purpose.
QUIET_PATHS = {"/api/recent"}

# Reserved-for-later settings, so the top of the file stays the single place
# to look. Unused by step 2; wired up in later steps.
# PIN = "123456"                        # step 7
# CLIPBOARD_AUTOCOPY_DEFAULT = True     # step 4
# HISTORY_LIMIT = 100                   # step 6
# POLL_SECONDS = 1.5                    # step 6

# ===================================================================


def fix_console_encoding() -> None:
    """Avoid UnicodeEncodeError if output is piped into a legacy-codepage file."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# ============================ FILENAMES ==============================
# A file name arriving from a browser is untrusted input. It has to survive
# Windows' rules and must never be able to point outside the data folder.

# Characters Windows refuses in a file name (plus control characters).
ILLEGAL_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')

# Names Windows reserves for old hardware, with or without an extension.
RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
    | {f"COM{i}" for i in ("\u00b9", "\u00b2", "\u00b3")}
    | {f"LPT{i}" for i in ("\u00b9", "\u00b2", "\u00b3")}
)


def sanitize_filename(raw: str) -> str:
    """
    Turn whatever the browser sent into a filename that is safe on Windows and
    cannot escape the data folder. Returns '' if nothing usable is left.
    """
    name = (raw or "").strip()

    # Keep only the final path segment: kills "../", "..\\", "C:\\fakepath\\"
    # and absolute paths in one move.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]

    name = ILLEGAL_CHARS.sub("_", name).strip()

    # Windows silently drops trailing dots and spaces, which would make the
    # stored name differ from what we report back.
    name = name.rstrip(" .")
    if not name:
        return ""

    stem, dot, ext = name.rpartition(".")
    if not stem:  # no extension at all (README, .hidden, ...)
        stem, dot, ext = name, "", ""
    elif len(ext) > 16:
        # Something like "report.2024.final.draft" - not a real extension.
        stem, dot, ext = name, "", ""

    # A reserved device name is still reserved with any extension ("CON.txt"),
    # and Windows refuses to create it at all.
    if stem.split(".", 1)[0].upper() in RESERVED_NAMES:
        stem = f"_{stem}"

    name = f"{stem}{dot}{ext}" if dot else stem

    if len(name) > MAX_NAME_LEN:
        tail = f"{dot}{ext}" if dot else ""
        stem = (stem[: max(1, MAX_NAME_LEN - len(tail))].rstrip(" .")) or "file"
        name = f"{stem}{tail}"

    return name


def split_name(name: str) -> tuple[str, str, str]:
    """'photo (2).jpg' -> ('photo (2)', '.', 'jpg'); no extension -> (n,'','')."""
    stem, dot, ext = name.rpartition(".")
    if not stem or len(ext) > 16:
        return name, "", ""
    return stem, dot, ext


def stamp_compact(ts: float | None = None) -> str:
    return datetime.datetime.fromtimestamp(
        ts if ts is not None else time.time()).strftime("%Y%m%d-%H%M%S")


def human_size(num_bytes: float) -> str:
    """Compact, readable size for the console log."""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < step or unit == "TB":
            precision = 0 if unit == "B" else 1
            return f"{num_bytes:.{precision}f} {unit}"
        num_bytes /= step
    return f"{num_bytes:.1f} TB"


# =============================== STORE ===============================
# Owns the folder layout, filename collisions and the history index.
# One lock guards everything that mutates disk state, so two uploads arriving
# at the same moment can never pick the same free name.


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.from_phone = root / "FromPhone"
        self.from_laptop = root / "FromLaptop"
        self.texts = root / "Texts"
        self.tmp = root / "_inbox_tmp"  # partial uploads; never listed
        self.index_path = root / ".teleport_index.json"
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._next_id = 1

    # ---- startup ----

    def start(self) -> None:
        for folder in (self.root, self.from_phone, self.from_laptop,
                       self.texts, self.tmp):
            folder.mkdir(parents=True, exist_ok=True)
        self._load_index()

    def discard_partials(self) -> int:
        """Delete half-finished uploads left behind by a crash or hard exit."""
        removed = 0
        for leftover in self.tmp.glob("*.part"):
            try:
                leftover.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    # ---- index ----

    def _load_index(self) -> None:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # missing or corrupt: start a fresh one rather than crash
        entries = data.get("entries")
        if not isinstance(entries, list):
            return
        self._entries = [e for e in entries if isinstance(e, dict)]
        highest = max((int(e.get("id", 0)) for e in self._entries), default=0)
        self._next_id = max(int(data.get("next_id", 0) or 0), highest + 1, 1)

    def _save_locked(self) -> None:
        """Atomic write: temp file then replace, so a crash cannot truncate it."""
        payload = {"next_id": self._next_id, "entries": self._entries}
        tmp = self.index_path.with_name(self.index_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.index_path)

    def _record_locked(self, entry: dict) -> dict:
        entry["id"] = self._next_id
        entry["ts"] = time.time()
        self._next_id += 1
        self._entries.append(entry)
        if len(self._entries) > MAX_INDEX_ENTRIES:
            del self._entries[: len(self._entries) - MAX_INDEX_ENTRIES]
        self._save_locked()
        return entry

    # ---- files ----

    def recent_files(self, limit: int = 12) -> list[dict]:
        """
        Newest-first list of files that arrived, for the receive confirmation
        on the page. Reads the index, so it only knows about uploads - files
        copied into the folder by hand show up once the listing endpoint
        walks the disk instead.
        """
        with self._lock:
            entries = [e for e in self._entries if e.get("type") == "file"]
            return list(reversed(entries[-limit:]))

    def new_partial(self) -> Path:
        """A private scratch path for one in-flight upload."""
        return self.tmp / f"{uuid.uuid4().hex}.part"

    def finalize_upload(self, partial: Path, sender: str,
                        raw_name: str, size: int) -> dict:
        """
        Move a finished upload into its folder under a name that is free.
        The whole check-then-rename runs under the lock, so two uploads of the
        same name can never overwrite each other.
        """
        folder = self.from_phone if sender == "phone" else self.from_laptop
        with self._lock:
            name = sanitize_filename(raw_name) or f"upload-{stamp_compact()}"
            final = unique_path(folder, name)
            # Safe: we hold the lock and just proved the name is free.
            os.replace(partial, final)
            return self._record_locked({
                "type": "file",
                "sender": sender,
                "original_name": raw_name,
                "stored_name": final.name,
                "rel_path": final.relative_to(self.root).as_posix(),
                "size": size,
            })


def unique_path(folder: Path, name: str) -> Path:
    """
    First free path for 'name' in 'folder': photo.jpg, photo (2).jpg, ...
    Callers must hold the Store lock. Never returns an existing file.
    """
    stem, dot, ext = split_name(name)
    candidate = folder / name
    counter = 1
    while candidate.exists():
        counter += 1
        if counter > 9999:  # pathological; practically unreachable
            return folder / f"{stem} {uuid.uuid4().hex[:8]}{dot}{ext}"
        candidate = folder / f"{stem} ({counter}){dot}{ext}"
    return candidate


def identify_sender(client_ip: str, local_ip: str | None) -> str:
    """
    Work out which device is talking to us.

    Loopback and the laptop's own LAN address are the laptop; any other address
    on the network is treated as the phone. ('from=' in the URL overrides this.)
    """
    ip = client_ip
    if ip.startswith("::ffff:"):  # IPv4 shown as IPv6-mapped
        ip = ip[7:]
    if ip in ("127.0.0.1", "::1", "localhost"):
        return "laptop"
    if local_ip and ip == local_ip:
        return "laptop"
    return "phone"


# ============================== NETWORK ==============================


def is_link_local(ip: str) -> bool:
    """169.254.x.x - an address Windows auto-assigns when there's no real network."""
    return ip.startswith("169.254.")


def detect_local_ip() -> str | None:
    """
    Find the address other devices can actually reach us on.

    Uses the UDP connect trick: 'connecting' a UDP socket picks the source
    address the OS would use for outbound traffic, and sends no packets.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 80))  # TEST-NET-1: reserved, never routed
        ip = probe.getsockname()[0]
        if not ip.startswith("127."):
            return ip
    except OSError:
        pass
    finally:
        probe.close()

    # Fallback: ask the resolver about our own hostname.
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


def list_candidate_ips() -> list[str]:
    """Every non-loopback IPv4 the machine knows about (diagnostics only)."""
    found: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return found
    for info in infos:
        ip = info[4][0]
        if not ip.startswith("127.") and ip not in found:
            found.append(ip)
    return found


def startup_banner(ip: str | None, port: int) -> None:
    line = "=" * 64
    print(line)
    print(f"  {APP_NAME} v{VERSION}")
    print(line)
    print()
    print("  On the LAPTOP:")
    print(f"      http://localhost:{port}")
    print()
    if ip:
        print("  On the PHONE (same Wi-Fi / hotspot):")
        print(f"      http://{ip}:{port}")
    else:
        print("  On the PHONE: local IP could not be detected automatically.")
        print("      Set LOCAL_IP_OVERRIDE at the top of this script, or pass --host.")
    print()
    if ip and is_link_local(ip):
        print("  ! WARNING: that address is link-local (169.254.x.x), which usually")
        print("    means the laptop has no real Wi-Fi connection right now.")
        others = [a for a in list_candidate_ips() if not is_link_local(a)]
        if others:
            print(f"    Other addresses found: {', '.join(others)}")
            print(f"    Try restarting with:  python {Path(sys.argv[0]).name} --host {others[0]}")
        print()

    others = [a for a in list_candidate_ips() if a != ip]
    if others:
        print(f"  Other local addresses (not advertised): {', '.join(others)}")
        print("    If the phone can't load the page, try one of those with --host.")
        print()

    print(f"  Data folder: {DATA_DIR}")
    print()
    print("  If the phone cannot connect, check in this order:")
    print("    1. Phone and laptop on the SAME network (disconnect laptop from")
    print("       other Wi-Fi first - Windows picks one adapter).")
    print("    2. Windows Firewall: allow this app on PRIVATE networks when asked.")
    print("       If the Wi-Fi profile is 'Public', run this in an Admin prompt:")
    print(f'         netsh advfirewall firewall add rule name="{APP_NAME}" dir=in '
          f"action=allow protocol=TCP localport={port} profile=any")
    print("    3. Some phone hotspots enable 'AP isolation', which blocks")
    print("       phone -> laptop traffic. Turn it off, or use a different hotspot.")
    print()
    print("  Press Ctrl+C to stop.")
    print(line)
    sys.stdout.flush()  # so the banner appears even when output is redirected


# ============================== SERVER ==============================


class UploadError(Exception):
    """A problem with an upload that the browser should be told about."""

    def __init__(self, status: int, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


def discard_file(path: Path) -> None:
    """Best-effort cleanup of a partial upload."""
    try:
        path.unlink()
    except OSError:
        pass


class ShareHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 keeps connections alive (needed for fast polling later).
    # It also means every response MUST carry an accurate Content-Length.
    protocol_version = "HTTP/1.1"
    server_version = f"{APP_NAME}/{VERSION}"
    sys_version = ""  # don't advertise the Python version

    # ---- request routing ----

    def do_GET(self) -> None:
        self._dispatch(send_body=True)

    def do_HEAD(self) -> None:
        self._dispatch(send_body=False)

    def do_POST(self) -> None:
        self._dispatch(send_body=True)

    def _dispatch(self, send_body: bool) -> None:
        try:
            self._route(send_body)
        except UploadError as exc:
            self.log_message("!! %d %s (%s)", exc.status, exc.message, exc.detail)
            try:
                self._send_json(
                    {"ok": False, "error": exc.message, "detail": exc.detail},
                    status=exc.status, send_body=send_body,
                )
            except OSError:
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser navigated away mid-response; not worth reporting
        except Exception as exc:  # noqa: BLE001 - never die on one bad request
            detail = f"{type(exc).__name__}: {exc}"
            self.log_message("!! 500 %s", detail)
            try:
                self._send_json(
                    {"ok": False, "error": "Internal server error", "detail": detail,
                     "path": urlparse(self.path).path},
                    status=500, send_body=send_body,
                )
            except OSError:
                pass

    def _route(self, send_body: bool) -> None:
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"

        if parsed.query:
            query = parse_qs(parsed.query, keep_blank_values=True)
        else:
            query = {}

        if route == "/":
            self._send_bytes(PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8",
                             send_body=send_body)
        elif route == "/api/info":
            self._send_json(self._info(), send_body=send_body)
        elif route == "/api/recent":
            limit = min(max(int((query.get("limit") or ["12"])[0] or 12), 1), 100)
            self._send_json({"ok": True,
                             "files": self.server.store.recent_files(limit)},
                            send_body=send_body)
        elif route == "/api/upload":
            self._handle_upload(query)
        elif route == "/favicon.ico":
            self._send_bytes(b"", "image/x-icon", send_body=False, status=204)
        else:
            self._send_json(
                {"ok": False, "error": "Not found", "path": route},
                status=404, send_body=send_body,
            )

    def _handle_upload(self, query: dict) -> None:
        """
        One file per request, sent as a raw binary body and streamed straight to
        disk in CHUNK_SIZE blocks. The file name arrives percent-encoded in the
        query string, so any Unicode name survives the trip.
        """
        raw_name = (query.get("name") or [""])[0]
        sender = (query.get("from") or [""])[0].lower()
        if sender not in ("phone", "laptop"):
            sender = identify_sender(self.client_address[0],
                                     getattr(self.server, "local_ip", None))

        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise UploadError(411, "Missing Content-Length",
                              "Send the file as a raw body with a Content-Length "
                              "header (the web page does this automatically).")
        try:
            length = int(length_header)
            if length < 0:
                raise ValueError
        except ValueError:
            raise UploadError(400, "Bad Content-Length",
                              f"Could not read Content-Length: {length_header!r}") from None

        partial = self.server.store.new_partial()
        written = 0
        try:
            with open(partial, "wb") as handle:
                while written < length:
                    chunk = self.rfile.read(min(CHUNK_SIZE, length - written))
                    if not chunk:
                        raise UploadError(
                            400, "Upload cut short",
                            f"The connection dropped after {human_size(written)} "
                            f"of {human_size(length)}.")
                    handle.write(chunk)
                    written += len(chunk)
                handle.flush()
        except UploadError:
            discard_file(partial)
            # We did not consume the whole body, so the connection is now out
            # of sync and must not be reused for a following request.
            self.close_connection = True
            raise
        except OSError as exc:
            discard_file(partial)
            self.close_connection = True
            raise UploadError(
                507, "Could not write to disk",
                f"{exc}. Check free space and folder permissions for "
                f"{self.server.store.root}.") from exc

        entry = self.server.store.finalize_upload(partial, sender, raw_name, written)
        self.log_message("received %-9s from %-6s -> %s",
                         human_size(written), sender.upper(), entry["rel_path"])
        self._send_json({"ok": True, **entry})

    def _info(self) -> dict:
        """Machine-readable state - handy for debugging from the phone."""
        _, _, host_port = self.headers.get("Host", "?").partition(":")
        return {
            "ok": True,
            "app": APP_NAME,
            "version": VERSION,
            "local_ip": detect_local_ip(),
            "bind": self.server.server_address[0],
            "port": self.server.server_address[1],
            "data_dir": str(DATA_DIR),
            # How the client reached us - 'localhost' on the laptop,
            # the LAN IP on the phone.
            "reached_via_host": self.headers.get("Host", "?"),
            "reached_via_port": host_port or "(default)",
            "client_ip": self.client_address[0],
            # Which side of the bridge this device is on.
            "sender": identify_sender(self.client_address[0],
                                      getattr(self.server, "local_ip", None)),
            "server_time": datetime.datetime.now().strftime("%H:%M:%S"),
        }

    # ---- response helpers ----

    def _send_bytes(self, payload: bytes, content_type: str, *,
                    send_body: bool = True, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # Always re-fetch: during development the page changes on every step.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body and payload:
            self.wfile.write(payload)

    def _send_json(self, data: dict, *, send_body: bool = True, status: int = 200) -> None:
        payload = json.dumps(data, indent=2).encode("utf-8")
        self._send_bytes(payload, "application/json; charset=utf-8",
                         send_body=send_body, status=status)

    # ---- logging ----

    def log_request(self, code="-", size="-") -> None:
        # The page polls this endpoint every couple of seconds; logging each
        # one would bury the upload lines that actually matter.
        if urlparse(self.path).path in QUIET_PATHS:
            return
        super().log_request(code, size)

    def log_message(self, fmt: str, *args) -> None:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        sys.stdout.write(f"  [{stamp}] {self.address_string()}  {fmt % args}\n")
        sys.stdout.flush()


class ShareServer(ThreadingHTTPServer):
    # Daemon threads so Ctrl+C shuts down immediately instead of hanging.
    daemon_threads = True
    # We set the socket options ourselves in server_bind(); leaving this True
    # lets the base class set SO_REUSEADDR, which on Windows cancels out
    # SO_EXCLUSIVEADDRUSE and lets a second copy bind the same port.
    allow_reuse_address = False

    def handle_error(self, request, client_address) -> None:
        """
        A phone that locks its screen, a cancelled upload or a Wi-Fi blip all
        drop the socket abruptly. That is normal, not a crash - without this,
        socketserver prints a full traceback to stderr for every one of them,
        which buries the errors that actually matter.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError, TimeoutError)):
            sys.stderr.write(f"  (client {client_address[0]} disconnected abruptly)\n")
            sys.stderr.flush()
            return
        super().handle_error(request, client_address)

    def server_bind(self) -> None:
        """
        Windows quirk: with SO_REUSEADDR (which http.server enables by default)
        a SECOND copy of this script binds the same port just fine, and the two
        servers then randomly steal each other's connections - the phone appears
        to work half the time. SO_EXCLUSIVEADDRUSE makes a duplicate launch fail
        loudly instead, which is what we want.
        """
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                super().server_bind()
                return
            except OSError:
                pass
            # Retry on a brand-new socket: the port may only be in TIME_WAIT
            # after a recent shutdown. Exclusive-use cannot be cleared on the
            # original socket, so a fresh one is needed. If the port really is
            # busy, this raises again with the real error for the message below.
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = socket.socket(self.address_family, self.socket_type)
            if hasattr(socket, "SO_REUSEADDR"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            super().server_bind()
            return

        self.allow_reuse_address = True  # non-Windows: base behaviour is fine
        super().server_bind()


def port_has_listener(port: int, timeout: float = 0.5) -> bool:
    """
    Is something accepting connections on this port right now?

    Windows reports WSAEACCES (not WSAEADDRINUSE) in some bind-conflict cases,
    so the errno alone cannot tell "another server owns this port" apart from
    "you lack permission". A quick connect tells us for sure.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        try:
            probe.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def start_server(host: str, port: int) -> tuple[ShareServer, str]:
    """Bind and return (server, actual_local_ip). Raises OSError if busy."""
    try:
        server = ShareServer((host, port), ShareHandler)
    except OSError as exc:
        script = Path(sys.argv[0]).name
        if port_has_listener(port):
            raise OSError(
                f"Port {port} is already in use - something is listening there.\n"
                f"    Another copy of {APP_NAME} may still be running.\n"
                f"    Close it, or start on a different port:\n"
                f"        python {script} --port {port + 1}"
            ) from exc
        if exc.errno in (13, 1, 10013) and port < 1024:
            raise OSError(
                f"Windows does not allow listening on port {port} (reserved).\n"
                f"    Use a port above 1024:\n"
                f"        python {script} --port {DEFAULT_PORT}"
            ) from exc
        if exc.errno in (13, 1, 10013):
            raise OSError(
                f"Not allowed to listen on port {port}.\n"
                f"    Another program or user may own it. Try another port:\n"
                f"        python {script} --port {port + 1}"
            ) from exc
        raise
    return server, detect_local_ip() or "127.0.0.1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description=f"{APP_NAME} - share files and text between laptop and phone.",
    )
    parser.add_argument("--host", default=BIND_ADDRESS,
                        help=f"address to listen on (default: {BIND_ADDRESS})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("--ip", default=None,
                        help="override the IP printed for the phone (if detection is wrong)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    fix_console_encoding()
    args = parse_args(argv)

    store = Store(DATA_DIR)
    store.start()
    stale = store.discard_partials()

    try:
        server, detected_ip = start_server(args.host, args.port)
    except OSError as exc:
        print(f"\nERROR: {exc}\n", file=sys.stderr)
        return 1

    server.store = store
    server.local_ip = detected_ip

    advertised_ip = args.ip or detected_ip
    if args.host not in ("0.0.0.0", "::", ""):
        advertised_ip = args.host  # pinned to one adapter; that IS the address

    startup_banner(advertised_ip, server.server_address[1])
    if stale:
        print(f"  (cleaned up {stale} unfinished upload"
              f"{'s' if stale != 1 else ''} from a previous run)")
        sys.stdout.flush()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\n  Shutting down. Bye.")
    finally:
        server.shutdown()
        server.server_close()
    return 0


# ============================== WEB PAGE ==============================
# Plain string - never use an f-string here, the CSS braces would break.

PAGE_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>Teleport Share</title>
<style>
  :root {
    --bg: #f4f5f7;
    --card: #ffffff;
    --text: #16181d;
    --muted: #666e7a;
    --line: #e3e6ea;
    --accent: #1f7a3f;
    --accent-soft: #e6f4ea;
    --danger: #c0392b;
    --danger-soft: #fdecec;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14161a;
      --card: #1d2026;
      --text: #eef1f4;
      --muted: #9aa3af;
      --line: #2c313a;
      --accent: #4ec97e;
      --accent-soft: #16301f;
      --danger: #ff8a80;
      --danger-soft: #3a1f1f;
    }
  }
  * { box-sizing: border-box; -webkit-text-size-adjust: 100%; }
  body {
    margin: 0;
    padding: max(16px, env(safe-area-inset-top)) 16px
             max(16px, env(safe-area-inset-bottom));
    background: var(--bg);
    color: var(--text);
    font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  .wrap { max-width: 560px; margin: 0 auto; }
  header { display: flex; align-items: center; gap: 10px; margin: 4px 0 18px; }
  h1 { font-size: 20px; margin: 0; letter-spacing: -0.01em; }
  .badge {
    margin-left: auto; background: var(--accent-soft); color: var(--accent);
    font-size: 12px; font-weight: 600; padding: 4px 10px;
    border-radius: 999px; white-space: nowrap;
  }
  .badge.err { background: var(--danger-soft); color: var(--danger); }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 14px; padding: 16px; margin-bottom: 14px;
  }
  .card > h2 {
    font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--muted); margin: 0 0 12px; font-weight: 600;
  }

  /* ---- upload area ---- */
  .drop {
    display: block; position: relative; text-align: center;
    border: 2px dashed var(--line); border-radius: 12px;
    padding: 30px 16px; cursor: pointer;
    transition: border-color .12s, background .12s;
    -webkit-tap-highlight-color: transparent; touch-action: manipulation;
  }
  .drop.over { border-color: var(--accent); background: var(--accent-soft); }
  .drop .big { font-size: 32px; line-height: 1; margin-bottom: 10px; }
  .drop .main { font-weight: 600; }
  .drop .hint { color: var(--muted); font-size: 13px; margin-top: 4px; }
  #picker {
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
  }
  .dest {
    margin: 12px 0 0; font-size: 13px; color: var(--muted); text-align: center;
  }
  .dest b { color: var(--text); font-weight: 600; }

  /* ---- file rows ---- */
  ul.rows { list-style: none; margin: 14px 0 0; padding: 0; }
  ul.rows li { padding: 10px 0; border-top: 1px solid var(--line); }
  .rowtop { display: flex; align-items: baseline; gap: 10px; }
  .fname {
    font-size: 14px; font-weight: 600; flex: 1; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .fmeta { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .bar {
    height: 5px; border-radius: 3px; background: var(--line);
    margin-top: 7px; overflow: hidden;
  }
  .bar > i { display: block; height: 100%; width: 0; background: var(--accent); transition: width .12s; }
  .fstatus { font-size: 12.5px; margin-top: 6px; word-break: break-word; }
  .fstatus.ok { color: var(--accent); }
  .fstatus.bad { color: var(--danger); }
  .fstatus.mut { color: var(--muted); }
  .clear {
    margin-top: 12px; background: none; border: 0; padding: 6px 2px;
    color: var(--muted); font: inherit; font-size: 13px; cursor: pointer;
  }
  .clear[disabled] { opacity: .4; }

  /* ---- received feed ---- */
  ul.feed { list-style: none; margin: 0; padding: 0; }
  ul.feed li {
    display: flex; align-items: baseline; gap: 9px;
    padding: 9px 0; border-top: 1px solid var(--line);
  }
  ul.feed li:first-child { border-top: 0; }
  ul.feed li.fresh { animation: flash 1.8s ease-out; }
  @keyframes flash {
    from { background: var(--accent-soft); }
    to   { background: transparent; }
  }
  .fdir {
    font-size: 10px; font-weight: 700; letter-spacing: 0.05em;
    padding: 3px 6px; border-radius: 4px; white-space: nowrap;
    background: var(--bg); color: var(--muted);
  }
  .fdir.phone  { background: #e8f0fe; color: #1a56c4; }
  .fdir.laptop { background: var(--accent-soft); color: var(--accent); }
  @media (prefers-color-scheme: dark) {
    .fdir.phone { background: #1b2a4a; color: #8fb4ff; }
  }
  .feedname {
    flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; font-size: 14px;
  }
  .feedsize {
    font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .empty { color: var(--muted); font-size: 14px; margin: 0; }

  /* ---- connection ---- */
  .kv { display: flex; justify-content: space-between; gap: 12px; padding: 4px 0; }
  .kv dt { color: var(--muted); font-size: 14px; }
  .kv dd {
    margin: 0; font-size: 14px; font-family: ui-monospace, Consolas, monospace;
    word-break: break-all; text-align: right;
  }
  code { font-family: ui-monospace, Consolas, monospace; }
  .foot { text-align: center; color: var(--muted); font-size: 12px; margin-top: 16px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Teleport Share</h1>
    <span class="badge" id="status">connecting&hellip;</span>
  </header>

  <div class="card">
    <h2>Send files</h2>
    <label class="drop" id="drop" for="picker">
      <div class="big">&#128206;</div>
      <div class="main">Tap to choose files</div>
      <div class="hint">or drop them here</div>
      <input type="file" id="picker" multiple>
    </label>
    <p class="dest">Files will be saved to <b id="dest">&hellip;</b></p>
    <ul class="rows" id="rows"></ul>
    <button class="clear" id="clear" disabled>Clear finished</button>
  </div>

  <div class="card">
    <h2>Received</h2>
    <ul class="feed" id="feed"></ul>
    <p class="empty" id="feedEmpty">Nothing yet. Send a file from the phone.</p>
  </div>

  <div class="card">
    <h2>Connection</h2>
    <dl style="margin:0">
      <div class="kv"><dt>This device is the</dt><dd id="who">&mdash;</dd></div>
      <div class="kv"><dt>Reached via</dt><dd id="via">&mdash;</dd></div>
      <div class="kv"><dt>Laptop address</dt><dd id="lan">&mdash;</dd></div>
    </dl>
  </div>

  <p class="foot">Saved in <code id="dir">&hellip;</code></p>
</div>

<script>
(function () {
  'use strict';

  var rows = document.getElementById('rows');
  var picker = document.getElementById('picker');
  var drop = document.getElementById('drop');
  var clearBtn = document.getElementById('clear');
  var chain = Promise.resolve();

  function fmt(n) {
    var units = ['B', 'KB', 'MB', 'GB'], i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + ' ' + units[i];
  }

  function setStatus(text, isError) {
    var el = document.getElementById('status');
    el.textContent = text;
    el.className = isError ? 'badge err' : 'badge';
  }

  // ---- one row per file ----
  function addRow(file) {
    var li = document.createElement('li');
    var top = document.createElement('div');
    top.className = 'rowtop';
    var name = document.createElement('span');
    name.className = 'fname';
    name.textContent = file.name;          // textContent, never innerHTML
    name.title = file.name;
    var meta = document.createElement('span');
    meta.className = 'fmeta';
    meta.textContent = fmt(file.size);
    top.appendChild(name);
    top.appendChild(meta);

    var bar = document.createElement('div');
    bar.className = 'bar';
    var fill = document.createElement('i');
    bar.appendChild(fill);

    var status = document.createElement('div');
    status.className = 'fstatus mut';
    status.textContent = 'waiting...';

    li.appendChild(top);
    li.appendChild(bar);
    li.appendChild(status);
    rows.appendChild(li);
    clearBtn.disabled = false;

    return {
      fill: fill,
      setMeta: function (t) { meta.textContent = t; },
      setStatus: function (t, kind) {
        status.textContent = t;
        status.className = 'fstatus' + (kind ? ' ' + kind : '');
      }
    };
  }

  function sendOne(file) {
    return new Promise(function (resolve) {
      var row = addRow(file);
      var xhr = new XMLHttpRequest();
      // Name is percent-encoded so Unicode names survive the query string.
      xhr.open('POST', '/api/upload?name=' + encodeURIComponent(file.name));
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');

      xhr.upload.onprogress = function (e) {
        if (!e.lengthComputable) return;
        var pct = Math.round(e.loaded / e.total * 100);
        row.fill.style.width = pct + '%';
        row.setMeta(pct + '%  ' + fmt(e.loaded));
        row.setStatus('uploading...', '');
      };

      xhr.onload = function () {
        var data = null;
        try { data = JSON.parse(xhr.responseText); } catch (err) {}
        if (xhr.status === 200 && data && data.ok) {
          row.fill.style.width = '100%';
          row.setMeta(fmt(data.size));
          row.setStatus('Saved to ' + data.rel_path, 'ok');
        } else {
          var why = 'HTTP ' + xhr.status;
          if (data && data.error) why = data.error;
          if (data && data.detail) why += ' - ' + data.detail;
          row.setStatus('Failed: ' + why, 'bad');
        }
        resolve();
      };

      xhr.onerror = function () {
        row.setStatus('Failed: connection lost. Is the laptop still running?', 'bad');
        resolve();
      };
      xhr.onabort = function () {
        row.setStatus('Cancelled', 'mut');
        resolve();
      };

      // Passing the File straight through: the browser streams it from disk,
      // so it never loads into page memory - even for very large files.
      xhr.send(file);
    });
  }

  function sendAll(files) {
    var i = 0;
    function next() {
      if (i >= files.length) return Promise.resolve();
      return sendOne(files[i++]).then(next);
    }
    return next();
  }

  // Queue uploads one at a time: a hotspot is a narrow pipe, and one file at
  // a time gives the other device a usable connection in between.
  function enqueue(files) {
    if (!files.length) return;
    chain = chain.then(function () { return sendAll(files); });
  }

  picker.addEventListener('change', function () {
    enqueue(Array.prototype.slice.call(picker.files));
    picker.value = '';   // so picking the same file again still fires change
  });

  ['dragenter', 'dragover'].forEach(function (ev) {
    drop.addEventListener(ev, function (e) {
      e.preventDefault(); e.stopPropagation();
      drop.classList.add('over');
    });
  });
  ['dragleave', 'dragend'].forEach(function (ev) {
    drop.addEventListener(ev, function () { drop.classList.remove('over'); });
  });
  drop.addEventListener('drop', function (e) {
    e.preventDefault(); e.stopPropagation();
    drop.classList.remove('over');
    if (e.dataTransfer && e.dataTransfer.files.length) {
      enqueue(Array.prototype.slice.call(e.dataTransfer.files));
    }
  });
  // Stop the browser from navigating to the dropped file.
  window.addEventListener('dragover', function (e) { e.preventDefault(); });
  window.addEventListener('drop', function (e) { e.preventDefault(); });

  clearBtn.addEventListener('click', function () {
    rows.innerHTML = '';
    clearBtn.disabled = true;
  });

  // ---- received files: polled so an upload shows up on its own ----
  var feed = document.getElementById('feed');
  var feedEmpty = document.getElementById('feedEmpty');
  var newestId = 0;
  var primed = false;

  function clockOf(ts) {
    try {
      return new Date(ts * 1000).toLocaleTimeString([], {
        hour: '2-digit', minute: '2-digit'
      });
    } catch (err) {
      return '';
    }
  }

  function renderFeed(items) {
    feedEmpty.style.display = items.length ? 'none' : 'block';
    feed.textContent = '';
    var highest = newestId;
    items.forEach(function (it) {
      var li = document.createElement('li');
      if (primed && it.id > newestId) li.className = 'fresh';
      var tag = document.createElement('span');
      tag.className = 'fdir ' + it.sender;
      tag.textContent = it.sender === 'phone' ? 'PHONE' : 'LAPTOP';
      var name = document.createElement('span');
      name.className = 'feedname';
      name.textContent = it.stored_name;      // never innerHTML: name is user input
      name.title = it.rel_path || it.stored_name;
      var meta = document.createElement('span');
      meta.className = 'feedsize';
      meta.textContent = fmt(it.size || 0) + '  ' + clockOf(it.ts);
      li.appendChild(tag);
      li.appendChild(name);
      li.appendChild(meta);
      feed.appendChild(li);
      if (it.id > highest) highest = it.id;
    });
    newestId = highest;
    primed = true;
  }

  function poll() {
    fetch('/api/recent?limit=12')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) throw new Error('bad response');
        setStatus('connected', false);
        renderFeed(d.files || []);
      })
      .catch(function () { setStatus('disconnected', true); });
  }

  poll();
  setInterval(poll, 2000);

  // ---- who am I, and where do files go ----
  fetch('/api/info')
    .then(function (r) { return r.json(); })
    .then(function (d) {
      setStatus('connected', false);
      var who = d.sender === 'laptop' ? 'Laptop' : 'Phone';
      document.getElementById('who').textContent = who;
      document.getElementById('dest').textContent =
        'SharedFiles/' + (d.sender === 'laptop' ? 'FromLaptop' : 'FromPhone');
      document.getElementById('via').textContent = d.reached_via_host;
      document.getElementById('lan').textContent = d.local_ip + ':' + d.port;
      document.getElementById('dir').textContent = d.data_dir;
    })
    .catch(function () {
      setStatus('disconnected', true);
    });
})();
</script>
</body>
</html>"""




# Entry point must stay last: main() serves PAGE_HTML, so the page string has
# to exist before the first request can arrive.
if __name__ == "__main__":
    sys.exit(main())