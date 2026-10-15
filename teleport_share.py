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
import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import re
import secrets
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

# ============================== CONFIG ==============================
# Everything you are likely to want to change lives in this block.

APP_NAME = "Teleport Share"
VERSION = "0.2.0"

# 0.0.0.0 = reachable from any network the laptop is on (needed for the phone).
# Set to the hotspot IP (e.g. "192.168.181.50") to only listen on that adapter.
BIND_ADDRESS = "0.0.0.0"

DEFAULT_PORT = 8765

# Fallback PIN, used only when there is no .env file. A real PIN lives in
# .env next to this script, which is never committed.
PIN = "123456"

ENV_FILE = Path(__file__).resolve().parent / ".env"

# How long an unlocked device stays unlocked before asking again.
SESSION_HOURS = 12

# Brute-force protection: after this many wrong PINs from one address, that
# address is refused for a while.
LOGIN_MAX_TRIES = 10
LOGIN_LOCKOUT_SECONDS = 60

# All shared data lives in here (created automatically on first run).
DATA_DIR = Path(__file__).resolve().parent / "SharedFiles"

# Reads/writes the socket in blocks of this size. Never loads a whole file
# into memory, so a 4 GB video costs the same RAM as a 4 KB text file.
CHUNK_SIZE = 256 * 1024

# Copy text arriving from the phone straight to the Windows clipboard.
CLIPBOARD_AUTOCOPY_DEFAULT = True

# Longest single message accepted, as characters and as a request body.
MAX_TEXT_CHARS = 20_000
MAX_TEXT_BYTES = 200_000

# Longest filename we will store; the extension is preserved when trimming.
MAX_NAME_LEN = 120

# Hard cap on index entries (history). Files on disk are never deleted.
MAX_INDEX_ENTRIES = 5000

# Endpoints reachable without unlocking. The page itself must load so it can
# show the PIN box; nothing else does.
AUTH_EXEMPT_PATHS = {"/", "/favicon.ico", "/api/login"}

# Endpoints with their own tailored log line (uploads and downloads report
# sender and destination, which is far more useful than "POST /api/upload").
QUIET_PATHS = {"/api/files", "/api/upload", "/api/download",
               "/api/texts", "/api/text", "/api/settings", "/api/file"}

# Reserved-for-later settings, so the top of the file stays the single place
# to look. Unused by step 4; wired up in later steps.
# PIN = "123456"                        # step 7
# HISTORY_LIMIT = 100                   # step 6

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
        self.config_path = root / ".teleport_config.json"
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._next_id = 1
        self.config: dict = {}

    # ---- startup ----

    def start(self) -> None:
        for folder in (self.root, self.from_phone, self.from_laptop,
                       self.texts, self.tmp):
            folder.mkdir(parents=True, exist_ok=True)
        self._load_index()
        try:
            saved = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.config = saved
        except (OSError, ValueError):
            pass  # no config yet: defaults apply

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

    def annotate(self, entry: dict, **fields) -> None:
        """Attach extra detail to an entry already saved (e.g. clipboard state)."""
        with self._lock:
            for existing in self._entries:
                if existing.get("id") == entry.get("id"):
                    existing.update(fields)
                    break
            self._save_locked()

    # ---- deleting ----

    def _drop_entries_locked(self, predicate) -> int:
        """Remove matching history entries. Call with the lock held."""
        before = len(self._entries)
        self._entries = [e for e in self._entries if not predicate(e)]
        removed = before - len(self._entries)
        if removed:
            self._save_locked()
        return removed

    def delete_file(self, raw: str) -> dict:
        """
        Delete one shared file: off the disk and out of the history.

        Goes through the same containment checks as a download, so a crafted
        request still cannot reach anything outside SharedFiles.
        """
        path = self.resolve_for_download(raw)  # raises UploadError if not allowed
        rel = path.relative_to(self.root.resolve()).as_posix()
        size = path.stat().st_size
        try:
            path.unlink()
        except OSError as exc:
            raise UploadError(500, "Could not delete",
                              f"{raw}: {exc}") from exc
        with self._lock:
            self._drop_entries_locked(
                lambda e: e.get("rel_path") == rel and e.get("type") == "file")
        return {"rel_path": rel, "size": size}

    def delete_all_files(self) -> dict:
        """
        Delete every shared file, at any depth, in one go.

        Messages under Texts/ are deliberately left alone - they are a separate
        kind of thing and cheap to keep.
        """
        root = self.root.resolve()
        removed, freed = 0, 0
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in self.HIDDEN_DIRS]
            here = Path(dirpath)
            for filename in filenames:
                if filename.startswith("."):
                    continue
                path = here / filename
                rel = path.relative_to(root)
                if not self._is_listed(rel):
                    continue
                try:
                    if not path.resolve().is_relative_to(root):
                        continue
                    freed += path.stat().st_size
                    path.unlink()
                    removed += 1
                except OSError:
                    continue  # a file we cannot remove is not worth aborting for
        with self._lock:
            self._drop_entries_locked(lambda e: e.get("type") == "file")
        return {"removed": removed, "bytes": freed}

    def delete_all_texts(self) -> dict:
        """
        Clear every saved message: the .txt files under Texts/ and the
        matching history entries. There is no undo.
        """
        removed, freed = 0, 0
        if self.texts.is_dir():
            for path in self.texts.rglob("*"):
                if not path.is_file() or path.suffix.lower() != ".txt":
                    continue
                try:
                    freed += path.stat().st_size
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        with self._lock:
            self._drop_entries_locked(lambda e: e.get("type") == "text")
        return {"removed": removed, "bytes": freed}

    # ---- settings ----

    def get_setting(self, key: str, default):
        with self._lock:
            return self.config.get(key, default)

    def set_setting(self, key: str, value) -> None:
        with self._lock:
            self.config[key] = value
            tmp = self.config_path.with_name(self.config_path.name + ".tmp")
            tmp.write_text(json.dumps(self.config, indent=1), encoding="utf-8")
            os.replace(tmp, self.config_path)

    # ---- texts ----

    def record_text(self, sender: str, text: str) -> dict:
        """Save a message as a timestamped .txt and note it in the history."""
        with self._lock:
            name = f"{stamp_compact()}_from-{sender}.txt"
            path = unique_path(self.texts, name)
            path.write_text(text, encoding="utf-8")
            return self._record_locked({
                "type": "text",
                "sender": sender,
                "text": text,
                "original_name": path.name,
                "stored_name": path.name,
                "rel_path": path.relative_to(self.root).as_posix(),
            })

    def recent_texts(self, limit: int = 20) -> list[dict]:
        """Newest-first messages, for the feed on both pages."""
        with self._lock:
            entries = [e for e in self._entries if e.get("type") == "text"]
            return list(reversed(entries[-limit:]))

    # ---- files ----

    # Bookkeeping folders: never listed, never downloadable.
    HIDDEN_DIRS = {"_inbox_tmp", "Texts"}

    def _is_listed(self, rel: Path) -> bool:
        """Is this relative path something the other device may see?"""
        return not any(part.startswith(".") or part in self.HIDDEN_DIRS
                       for part in rel.parts)

    def list_files(self, limit: int = 200) -> list[dict]:
        """
        Every shareable file, newest first.

        Walks the folder rather than the index, so files you copy in by hand
        show up without being 'registered'. Symlinks that point outside the
        data folder are skipped, and symlinked directories are never followed.
        """
        root = self.root.resolve()
        entries: list[dict] = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in self.HIDDEN_DIRS]
            here = Path(dirpath)
            for filename in filenames:
                if filename.startswith("."):
                    continue
                path = here / filename
                rel = path.relative_to(root)
                if not self._is_listed(rel):
                    continue
                try:
                    resolved = path.resolve()
                    if not resolved.is_relative_to(root):
                        continue  # symlink escaping the data folder
                    stat = resolved.stat()
                except (OSError, ValueError):
                    continue  # vanished or unreadable mid-walk
                folder = rel.parts[0] if len(rel.parts) > 1 else ""
                entries.append({
                    "rel_path": rel.as_posix(),
                    "name": filename,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "sender": "phone" if folder == "FromPhone" else "laptop",
                    "manual": folder not in ("FromPhone", "FromLaptop"),
                })
        entries.sort(key=lambda e: e["mtime"], reverse=True)
        return entries[:limit]

    def resolve_for_download(self, raw: str) -> Path:
        """
        Turn a requested relative path into a real file inside the data folder,
        or raise UploadError explaining why not.

        Every check is deliberate and redundant: explicit rejection of '..',
        then resolve(), then a containment test. Any one of them alone would do,
        but this is the route where a bug means reading arbitrary files.
        """
        raw = (raw or "").strip()
        if not raw:
            raise UploadError(400, "No file requested",
                              "The 'path' parameter was empty.")
        rel = Path(raw.replace("\\", "/"))
        if rel.is_absolute() or any(part in ("..", ".") for part in rel.parts):
            raise UploadError(400, "Bad path",
                              f"Refusing to serve {raw!r}.")
        if not self._is_listed(rel):
            raise UploadError(403, "Not available",
                              f"{raw!r} is an internal file.")
        root = self.root.resolve()
        target = (root / rel).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise UploadError(404, "File not found", f"No such file: {raw}")
        return target

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


