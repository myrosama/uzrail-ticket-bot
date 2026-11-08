"""Usage logs: finished session logs go to the developer, so bugs can be found and fixed from real runs.

On by default; Settings → "Share usage logs" turns it off. What is sent: a session's events.jsonl (every check,
booking step and error; the recorder never writes names, passport numbers or passwords), with emails, phone
numbers, passport-like numbers and links blanked out, plus the app version and the OS. A random install id groups
the sessions of one PC. The receiving end (tools/logs-worker) stores no IP address and deletes logs after 30 days.
"""
from __future__ import annotations

import gzip
import logging
import platform
import re
import secrets

import requests

from . import __version__, paths
from .recorder import rec

URL = "https://uzrail-logs.sadrikov49.workers.dev/upload"
MAX_GZ = 4 * 1024 * 1024
BACKLOG = 10                    # at most this many unsent sessions per run (the newest)
SCRUB = [
    (re.compile(r"https?://[^\s\"]+"), "<link>"),                 # pay links carry order data
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"\+?998[\d \-]{9,13}"), "<phone>"),
    (re.compile(r"\b[A-Z]{2}\d{7}\b"), "<document>"),
]

log = logging.getLogger("uzrailbot.telemetry")


def enabled(store) -> bool:
    return bool(store.cfg.get("share_logs", True))


def install_id(store) -> str:
    if not store.cfg.get("install_id"):
        store.cfg["install_id"] = secrets.token_hex(12)
        store.save()
    return store.cfg["install_id"]


def scrub(text: str) -> str:
    for rx, mask in SCRUB:
        text = rx.sub(mask, text)
    return text


def pack(text: str) -> bytes | None:
    """Gzip a session log. Very long sessions drop the per-check lines first (the booking steps matter most)."""
    gz = gzip.compress(text.encode("utf-8"))
    if len(gz) > MAX_GZ:
        text = "\n".join(line for line in text.splitlines()
                         if '"kind": "check"' not in line and '"kind": "http"' not in line)
        gz = gzip.compress(text.encode("utf-8"))
    return gz if len(gz) <= MAX_GZ else None      # shortcut: a session too big even then is skipped, not cut


def _post(store, folder, gz: bytes, timeout: float):
    return requests.post(URL, data=gz, timeout=timeout, headers={
        "Content-Type": "application/gzip", "X-Install": install_id(store), "X-Session": folder.name,
        "X-Version": __version__, "X-OS": f"{platform.system()} {platform.release()}"})


def report(store, note: str, timeout: float = 15) -> bool:
    """The user pressed "Report a problem": send this session's log with their note now. It is their explicit
    request, so it works with sharing off. Not marked as sent: the upload at close replaces this copy."""
    rec.ev("user_report", note=note[:1000])
    if rec.folder is None:
        return False
    try:
        gz = pack(scrub((rec.folder / "events.jsonl").read_text(encoding="utf-8", errors="replace")))
        return gz is not None and _post(store, rec.folder, gz, timeout).status_code < 300
    except (OSError, requests.RequestException) as e:
        log.info("problem report not sent: %s", type(e).__name__)
        return False


def upload(store, closing: bool = False, timeout: float = 10) -> int:
    """At start: send every earlier session not sent yet. When closing: send only the current one (so quitting
    waits at most `timeout`). Never raises: offline or refused means trying again at the next start.
    Returns how many were sent."""
    if not enabled(store):
        return 0
    root = paths.app_dir() / "logs" / "sessions"
    if not root.is_dir():
        return 0
    sent = 0
    for folder in sorted(p for p in root.iterdir() if p.is_dir())[-BACKLOG:]:
        events, mark = folder / "events.jsonl", folder / ".sent"
        if not events.exists() or mark.exists() or (folder == rec.folder) != closing:
            continue
        try:
            gz = pack(scrub(events.read_text(encoding="utf-8", errors="replace")))
            if gz is None:
                mark.write_text("too big")
                continue
            r = _post(store, folder, gz, timeout)
            if r.status_code < 300:
                mark.write_text("")
                sent += 1
            else:
                log.info("usage log %s not accepted: HTTP %s", folder.name, r.status_code)
        except (OSError, requests.RequestException) as e:
            log.info("usage log %s not sent: %s", folder.name, type(e).__name__)
            break                                  # offline: don't try the rest now
    return sent
