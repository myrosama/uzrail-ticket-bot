"""Live link between this PC and the Mini App, so the phone sees everything the desktop window shows and can act on it.

Every few seconds the bot sends its status through a small Cloudflare Worker (tools/logs-worker) and collects any
commands from the Mini App. Both directions are encrypted with AES-GCM. The key travels only in the Mini App's Telegram
link (in the part after #), so the Worker stores data it cannot read.
"""
from __future__ import annotations

import base64
import collections
import json
import logging
import os
import secrets
import threading
import time

import requests
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import __version__, crypto, telemetry
from .engine import today_tashkent
from .recorder import rec

WORKER = "https://uzrail-logs.sadrikov49.workers.dev/live/"
VIEWING, IDLE, OFFLINE = 2, 10, 30         # seconds between updates: Mini App open, closed, no internet
MAX_AGE = 300                              # a command older than this is ignored
COMMANDS = {"trip", "toggle", "del", "batch", "pax", "tickets", "pause", "cancel", "transfer", "report", "sharelogs",
            "autoupdate"}                  # the railway password never goes this way
QUIET = {"check", "http", "ws", "heartbeat", "session_start", "notify", "plan", "miniapp"}

log = logging.getLogger("uzrailbot.remote")


def seal(key: bytes, obj) -> str:
    nonce = os.urandom(12)
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, json.dumps(obj, ensure_ascii=False).encode("utf-8"), None)).decode()


def unseal(key: bytes, text: str):
    raw = base64.b64decode(text)
    return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], None))


class Remote:
    def __init__(self, store, bot):
        self.store, self.bot = store, bot
        self.activity = collections.deque(maxlen=80)
        self.seen = collections.deque(maxlen=200)      # commands already run
        self.http = requests.Session()
        if not store.cfg.get("live"):
            store.cfg["live"] = {"id": secrets.token_hex(16), "token": crypto.protect(secrets.token_hex(24)),
                                 "key": crypto.protect(base64.b64encode(os.urandom(32)).decode())}
            store.save()
        live = store.cfg["live"]
        self.id, self.token, self.key = live["id"], crypto.unprotect(live["token"]), base64.b64decode(crypto.unprotect(live["key"]))
        self.base = store.cfg.get("live_url") or WORKER     # someone can point this at their own Worker
        rec.listeners.append(self._on_event)

    def start(self):
        threading.Thread(target=self._run, name="remote", daemon=True).start()

    def link(self) -> dict:
        """What the Mini App needs to connect."""
        return {"u": self.base, "i": self.id, "k": base64.urlsafe_b64encode(self.key).decode().rstrip("=")}

    def _on_event(self, row):
        if row["kind"] not in QUIET:
            self.activity.append(row)

    def payload(self) -> dict:
        engine = self.bot.engine
        snap = engine.snapshot()
        state = self.bot.miniapp_state()
        state.update(status={"state": snap["state"], "detail": snap["detail"], "lastCheck": snap.get("last_check") or 0},
                     board=snap["board"], stats=snap["stats"], activity=list(self.activity), now=time.time(), today=today_tashkent(),
                     paused=bool(self.store.cfg.get("paused")), version=__version__, shareLogs=telemetry.enabled(self.store),
                     autoUpdate=bool(self.store.cfg.get("auto_update", True)),
                     targets={h["order"]: engine.transfer_targets(h["order"]) for h in snap["holds"]})
        return state

    def _run(self):
        while not self.bot.stop_ev.is_set():
            wait = IDLE
            try:
                r = self.http.put(self.base + self.id, data=seal(self.key, self.payload()), timeout=10,
                                  headers={"x-live-token": self.token, "Content-Type": "text/plain"})
                if r.status_code == 200:
                    j = r.json()
                    wait = VIEWING if j.get("viewing") else IDLE
                    for text in j.get("cmds") or []:
                        threading.Thread(target=self.command, args=(text,), daemon=True).start()
                else:
                    log.info("live link refused: HTTP %s", r.status_code)
                    wait = OFFLINE
            except (requests.RequestException, ValueError):
                wait = OFFLINE
            except Exception:
                log.exception("live link")
                wait = OFFLINE
            self.bot.stop_ev.wait(wait)

    def command(self, text: str) -> bool:
        """Run one command from the Mini App. Only someone with the key can write one; old or repeated ones are ignored."""
        try:
            cmd = unseal(self.key, text)
        except (InvalidTag, ValueError, KeyError):
            log.info("live link: ignored a command that could not be decrypted")
            return False
        nonce = cmd.pop("n", None)
        if not isinstance(cmd, dict) or cmd.get("a") not in COMMANDS or not nonce or nonce in self.seen \
                or abs(time.time() - float(cmd.pop("ts", 0))) > MAX_AGE:
            return False
        self.seen.append(nonce)
        try:
            self.bot._webapp({"web_app_data": {"data": json.dumps(cmd)}})
        except Exception:
            log.exception("live command")
            return False
        return True