def load_env_file(path: Path) -> dict:
    """
    Read a tiny .env file. Not a full dotenv implementation - just the part a
    person actually writes by hand:

        # a comment
        PIN=123456
        export DATA_DIR="C:/Some Folder/Shared"

    Quoted values keep their spaces, blank lines and comments are ignored, and
    a line without '=' is skipped rather than guessed at.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def ensure_env_file(path: Path) -> bool:
    """
    Create the .env on first run with a random PIN, so the default is never
    something guessable. Returns True if a file was created.
    """
    if path.exists():
        return False
    generated = f"{secrets.randbelow(900000) + 100000:06d}"
    try:
        path.write_text(
            "# Teleport Share settings. This file holds your PIN - do not\n"
            "# commit it, and do not share it.\n"
            "#\n"
            "# Set PIN to an empty value to switch the lock off:\n"
            "#   PIN=\n"
            "\n"
            f"PIN={generated}\n",
            encoding="utf-8")
    except OSError:
        return False
    print(f"  Created {path.name} with a new random PIN: {generated}")
    print("  Write it down, or change it any time in that file.")
    sys.stdout.flush()
    return True


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


def startup_banner(ip: str | None, port: int, pin_source: str) -> None:
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
    if PIN:
        print(line)
        print(f"  PIN required ({len(PIN)} characters), read from {pin_source}")
        if len(PIN) != 6:
            print(f"  ! That is not six characters. It is still accepted, but a")
            print(f"    six digit PIN is much harder to guess by accident.")
        if pin_source != ENV_FILE.name:
            print("  ! No .env found, so the built-in fallback PIN is in use.")
            print("    Change it in " + str(ENV_FILE) + " - never commit that file.")
    else:
        print(line)
        print("  PIN is switched off - anyone who reaches this page has full access.")
    print(line)
    sys.stdout.flush()  # so the banner appears even when output is redirected


# ============================= CLIPBOARD ==============================
# Windows only, and done through ctypes rather than tkinter (which may be
# missing from a Python build and would pop a window) or pyperclip (pip).

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def copy_to_clipboard(text: str) -> tuple[bool, str]:
    """
    Put text on the Windows clipboard.

    Returns (ok, reason_if_not). Another program can hold the clipboard open
    for a moment, so opening is retried a few times before giving up.
    """
    if os.name != "nt":
        return False, "Clipboard is only wired up on Windows."

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # These default to 32-bit ints, which silently truncates pointers on a
    # 64-bit process - so every pointer-returning call needs restype set.
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]

    opened = False
    for _ in range(10):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.05)  # another app is using it; almost always momentary
    if not opened:
        return False, "Another program is holding the clipboard open."

    try:
        if not user32.EmptyClipboard():
            return False, "Could not clear the clipboard."
        buffer = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buffer)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False, "Out of memory."
        locked = kernel32.GlobalLock(handle)
        if not locked:
            kernel32.GlobalFree(handle)
            return False, "Could not lock clipboard memory."
        try:
            ctypes.memmove(locked, buffer, size)
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False, "Windows refused the clipboard data."
        # Ownership passed to the system - deliberately not freed.
        return True, ""
    finally:
        user32.CloseClipboard()


def read_clipboard() -> str:
    """Read the clipboard back. Used by the tests to prove the copy worked."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    # Without an explicit restype this returns a 32-bit int and truncates the
    # handle on a 64-bit process, so the clipboard always reads back empty.
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [wintypes.UINT]

    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return ""
        try:
            return ctypes.c_wchar_p(locked).value or ""
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


