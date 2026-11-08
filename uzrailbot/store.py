"""Settings (config.json) and runtime state (state.json), thread-safe. Secrets are stored protected."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import threading
import uuid

from . import crypto, paths
from .classes import is_class_code

DEFAULT_WEBAPP_URL = "https://myrosama.github.io/uzrail-ticket-bot/"
MAX_PASSENGERS = 4          # the railway allows 4 tickets per order
MAX_DATES = 30

CONFIG_DEFAULTS = {
    "version": 1,
    "telegram": {"token": "", "owner_id": None, "owner_name": "", "bot_username": ""},
    "railway": {"email": "", "password": ""},
    "webapp_url": DEFAULT_WEBAPP_URL,
    "paused": False,
    "auto_update": True,
    "share_logs": True,           # send session logs to the developer (telemetry.py); off in Settings
    "install_id": "",
    "trips": [],
    "transport_key": "",
}
STATE_DEFAULTS = {"holds": {}, "done": {}, "pending": [], "cars": {}, "tg_offset": 0}


def _merge(defaults: dict, loaded: dict) -> dict:
    out = copy.deepcopy(defaults)
    for k, v in (loaded or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read(path, defaults):
    try:
        with open(path, encoding="utf-8") as f:
            return _merge(defaults, json.load(f))
    except FileNotFoundError:
        return copy.deepcopy(defaults)
    except (OSError, ValueError):
        bad = str(path) + ".broken"
        try:
            os.replace(path, bad)
        except OSError:
            pass
        return copy.deepcopy(defaults)


def _write(path, data):
    # One-shot dumps without indent runs in C without letting other threads in; json.dump (and indent) encodes
    # piece by piece, and a booking thread adding a key meanwhile crashed the save ("dictionary changed size").
    text = json.dumps(data, ensure_ascii=False)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def normalize_trip(t: dict) -> dict:
    """Validate a trip coming from the Mini App. Raises ValueError with a readable message."""
    def station(s, what):
        if not isinstance(s, dict) or not str(s.get("code", "")).isdigit():
            raise ValueError(f"{what} station is missing")
        return {"code": str(s["code"]), "name": str(s.get("name") or s["code"])[:40]}

    today = dt.date.today()
    dates = []
    for d in t.get("dates") or []:
        try:
            day = dt.date.fromisoformat(str(d)[:10])
        except ValueError:
            continue
        if day >= today and day.isoformat() not in dates:
            dates.append(day.isoformat())
    dates.sort()
    if not dates:
        raise ValueError("pick at least one date from today on")
    if len(dates) > MAX_DATES:
        raise ValueError(f"at most {MAX_DATES} dates per trip")
    classes = [c for c in (t.get("classes") or []) if is_class_code(c)]
    mode = "buy" if t.get("mode") == "buy" else "notify"
    passengers = [str(p) for p in (t.get("passengers") or [])][:MAX_PASSENGERS]
    if mode == "buy" and not passengers:
        raise ValueError("auto-buy needs at least one passenger")
    frm, to = station(t.get("from"), "Departure"), station(t.get("to"), "Arrival")
    if frm["code"] == to["code"]:
        raise ValueError("departure and arrival are the same station")
    try:
        max_price = max(0, int(t.get("maxPrice") or 0))
    except (TypeError, ValueError):
        max_price = 0
    try:
        speed = min(4, max(1, int(t.get("speed") or 1)))
    except (TypeError, ValueError):
        speed = 1
    # only these train numbers (empty = every train on the route); older configs had one "train" string
    raw = t.get("trains")
    if raw is None:
        raw = [t["train"]] if str(t.get("train") or "").strip() else []
    trains = []
    for n in raw if isinstance(raw, list) else []:
        n = str(n).strip()[:8]
        if n and n not in trains:
            trains.append(n)
    trains = trains[:20]
    return {
        "id": str(t.get("id") or uuid.uuid4().hex[:8]),
        "from": frm,
        "to": to,
        "dates": dates,
        "classes": classes,
        "maxPrice": max_price,
        "mode": mode,
        "passengers": passengers,
        "speed": speed,
        "trains": trains,
        "together": bool(t.get("together", False)),
        "active": bool(t.get("active", True)),
    }


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.cfg = _read(paths.config_path(), CONFIG_DEFAULTS)
        self.state = _read(paths.state_path(), STATE_DEFAULTS)
        # trips saved by older versions get the current fields (e.g. v0.2 "train" -> v0.3 "trains")
        upgraded = []
        for t in self.cfg.get("trips") or []:
            try:
                upgraded.append(normalize_trip(t))
            except (ValueError, KeyError, TypeError):
                upgraded.append(dict(t, trains=t.get("trains") or []))
        self.cfg["trips"] = upgraded

    def save(self):
        with self.lock:
            _write(paths.config_path(), self.cfg)
            _write(paths.state_path(), self.state)

    # ---------------------------------------------------------------- telegram
    def bot_token(self) -> str:
        return crypto.unprotect(self.cfg["telegram"]["token"])

    def set_telegram(self, token: str, owner_id: int, owner_name: str, bot_username: str):
        with self.lock:
            self.cfg["telegram"].update(token=crypto.protect(token), owner_id=int(owner_id),
                                        owner_name=owner_name, bot_username=bot_username)
            self.save()

    @property
    def owner_id(self):
        return self.cfg["telegram"].get("owner_id")

    def is_configured(self) -> bool:
        return bool(self.cfg["telegram"]["token"] and self.owner_id)

    # ---------------------------------------------------------------- railway account
    def railway_login(self):
        r = self.cfg["railway"]
        return r.get("email", ""), crypto.unprotect(r.get("password", ""))

    def set_railway_login(self, email: str, password: str):
        with self.lock:
            self.cfg["railway"] = {"email": email.strip(), "password": crypto.protect(password)}
            self.save()

    def transport_key(self) -> str:
        with self.lock:
            if not self.cfg.get("transport_key"):
                self.cfg["transport_key"] = crypto.protect(crypto.new_private_key_pem())
                self.save()
            return crypto.unprotect(self.cfg["transport_key"])

    # ---------------------------------------------------------------- trips
    def trips(self) -> list:
        with self.lock:
            return copy.deepcopy(self.cfg["trips"])

    def trip(self, trip_id: str):
        with self.lock:
            return next((copy.deepcopy(t) for t in self.cfg["trips"] if t["id"] == trip_id), None)

    def upsert_trip(self, trip: dict) -> dict:
        t = normalize_trip(trip)
        with self.lock:
            for i, old in enumerate(self.cfg["trips"]):
                if old["id"] == t["id"]:
                    self.cfg["trips"][i] = t
                    break
            else:
                self.cfg["trips"].append(t)
            self.save()
        return t

    def delete_trip(self, trip_id: str) -> bool:
        with self.lock:
            before = len(self.cfg["trips"])
            self.cfg["trips"] = [t for t in self.cfg["trips"] if t["id"] != trip_id]
            self.state["done"].pop(trip_id, None)
            self.save()
            return len(self.cfg["trips"]) != before

    def set_trip_active(self, trip_id: str, active: bool) -> bool:
        with self.lock:
            for t in self.cfg["trips"]:
                if t["id"] == trip_id:
                    t["active"] = active
                    self.save()
                    return True
        return False

    # ---------------------------------------------------------------- passengers already ticketed per trip
    def done_passengers(self, trip_id: str) -> list:
        with self.lock:
            return list(self.state["done"].get(trip_id, []))

    def mark_done(self, trip_id: str, passenger_ids: list):
        with self.lock:
            cur = self.state["done"].setdefault(trip_id, [])
            for p in passenger_ids:
                if p not in cur:
                    cur.append(p)
            self.save()