# ============================== AUTH ================================
#
# The PIN only stops someone who opens the URL. Traffic is still plain HTTP,
# so it does not stop a determined sniffer on the same network - see the
# Security notes in the README.


class Sessions:
    """Unlocked devices, held in memory only.

    Tokens deliberately do NOT survive a restart: if the laptop is rebooted
    everyone has to type the PIN again, which is what you want.
    """

    def __init__(self, hours: int = SESSION_HOURS) -> None:
        self._tokens: dict[str, float] = {}
        self._lock = threading.Lock()
        self._ttl = hours * 3600

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune_locked()
            self._tokens[token] = time.time() + self._ttl
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            expires = self._tokens.get(token)
            if expires is None:
                return False
            if expires < time.time():
                del self._tokens[token]
                return False
            return True

    def _prune_locked(self) -> None:
        now = time.time()
        for token in [t for t, exp in self._tokens.items() if exp < now]:
            del self._tokens[token]

    def count(self) -> int:
        with self._lock:
            self._prune_locked()
            return len(self._tokens)


class LoginGuard:
    """Slows down guessing the PIN, per source address."""

    def __init__(self, max_tries: int = LOGIN_MAX_TRIES,
                 lockout: int = LOGIN_LOCKOUT_SECONDS) -> None:
        self._fails: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}
        self._lock = threading.Lock()
        self.max_tries = max_tries
        self.lockout = lockout

    def locked_for(self, ip: str) -> float:
        """Seconds remaining on the lockout, or 0 if not locked."""
        with self._lock:
            until = self._locked.get(ip, 0)
            if not until:
                return 0
            remaining = until - time.time()
            if remaining <= 0:
                del self._locked[ip]
                self._fails.pop(ip, None)
                return 0
            return remaining

    def record_failure(self, ip: str) -> None:
        with self._lock:
            now = time.time()
            recent = [t for t in self._fails.get(ip, []) if now - t < 300]
            recent.append(now)
            self._fails[ip] = recent
            if len(recent) >= self.max_tries:
                self._locked[ip] = now + self.lockout
                self._fails.pop(ip, None)

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)
            self._locked.pop(ip, None)


def pin_matches(given: str) -> bool:
    """Compare without leaking the PIN through timing."""
    if not PIN:
        return True  # PIN switched off
    if not isinstance(given, str):
        return False
    return hmac.compare_digest(given.strip(), PIN)


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


def content_disposition(filename: str) -> str:
    """
    'attachment' with a readable name for every browser.

    The plain filename= is an ASCII fallback (some old clients read only that);
    filename*=UTF-8'' carries the real name so accents and non-Latin scripts
    survive the round trip.
    """
    ascii_name = filename.encode("ascii", "replace").decode("ascii")
    ascii_name = ascii_name.replace('"', "").replace("\\", "_")
    return (f"attachment; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{quote(filename, safe='')}")


class ShareHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 keeps connections alive (needed for fast polling later).
    # It also means every response MUST carry an accurate Content-Length.
    protocol_version = "HTTP/1.1"
    server_version = f"{APP_NAME}/{VERSION}"
    sys_version = ""  # don't advertise the Python version
    # Nagle's algorithm holds back small writes waiting for an ACK, which adds
    # up to 40ms of delay per exchange on a slow link. Free to turn off.
    disable_nagle_algorithm = True

    # ---- request routing ----

    def do_GET(self) -> None:
        self._dispatch(send_body=True)

    def do_HEAD(self) -> None:
        self._dispatch(send_body=False)

    def do_POST(self) -> None:
        self._dispatch(send_body=True)

    def do_DELETE(self) -> None:
        self._dispatch(send_body=True)

    def _dispatch(self, send_body: bool) -> None:
        try:
            self._route(send_body)
        except UploadError as exc:
            self.log_message("!! %d %s (%s)", exc.status, exc.message, exc.detail)
            # An error reply may come before the request body was read. Closing
            # is cheap and stops the unread remainder from being mistaken for
            # the next request line on a keep-alive connection.
            self.close_connection = True
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

        if route not in AUTH_EXEMPT_PATHS:
            self._require_auth()

        if route == "/":
            self._send_bytes(PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8",
                             send_body=send_body)
        elif route == "/api/info":
            self._send_json(self._info(), send_body=send_body)
        elif route == "/api/file" and self.command == "DELETE":
            self._handle_delete(query)
        elif route == "/api/files" and self.command == "DELETE":
            self._handle_delete_all()
        elif route == "/api/login":
            self._handle_login()
        elif route == "/api/texts" and self.command == "DELETE":
            self._handle_clear_texts()
        elif route == "/api/texts":
            limit = min(max(int((query.get("limit") or ["20"])[0] or 20), 1), 100)
            self._send_json({"ok": True,
                             "texts": self.server.store.recent_texts(limit)},
                            send_body=send_body)
        elif route == "/api/settings":
            self._handle_settings()
        elif route == "/api/text":
            self._handle_text(query)
        elif route == "/api/files":
            limit = min(max(int((query.get("limit") or ["200"])[0] or 200), 1), 500)
            self._send_json({
                "ok": True,
                "data_dir": str(self.server.store.root),
                # Which side is asking. Included here as well as in /api/info
                # so the page needs only this one request to decide whether to
                # auto-save; waiting for a second response would race.
                "sender": identify_sender(self.client_address[0],
                                          getattr(self.server, "local_ip", None)),
                "files": self.server.store.list_files(limit),
            }, send_body=send_body)
        elif route == "/api/download":
            self._handle_download(query)
        elif route == "/api/upload":
            self._handle_upload(query)
        elif route == "/favicon.ico":
            self._send_bytes(b"", "image/x-icon", send_body=False, status=204)
        else:
            self._send_json(
                {"ok": False, "error": "Not found", "path": route},
                status=404, send_body=send_body,
            )

    def _handle_download(self, query: dict) -> None:
        """
        Stream a file back to the requesting device, a block at a time.
        The path has already been checked to be inside the data folder.
        """
        raw = (query.get("path") or [""])[0]
        path = self.server.store.resolve_for_download(raw)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise UploadError(404, "File not found",
                              f"{raw} could not be read: {exc}") from exc

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", content_disposition(path.name))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        sent = 0
        with open(path, "rb") as handle:
            while sent < size:
                chunk = handle.read(min(CHUNK_SIZE, size - sent))
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
        self.log_message("sent    %-9s to %-15s (%s)",
                         human_size(sent), self.client_address[0], raw)

    def _handle_clear_texts(self) -> None:
        result = self.server.store.delete_all_texts()
        self.log_message("cleared ALL messages: %d removed, %s freed",
                         result["removed"], human_size(result["bytes"]))
        self._send_json({"ok": True, **result})

    def _handle_delete(self, query: dict) -> None:
        raw = (query.get("path") or [""])[0]
        gone = self.server.store.delete_file(raw)
        self.log_message("deleted %-9s (%s)", human_size(gone["size"]),
                         gone["rel_path"])
        self._send_json({"ok": True, **gone})

    def _require_auth(self) -> None:
        """Every API route except the login one goes through here."""
        if not PIN:
            return  # PIN switched off
        token = self.headers.get("X-Auth-Token", "")
        if not self.server.sessions.valid(token):
            raise UploadError(401, "Locked",
                              "Enter the PIN to unlock this page.")

    def _handle_login(self) -> None:
        """Swap the right PIN for a random token the page keeps."""
        ip = self.client_address[0]

        # Always consume the body, even when the PIN is switched off and the
        # answer needs no input - otherwise it stays in the socket.
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        if not PIN:
            self._send_json({"ok": True, "pin_required": False, "token": ""})
            return

        try:
            payload = json.loads(raw.decode("utf-8")) if length else {}
        except (ValueError, UnicodeDecodeError) as exc:
            raise UploadError(400, "Bad request", str(exc)) from exc

        # Check the lockout only after reading the body. Refusing early would
        # leave an unread body in the socket, and the next request on that
        # keep-alive connection would be parsed starting mid-JSON.
        wait = self.server.login_guard.locked_for(ip)
        if wait:
            self.log_message("!! login locked out %s for %ds", ip, int(wait) + 1)
            raise UploadError(429, "Too many tries",
                              f"Wait {int(wait) + 1} seconds and try again.")

        if not isinstance(payload, dict) or not pin_matches(payload.get("pin", "")):
            self.server.login_guard.record_failure(ip)
            self.log_message("!! wrong PIN from %s", ip)
            raise UploadError(401, "Wrong PIN", "That PIN is not correct.")

        self.server.login_guard.record_success(ip)
        token = self.server.sessions.create()
        self.log_message("unlocked %s (session %s)", ip, token[:8] + "...")
        self._send_json({"ok": True, "pin_required": True, "token": token,
                         "expires_in": SESSION_HOURS * 3600})

    def _handle_delete_all(self) -> None:
        result = self.server.store.delete_all_files()
        self.log_message("deleted ALL files: %d removed, %s freed",
                         result["removed"], human_size(result["bytes"]))
        self._send_json({"ok": True, **result})

    def _handle_text(self, query: dict) -> None:
        """
        A typed message: saved as a timestamped .txt, added to the history, and
        - for text arriving from the phone - optionally copied to the Windows
        clipboard so it can be pasted straight into whatever is on screen.
        """
        length_header = self.headers.get("Content-Length")
        if not length_header:
            raise UploadError(411, "Missing Content-Length",
                              "Send the message as a JSON body.")
        try:
            length = int(length_header)
        except ValueError:
            raise UploadError(400, "Bad Content-Length",
                              f"Could not read Content-Length: {length_header!r}") from None
        if length <= 0:
            raise UploadError(400, "Empty message", "Nothing was sent.")
        if length > MAX_TEXT_BYTES:
            raise UploadError(413, "Message too long",
                              f"Keep it under {MAX_TEXT_CHARS} characters.")

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise UploadError(400, "Bad message",
                              f"Body must be JSON like {{\"text\": \"...\"}}: {exc}") from exc
        if not isinstance(payload, dict):
            raise UploadError(400, "Bad message", "Body must be a JSON object.")

        text = payload.get("text")
        if not isinstance(text, str):
            raise UploadError(400, "Bad message", "The 'text' field must be a string.")
        text = text.strip()
        if not text:
            raise UploadError(400, "Empty message", "Type something first.")
        if len(text) > MAX_TEXT_CHARS:
            raise UploadError(413, "Message too long",
                              f"Keep it under {MAX_TEXT_CHARS} characters "
                              f"(this one is {len(text)}).")

        sender = str(payload.get("from") or (query.get("from") or [""])[0]).lower()
        if sender not in ("phone", "laptop"):
            sender = identify_sender(self.client_address[0],
                                     getattr(self.server, "local_ip", None))

        entry = self.server.store.record_text(sender, text)

        clipboard = {"copied": False, "detail": "Not applicable."}
        want_clip = self.server.store.get_setting("clipboard_autocopy",
                                                  CLIPBOARD_AUTOCOPY_DEFAULT)
        if sender == "phone" and want_clip:
            copied, why = copy_to_clipboard(text)
            clipboard = {"copied": copied,
                         "detail": "" if copied else why}
        elif sender == "phone":
            clipboard = {"copied": False,
                         "detail": "Clipboard auto-copy is switched off."}

        # Remember whether it reached the clipboard, so both pages can show it.
        self.server.store.annotate(entry, clipboard=clipboard)

        self.log_message("text    from %-6s (%d chars)%s -> %s",
                         sender.upper(), len(text),
                         "  [copied to clipboard]" if clipboard["copied"] else "",
                         entry["rel_path"])
        self._send_json({"ok": True, **entry, "clipboard": clipboard})

    def _handle_settings(self) -> None:
        """Read or change the little settings the page can toggle."""
        store = self.server.store
        if self.command == "GET":
            self._send_json({
                "ok": True,
                "clipboard_autocopy": store.get_setting("clipboard_autocopy",
                                                        CLIPBOARD_AUTOCOPY_DEFAULT),
            })
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8")) \
                if length else {}
        except (ValueError, UnicodeDecodeError) as exc:
            raise UploadError(400, "Bad settings", str(exc)) from exc
        if not isinstance(payload, dict):
            raise UploadError(400, "Bad settings", "Body must be a JSON object.")

        if "clipboard_autocopy" in payload:
            store.set_setting("clipboard_autocopy",
                              bool(payload["clipboard_autocopy"]))
        self._send_json({
            "ok": True,
            "clipboard_autocopy": store.get_setting("clipboard_autocopy",
                                                    CLIPBOARD_AUTOCOPY_DEFAULT),
        })

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
            "pin_required": bool(PIN),
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
    global PIN
    fix_console_encoding()
    args = parse_args(argv)

    # A real PIN lives in .env, which is gitignored. The value in this file is
    # only a fallback for the rare case there is no .env at all.
    ensure_env_file(ENV_FILE)
    env = load_env_file(ENV_FILE)
    pin_source = ENV_FILE.name
    if "PIN" in env:
        PIN = env["PIN"].strip()
        pin_source = ENV_FILE.name
    else:
        pin_source = "the fallback inside the script (no PIN in .env)"

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
    server.sessions = Sessions()
    server.login_guard = LoginGuard()

    advertised_ip = args.ip or detected_ip
    if args.host not in ("0.0.0.0", "::", ""):
        advertised_ip = args.host  # pinned to one adapter; that IS the address

    startup_banner(advertised_ip, server.server_address[1], pin_source)
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
  .iconbtn {
    margin-left: auto; width: 40px; height: 40px; flex: none;
    display: flex; align-items: center; justify-content: center;
    font-size: 19px; line-height: 1; cursor: pointer;
    background: var(--accent-soft); color: var(--accent);
    border: 0; border-radius: 11px;
    -webkit-tap-highlight-color: transparent; touch-action: manipulation;
  }
  .iconbtn:active { transform: scale(.94); }
  .iconbtn[disabled] { opacity: .45; cursor: default; }
  .badge { margin-left: 8px; font-size: 12px; font-weight: 600; padding: 4px 10px;
    border-radius: 999px; white-space: nowrap;
    background: var(--accent-soft); color: var(--accent); }
  .badge.err { background: var(--danger-soft); color: var(--danger); }

  /* ---- text modal ---- */
  dialog {
    border: 0; border-radius: 16px; padding: 0; width: min(520px, calc(100vw - 28px));
    background: var(--card); color: var(--text);
    box-shadow: 0 18px 50px rgba(0,0,0,.32);
  }
  dialog::backdrop { background: rgba(0,0,0,.45); }
  .dlg { padding: 18px; }
  .dlg h3 { margin: 0 0 4px; font-size: 17px; }
  .dlg .hint { margin: 0 0 12px; font-size: 13px; color: var(--muted); }
  #textInput {
    width: 100%; resize: vertical; min-height: 108px;
    font: 15px/1.45 inherit; font-family: inherit; color: var(--text);
    background: var(--bg); border: 1px solid var(--line); border-radius: 10px;
    padding: 11px 12px;
  }
  #textInput:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  .dlgrow {
    display: flex; align-items: center; gap: 10px; margin-top: 14px;
  }
  .dlgrow .spacer { flex: 1; }
  .btn {
    border: 0; border-radius: 10px; padding: 11px 18px;
    font: 600 14px inherit; font-family: inherit; cursor: pointer;
    background: var(--accent); color: #fff;
    -webkit-tap-highlight-color: transparent; touch-action: manipulation;
  }
  .btn:active { opacity: .8; }
  .btn.ghost { background: transparent; color: var(--muted); }
  .btn[disabled] { opacity: .5; }

  /* ---- message feed ---- */
  ul.msgs { list-style: none; margin: 0; padding: 0; }
  ul.msgs li {
    padding: 11px 0; border-top: 1px solid var(--line);
  }
  ul.msgs li:first-child { border-top: 0; }
  ul.msgs li.fresh { animation: flash 1.8s ease-out; }
  ul.msgs .msgtop { display: flex; align-items: baseline; gap: 8px; margin-bottom: 4px; }
  ul.msgs .msgbody {
    font-size: 14.5px; white-space: pre-wrap; word-break: break-word;
  }
  ul.msgs .copied {
    font-size: 11px; font-weight: 600; color: var(--accent);
    background: var(--accent-soft); padding: 2px 6px; border-radius: 4px;
  }
  .copybtn {
    margin-left: auto; flex: none;
    background: var(--bg); color: var(--muted);
    border: 1px solid var(--line); border-radius: 8px;
    font: 600 12px inherit; font-family: inherit;
    padding: 5px 11px; cursor: pointer;
    -webkit-tap-highlight-color: transparent; touch-action: manipulation;
  }
  .copybtn:active { opacity: .7; }
  .copybtn.ok { color: var(--accent); background: var(--accent-soft);
                border-color: var(--accent); }
  .copybtn.bad { color: var(--danger); background: var(--danger-soft);
                 border-color: var(--danger); }
  .switch {
    display: flex; align-items: center; gap: 9px; margin: 0 0 12px;
    padding: 10px 12px; border-radius: 10px; background: var(--bg);
    font-size: 13px; color: var(--muted);
  }
  .switch input { width: 17px; height: 17px; accent-color: var(--accent); flex: none; }
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
  ul.feed li .rowtop { display: flex; align-items: baseline; gap: 9px; }
  ul.feed li .rowbot {
    display: flex; align-items: center; justify-content: space-between;
    gap: 10px; margin-top: 6px;
  }
  .feedsize {
    font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums;
    min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .dl {
    flex: none; background: var(--accent-soft); color: var(--accent);
    text-decoration: none; font-size: 13px; font-weight: 600;
    padding: 8px 14px; border-radius: 9px; white-space: nowrap;
    -webkit-tap-highlight-color: transparent; touch-action: manipulation;
  }
  .dl:active { opacity: .65; }
  .delbtn {
    flex: none; background: transparent; color: var(--muted);
    border: 1px solid var(--line); border-radius: 9px;
    font: 600 12px inherit; font-family: inherit;
    padding: 8px 12px; cursor: pointer; margin-left: 6px;
    -webkit-tap-highlight-color: transparent; touch-action: manipulation;
  }
  .delbtn:active { opacity: .7; }
  .delbtn.arm { color: var(--danger); background: var(--danger-soft);
                border-color: var(--danger); }
  .delall {
    margin-top: 12px; background: transparent; color: var(--muted);
    border: 1px solid var(--line); border-radius: 9px;
    font: 600 13px inherit; font-family: inherit;
    padding: 9px 14px; cursor: pointer;
    -webkit-tap-highlight-color: transparent; touch-action: manipulation;
  }
  .delall.arm { color: var(--danger); background: var(--danger-soft);
                border-color: var(--danger); }
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

  /* ---- lock screen ---- */
  #lock {
    position: fixed; inset: 0; z-index: 50;
    display: flex; align-items: center; justify-content: center;
    background: var(--bg); padding: 20px;
  }
  #lock[hidden] { display: none; }
  .lockbox { width: min(360px, 100%); text-align: center; }
  .lockbox .glyph { font-size: 34px; margin-bottom: 10px; }
  .lockbox h2 { font-size: 19px; margin: 0 0 4px; }
  .lockbox p { color: var(--muted); font-size: 13px; margin: 0 0 18px; }
  #pinInput {
    width: 100%; text-align: center; letter-spacing: 10px;
    font: 600 26px inherit; font-family: inherit; color: var(--text);
    background: var(--card); border: 1px solid var(--line);
    border-radius: 12px; padding: 14px 12px 14px 22px;
  }
  #pinInput:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  #pinMsg {
    min-height: 20px; font-size: 13px; margin: 10px 0 0; color: var(--danger);
  }
</style>
</head>
<body>
<div id="lock">
  <div class="lockbox">
    <div class="glyph">&#128274;</div>
    <h2>Teleport Share is locked</h2>
    <p>Enter the PIN to unlock this device.</p>
    <input type="text" id="pinInput" inputmode="numeric" autocomplete="off"
           autocapitalize="off" spellcheck="false" maxlength="12"
           placeholder="&bull;&bull;&bull;&bull;&bull;&bull;">
    <p id="pinMsg" role="alert"></p>
    <button type="button" class="btn" id="pinGo">Unlock</button>
  </div>
</div>

<div class="wrap">
  <header>
    <h1>Teleport Share</h1>
    <button class="iconbtn" id="textBtn" aria-label="Send text" title="Send text">&#9998;</button>
    <span class="badge" id="status">connecting&hellip;</span>
  </header>

  <div class="card">
    <h2>Messages</h2>
    <label class="switch" id="clipRow" hidden>
      <input type="checkbox" id="clipToggle">
      <span>Copy text from the phone to the Windows clipboard</span>
    </label>
    <ul class="msgs" id="msgs"></ul>
    <p class="empty" id="msgsEmpty">No messages yet.</p>
    <button type="button" class="delall" id="clearMsgs" hidden>Clear all messages</button>
  </div>

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
    <h2>Files on the laptop</h2>
    <ul class="feed" id="feed"></ul>
    <p class="empty" id="feedEmpty">Nothing shared yet.</p>
    <button type="button" class="delall" id="delAll" hidden>Delete all files</button>
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

<dialog id="textDlg">
  <div class="dlg">
    <h3>Send text</h3>
    <p class="hint" id="textDest">It appears on the other screen straight away.</p>
    <textarea id="textInput" placeholder="Type a message..."></textarea>
    <div class="dlgrow">
      <span class="spacer"></span>
      <button type="button" class="btn ghost" id="textCancel">Cancel</button>
      <button type="button" class="btn" id="textSend">Send</button>
    </div>
  </div>
</dialog>

<script>
(function () {
  'use strict';

  // ---- lock screen ----
  var TOKEN_KEY = 'pocketShare.token';
  var token = null;
  try { token = localStorage.getItem(TOKEN_KEY); } catch (err) { token = null; }

  var lock = document.getElementById('lock');
  var pinInput = document.getElementById('pinInput');
  var pinMsg = document.getElementById('pinMsg');
  var pinGo = document.getElementById('pinGo');

  function showLock(message) {
    lock.hidden = false;
    if (message) pinMsg.textContent = message;
    try { pinInput.focus(); pinInput.select(); } catch (err) {}
  }

  function unlock() {
    var entered = pinInput.value.trim();
    if (!entered) return;
    pinGo.disabled = true;
    pinMsg.textContent = '';
    fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin: entered })
    })
      .then(function (r) {
        return r.json().then(function (d) { return { ok: r.ok, d: d }; });
      })
      .then(function (res) {
        if (!res.ok || !res.d.ok) {
          throw new Error(res.d.detail || 'That PIN is not correct.');
        }
        token = res.d.token || '';
        try { localStorage.setItem(TOKEN_KEY, token); } catch (err) {}
        pinInput.value = '';
        lock.hidden = true;
        start();
      })
      .catch(function (err) {
        pinMsg.textContent = err.message;
        pinInput.value = '';
        try { pinInput.focus(); } catch (e) {}
      })
      .then(function () { pinGo.disabled = false; });
  }

  pinGo.addEventListener('click', unlock);
  pinInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); unlock(); }
  });

  // One place that attaches the token and notices a locked session, so no
  // request can accidentally go out unauthenticated.
  function api(url, options) {
    options = options || {};
    options.headers = options.headers || {};
    if (token) options.headers['X-Auth-Token'] = token;
    return fetch(url, options).then(function (r) {
      if (r.status === 401) {
        token = null;
        try { localStorage.removeItem(TOKEN_KEY); } catch (err) {}
        showLock('Session expired - enter the PIN again.');
        throw new Error('locked');
      }
      return r;
    });
  }

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
      if (token) xhr.setRequestHeader('X-Auth-Token', token);

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
        if (xhr.status === 401) {
          token = null;
          try { localStorage.removeItem(TOKEN_KEY); } catch (err) {}
          showLock('Session expired - enter the PIN again.');
          resolve();
          return;
        }
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

  // ---- text: icon button -> modal -> send ----
  var textBtn = document.getElementById('textBtn');
  var dlg = document.getElementById('textDlg');
  var textInput = document.getElementById('textInput');
  var textSend = document.getElementById('textSend');
  var textCancel = document.getElementById('textCancel');
  var textDest = document.getElementById('textDest');
  var msgs = document.getElementById('msgs');
  var msgsEmpty = document.getElementById('msgsEmpty');
  var lastMsgSig = '';
  var seenMsgs = {};
  var clipRow = document.getElementById('clipRow');
  var clipToggle = document.getElementById('clipToggle');
  var clearMsgs = document.getElementById('clearMsgs');

  textBtn.addEventListener('click', function () {
    textDest.textContent = isPhone
      ? 'It appears on the laptop straight away.'
      : 'It appears on the phone straight away.';
    dlg.showModal();
    textInput.focus();
  });
  textCancel.addEventListener('click', function () { dlg.close(); });
  dlg.addEventListener('close', function () { textInput.value = ''; });

  function sendText() {
    var text = textInput.value.trim();
    if (!text) return;
    textSend.disabled = true;
    api('/api/text', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok || !res.d.ok) throw new Error(res.d.error || 'send failed');
        textInput.value = '';
        dlg.close();
      })
      .catch(function (err) {
        textDest.textContent = 'Could not send: ' + err.message;
      })
      .then(function () { textSend.disabled = false; });
  }

  textSend.addEventListener('click', sendText);
  textInput.addEventListener('keydown', function (e) {
    // Enter sends, Ctrl+Enter makes a new line - the usual chat convention.
    if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey) {
      e.preventDefault();
      sendText();
    }
  });

  clipToggle.addEventListener('change', function () {
    api('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clipboard_autocopy: clipToggle.checked })
    }).catch(function () { clipToggle.checked = !clipToggle.checked; });
  });

  function loadSettings() {
    return api('/api/settings')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        // Only the laptop has a Windows clipboard worth filling.
        clipRow.hidden = !isPhone;
        clipToggle.checked = !!d.clipboard_autocopy;
      });
  }

  // ---- copying text on this device ----
  // navigator.clipboard only works in a secure context. localhost counts as
  // one, but http://192.168.x.x on the phone does not - so there has to be a
  // fallback that works over plain HTTP, using a throwaway textarea.
  function legacyCopy(text) {
    var area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;' +
      'padding:0;border:none;outline:none;box-shadow:none;background:transparent;opacity:0';
    document.body.appendChild(area);
    area.focus();
    area.select();
    // iOS needs an explicit range, or the selection is empty and nothing copies.
    try { area.setSelectionRange(0, text.length); } catch (err) {}
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
    document.body.removeChild(area);
    return ok;
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text)
        .then(function () { return true; })
        .catch(function () { return legacyCopy(text); });
    }
    return Promise.resolve(legacyCopy(text));
  }

  var clearMsgs = document.getElementById('clearMsgs');
  armOnce(clearMsgs, function () {
    if (!window.confirm(
      'Clear all messages?\n\n' +
      'This deletes every saved .txt under Texts\\ and empties the message ' +
      'history on both screens. Shared files are not touched.\n\n' +
      'This cannot be undone.')) {
      return Promise.reject(new Error('cancelled'));
    }
    return api('/api/texts', { method: 'DELETE' })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (!d.ok) throw new Error(d.error || 'failed'); });
  }, 'Really clear?', 'Clear all messages');

  function renderMsgs(items) {
    var sig = items.map(function (t) { return t.id; }).join(',');
    if (sig === lastMsgSig) return;
    lastMsgSig = sig;

    msgsEmpty.style.display = items.length ? 'none' : 'block';
    clearMsgs.hidden = items.length === 0;
    msgs.textContent = '';
    items.forEach(function (t) {
      var li = document.createElement('li');
      if (seenMsgs[t.id]) li.className = 'fresh';
      seenMsgs[t.id] = true;

      var top = document.createElement('div');
      top.className = 'msgtop';
      var tag = document.createElement('span');
      tag.className = 'fdir ' + t.sender;
      tag.textContent = t.sender === 'phone' ? 'PHONE' : 'LAPTOP';
      var when = document.createElement('span');
      when.className = 'feedsize';
      when.textContent = clockOf(t.ts);
      top.appendChild(tag);
      top.appendChild(when);
      if (t.clipboard && t.clipboard.copied) {
        var cp = document.createElement('span');
        cp.className = 'copied';
        cp.textContent = 'pasted-ready';
        top.appendChild(cp);
      }

      var copy = document.createElement('button');
      copy.type = 'button';
      copy.className = 'copybtn';
      copy.textContent = 'Copy';
      copy.addEventListener('click', function () {
        var button = this;
        button.disabled = true;
        copyText(t.text).then(function (ok) {
          button.disabled = false;
          button.textContent = ok ? 'Copied' : 'Press & hold';
          button.classList.add(ok ? 'ok' : 'bad');
          setTimeout(function () {
            button.textContent = 'Copy';
            button.classList.remove('ok', 'bad');
          }, 1800);
        });
      });
      top.appendChild(copy);

      var body = document.createElement('div');
      body.className = 'msgbody';    // textContent, so text is never markup
      body.textContent = t.text;

      li.appendChild(top);
      li.appendChild(body);
      msgs.appendChild(li);
    });
  }

  function pollMsgs() {
    api('/api/texts?limit=20')
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d.ok) renderMsgs(d.texts || []); })
      .catch(function () {});
  }

  // ---- files on the laptop: polled so an upload shows up on its own ----
  var feed = document.getElementById('feed');
  var feedEmpty = document.getElementById('feedEmpty');
  var seen = {};
  var seenCount = 0;
  var primed = false;
  var lastSignature = '';
  var isPhone = false;

  // Files the phone has already saved, remembered across page reloads so a
  // reload never re-downloads the whole history.
  var SAVED_KEY = 'pocketShare.saved';

  function loadSaved() {
    try {
      return JSON.parse(localStorage.getItem(SAVED_KEY)) || [];
    } catch (err) {
      return [];
    }
  }

  function markSaved(relPath) {
    try {
      var list = loadSaved();
      if (list.indexOf(relPath) === -1) list.push(relPath);
      if (list.length > 300) list = list.slice(-300);
      localStorage.setItem(SAVED_KEY, JSON.stringify(list));
    } catch (err) {
      // Private mode / storage disabled: we just re-download next reload.
    }
  }

  // Save the file without asking. Using an anchor with the download attribute
  // lets the browser stream it natively - the file never passes through JS
  // memory, so size does not matter.
  function autoSave(item) {
    try {
      var link = document.createElement('a');
      link.href = '/api/download?path=' + encodeURIComponent(item.rel_path);
      link.download = item.name;
      link.style.display = 'none';
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    } catch (err) {
      // Some browsers refuse downloads with no user gesture. The Download
      // button on the row is still there, so nothing is lost.
    }
  }

  function signatureOf(items) {
    // Rebuilding the list every 2s would replace the DOM nodes underneath the
    // user's finger, so a tap on Download can land on a detached element and
    // do nothing. Nothing is re-rendered unless something actually changed.
    var parts = [];
    for (var i = 0; i < items.length; i++) {
      parts.push(items[i].rel_path + '|' + items[i].mtime + '|' + items[i].size);
    }
    return parts.join(',');
  }

  function clockOf(ts) {
    try {
      return new Date(ts * 1000).toLocaleTimeString([], {
        hour: '2-digit', minute: '2-digit'
      });
    } catch (err) {
      return '';
    }
  }

  // ---- deleting ----
  // Deleting is easy to do by accident, especially on a phone. Every delete
  // asks twice: first tap turns the button red, second tap actually does it.
  function armOnce(button, action, question, resetLabel) {
    var armed = false;
    var timer = null;
    resetLabel = resetLabel || 'Delete';
    button.addEventListener('click', function () {
      if (!armed) {
        armed = true;
        button.classList.add('arm');
        button.textContent = question;
        timer = setTimeout(function () {
          armed = false;
          button.classList.remove('arm');
          button.textContent = resetLabel;
        }, 4000);
        return;
      }
      clearTimeout(timer);
      button.disabled = true;
      action()
        .catch(function (err) { alert('Could not delete: ' + err.message); })
        .then(function () {
          // The poll will redraw the list without it either way.
          button.disabled = false;
        });
    });
  }

  var delAll = document.getElementById('delAll');
  armOnce(delAll, function () {
    if (!window.confirm(
      'Delete every shared file?\n\n' +
      'This removes them from the folder and from the history on both ' +
      'screens. It cannot be undone.\n\n' +
      'Saved messages under Texts\\ are kept.')) {
      return Promise.reject(new Error('cancelled'));
    }
    return api('/api/files', { method: 'DELETE' })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (!d.ok) throw new Error(d.error || 'failed'); });
  }, 'Really delete all?');

  function renderFeed(items) {
    var signature = signatureOf(items);
    if (signature === lastSignature) return;
    lastSignature = signature;

    delAll.hidden = items.length === 0;
    feedEmpty.style.display = items.length ? 'none' : 'block';
    feed.textContent = '';
    items.forEach(function (it) {
      // These come from the disk, not the index, so there is no id to track -
      // name plus mtime identifies "this is the version I have not seen".
      var key = it.rel_path + '|' + it.mtime;
      var li = document.createElement('li');
      if (primed && !seen[key]) li.className = 'fresh';

      var top = document.createElement('div');
      top.className = 'rowtop';
      var tag = document.createElement('span');
      tag.className = 'fdir ' + it.sender;
      tag.textContent = it.sender === 'phone' ? 'PHONE' : 'LAPTOP';
      var name = document.createElement('span');
      name.className = 'feedname';
      name.textContent = it.name;             // never innerHTML: name is user input
      name.title = it.rel_path;
      top.appendChild(tag);
      top.appendChild(name);

      var bot = document.createElement('div');
      bot.className = 'rowbot';
      var meta = document.createElement('span');
      meta.className = 'feedsize';
      meta.textContent = fmt(it.size || 0) + '  ·  ' + clockOf(it.mtime) +
        (it.manual ? '  ·  copied in by hand' : '');
      var link = document.createElement('a');
      link.className = 'dl';
      link.textContent = 'Download';
      link.href = '/api/download?path=' + encodeURIComponent(it.rel_path);
      link.setAttribute('download', it.name);

      var del = document.createElement('button');
      del.type = 'button';
      del.className = 'delbtn';
      del.textContent = 'Delete';
      del.setAttribute('aria-label', 'Delete ' + it.name);
      armOnce(del, function () {
        return api('/api/file?path=' + encodeURIComponent(it.rel_path),
                   { method: 'DELETE' })
          .then(function (r) { return r.json(); })
          .then(function (d) { if (!d.ok) throw new Error(d.error || 'failed'); });
      }, 'Really delete?');

      bot.appendChild(meta);
      bot.appendChild(link);
      bot.appendChild(del);

      li.appendChild(top);
      li.appendChild(bot);
      feed.appendChild(li);

      if (!seen[key]) { seen[key] = true; seenCount++; }
    });
    // Keep the memory-tracking map from growing forever.
    if (seenCount > 300) { seen = {}; seenCount = 0; primed = true; }
    primed = true;

    // On the phone, anything the laptop shared is saved straight away - no tap,
    // no confirmation. Files the phone sent are skipped: it already has them.
    if (isPhone) {
      var saved = loadSaved();
      items.forEach(function (it) {
        if (it.sender === 'phone') return;
        if (saved.indexOf(it.rel_path) !== -1) return;
        markSaved(it.rel_path);
        autoSave(it);
      });
    }
  }

  function poll() {
    api('/api/files?limit=200')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) throw new Error('bad response');
        setStatus('connected', false);
        isPhone = (d.sender !== 'laptop');
        renderFeed(d.files || []);
      })
      .catch(function () { setStatus('disconnected', true); });
  }

  function pollAll() {
    poll();
    pollMsgs();
  }

  function pollInfo() {
    api('/api/info')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        setStatus('connected', false);
      var who = d.sender === 'laptop' ? 'Laptop' : 'Phone';
      isPhone = (d.sender !== 'laptop');
      loadSettings();
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
  }

  // Nothing polls until this device has unlocked, so a locked page does not
  // fire a request every two seconds just to be told it is locked.
  var running = false;

  function start() {
    if (running) return;
    running = true;
    pollAll();
    pollInfo();
    setInterval(pollAll, 2000);
    setInterval(pollInfo, 15000);
  }

  // If we already hold a token, try to use it; otherwise show the lock screen.
  if (token) {
    api('/api/info')
      .then(function () {
        lock.hidden = true;   // this device already has a valid session
        start();
      })
      .catch(function () { showLock('Enter the PIN to unlock.'); });
  } else {
    showLock();
  }
})();
</script>
</body>
</html>"""




# Entry point must stay last: main() serves PAGE_HTML, so the page string has
# to exist before the first request can arrive.
if __name__ == "__main__":
    sys.exit(main())
