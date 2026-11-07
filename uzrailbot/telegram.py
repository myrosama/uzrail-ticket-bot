"""Telegram side: long polling (no server needed), owner-only control, guided trip setup in chat,
and every notification the engine produces (reservation with Pay link, countdown, renewals, PDF)."""
from __future__ import annotations

import base64
import calendar
import datetime as dt
import html
import json
import logging
import queue
import threading
import time

import requests

from . import crypto, eticket
from .classes import CLASSES, label
from .recorder import rec
from .store import MAX_PASSENGERS

log = logging.getLogger("uzrailbot.telegram")
API = "https://api.telegram.org/bot{token}/{method}"
H = html.escape


class TgError(Exception):
    def __init__(self, desc, code=0, retry_after=0):
        super().__init__(desc)
        self.code, self.retry_after = code, retry_after


class TgApi:
    def __init__(self, token: str):
        self.token = token
        self.s = requests.Session()

    def call(self, method: str, http_timeout: float = 30, files=None, **params):
        url = API.format(token=self.token, method=method)
        if files:
            r = self.s.post(url, data={k: v for k, v in params.items() if v is not None}, files=files, timeout=http_timeout)
        else:
            r = self.s.post(url, json={k: v for k, v in params.items() if v is not None}, timeout=http_timeout)
        try:
            j = r.json()
        except ValueError:
            raise TgError(f"HTTP {r.status_code}", r.status_code)
        if not j.get("ok"):
            raise TgError(j.get("description", "error"), j.get("error_code", r.status_code),
                          (j.get("parameters") or {}).get("retry_after", 0))
        return j["result"]


# Uzbek spellings people type -> the spelling the railway search understands (search needs >= 4 letters
# and matches inside names, but "Urganch" finds nothing while "Urgench" does).
STATION_ALIASES = {
    "toshkent": "Tashkent", "urganch": "Urgench", "buxoro": "Bukhara", "samarqand": "Samarkand",
    "qarshi": "Karshi", "jizzax": "Djizakh", "navoiy": "Navoi", "fargona": "Fergana", "andijon": "Andijan",
    "termiz": "Termez", "guliston": "Gulistan", "qoqon": "Kokand", "margilon": "Margilan", "xiva": "Khiva",
    "nukus": "Nukus", "namangan": "Namangan", "shahrisabz": "Shahrisabz", "denov": "Denau", "qongirot": "Kungrad",
    "angren": "Angren", "chirchiq": "Chirchik", "olmaliq": "Almalyk", "bekobod": "Bekabad", "zarafshon": "Zarafshan",
    "uchquduq": "Uchkuduk", "kattaqorgon": "Kattakurgan", "guzor": "Guzar", "boysun": "Baysun",
    "sariosiyo": "Sariasiya", "qumqorgon": "Kumkurgan", "jarqorgon": "Djarkurgan", "kogon": "Kagan",
    "gijduvon": "Gijduvan", "tortkol": "Turtkul", "beruniy": "Beruni", "xojayli": "Khodjeyli", "pop": "Pap",
    "yangiyer": "Yangiyer", "xovos": "Khavast", "shovot": "Shavat", "miskin": "Miskin", "kitob": "Kitab",
}


def find_stations(query: str) -> list:
    client = eticket.Client()
    found = client.stations(query) if len(query) >= 4 else []
    if found:
        return found
    key = "".join(ch for ch in query.lower() if ch.isalpha())
    for alias, official in STATION_ALIASES.items():
        if len(key) >= 3 and (alias.startswith(key) or key.startswith(alias)):
            found = client.stations(official)
            if found:
                return found
    return []


def fmt_sum(n) -> str:
    return f"{int(n or 0):,}".replace(",", " ")


def fmt_left(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


class Bot:
    """Also the engine's notifier."""

    def __init__(self, store):
        self.store = store
        self.engine = None
        self.api = TgApi(store.bot_token())
        self.out = queue.Queue()
        self.wiz = None               # the trip wizard / account dialog in progress
        self.stop_ev = threading.Event()
        self.healthy = True
        self.last_error = ""

    # ---------------------------------------------------------------- lifecycle
    def start(self, engine):
        self.engine = engine
        threading.Thread(target=self._sender, name="tg-send", daemon=True).start()
        threading.Thread(target=self._poller, name="tg-poll", daemon=True).start()

    def stop(self):
        self.stop_ev.set()

    # ---------------------------------------------------------------- sending (queued, retried)
    def send(self, text, markup=None, on_sent=None, document=None, filename="ticket.pdf"):
        self.out.put(("doc" if document else "msg", text, markup, on_sent, document, filename))

    def edit(self, msg_id, text, markup=None):
        self.out.put(("edit", text, markup, msg_id, None, None))

    def _sender(self):
        chat = self.store.owner_id
        while not self.stop_ev.is_set():
            try:
                kind, text, markup, extra, doc, fname = self.out.get(timeout=0.5)
            except queue.Empty:
                continue
            for attempt in range(60):
                try:
                    if kind == "msg":
                        res = self.api.call("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
                                            reply_markup=markup, link_preview_options={"is_disabled": True})
                        if extra:
                            extra(res["message_id"])
                    elif kind == "edit":
                        self.api.call("editMessageText", chat_id=chat, message_id=extra, text=text, parse_mode="HTML",
                                      reply_markup=markup, link_preview_options={"is_disabled": True})
                    else:
                        self.api.call("sendDocument", http_timeout=60, files={"document": (fname, doc, "application/pdf")},
                                      chat_id=chat, caption=text, parse_mode="HTML")
                    self.healthy = True
                    break
                except TgError as e:
                    if e.retry_after:
                        time.sleep(e.retry_after)
                        continue
                    if "not modified" in str(e) or e.code == 400:
                        if "not modified" not in str(e):
                            log.warning("telegram refused a message: %s", e)
                        break
                    self.healthy, self.last_error = False, str(e)
                    if e.code in (401, 403):
                        log.error("telegram: %s (bot token revoked or bot blocked)", e)
                        time.sleep(30)
                        continue
                    time.sleep(min(60, 2 ** attempt))
                except requests.RequestException as e:
                    self.healthy, self.last_error = False, type(e).__name__
                    time.sleep(min(60, 2 ** attempt))

    # ---------------------------------------------------------------- receiving
    def _poller(self):
        try:
            self.api.call("deleteWebhook", drop_pending_updates=False)
            self.api.call("setMyCommands", commands=[
                {"command": "new", "description": "Watch a new trip"},
                {"command": "trips", "description": "My trips"},
                {"command": "status", "description": "What the bot is doing"},
                {"command": "account", "description": "Railway site login"},
                {"command": "pause", "description": "Pause everything"},
                {"command": "resume", "description": "Resume"},
                {"command": "help", "description": "How it works"},
            ])
        except Exception as e:
            log.warning("telegram setup calls: %s", e)
        offset = int(self.store.state.get("tg_offset") or 0)
        while not self.stop_ev.is_set():
            try:
                ups = self.api.call("getUpdates", http_timeout=35, timeout=25, offset=offset, limit=50,
                                    allowed_updates=["message", "callback_query"])
                self.healthy = True
            except TgError as e:
                self.healthy, self.last_error = False, str(e)
                time.sleep(e.retry_after or 5)
                continue
            except requests.RequestException as e:
                self.healthy, self.last_error = False, type(e).__name__
                time.sleep(3)
                continue
            for u in ups:
                offset = u["update_id"] + 1
                try:
                    self._handle(u)
                except Exception:
                    log.exception("handling a telegram update")
            if ups:
                self.store.state["tg_offset"] = offset
                self.store.save()

    def _handle(self, u):
        msg = u.get("message")
        cb = u.get("callback_query")
        who = (msg or cb or {}).get("from", {}).get("id")
        if who != self.store.owner_id:
            if msg:
                self.api.call("sendMessage", chat_id=msg["chat"]["id"], text="This is a private bot.")
            return
        if cb:
            self._callback(cb)
        elif msg and msg.get("web_app_data"):
            self._webapp(msg)
        elif msg and msg.get("text"):
            self._text(msg)

    # ---------------------------------------------------------------- keyboards
    def miniapp_url(self) -> str:
        """The Mini App link, with the current state in the #fragment (no server needed)."""
        email, pw = self.store.railway_login()
        state = {
            "v": 1, "bot": self.store.cfg["telegram"].get("bot_username", ""), "email": email, "hasPw": bool(pw),
            "key": crypto.public_key_b64(self.store.transport_key()),
            "pax": [{"i": p["id"], "n": p["name"]} for p in (self.engine.passenger_list() if self.engine else [])],
            "trips": [{k: t[k] for k in ("id", "from", "to", "dates", "classes", "maxPrice", "mode", "passengers",
                                         "speed", "train", "together", "active")} for t in self.store.trips()],
        }
        raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        frag = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return self.store.cfg.get("webapp_url", "").rstrip("/") + "/#s=" + frag

    def main_kb(self):
        paused = self.store.cfg.get("paused")
        first = [{"text": "🚆 Trips & settings", "web_app": {"url": self.miniapp_url()}}]
        return {"keyboard": [first,
                             [{"text": "📊 Status"}, {"text": "▶️ Resume" if paused else "⏸ Pause"}]],
                "resize_keyboard": True, "is_persistent": True}

    # ---------------------------------------------------------------- data coming back from the Mini App
    def _webapp(self, msg):
        try:
            d = json.loads(msg["web_app_data"]["data"])
        except (KeyError, ValueError):
            return self.send("⚠️ I couldn't read what the app sent. Please try again.", self.main_kb())
        act = d.get("a")
        rec.ev("miniapp", action=act)
        if act == "trip":
            try:
                t = self.store.upsert_trip(d.get("trip") or {})
            except ValueError as e:
                return self.send(f"⚠️ Not saved: {H(str(e))}.", self.main_kb())
            rec.ev("trip_saved", trip=t["id"], mode=t["mode"], dates=t["dates"], classes=t["classes"],
                   max_price=t["maxPrice"], passengers=len(t["passengers"]), speed=t["speed"],
                   route=[t["from"]["code"], t["to"]["code"]])
            return self.send("✅ <b>Saved. Watching now.</b>\n\n" + self._summary(t), self.main_kb())
        if act == "toggle":
            self.store.set_trip_active(str(d.get("id")), bool(d.get("active")))
            return self.send("▶️ Trip resumed." if d.get("active") else "⏸ Trip paused.", self.main_kb())
        if act == "del":
            self.store.delete_trip(str(d.get("id")))
            return self.send("🗑 Trip deleted.", self.main_kb())
        if act == "account":
            try:
                password = crypto.decrypt_from_miniapp(self.store.transport_key(), d.get("pw", ""))
            except Exception:
                return self.send("⚠️ I couldn't decrypt the password. Open the app from the latest keyboard "
                                 "button and try again.", self.main_kb())
            email = str(d.get("email", "")).strip()
            self.send("🔐 Checking your railway login…")
            threading.Thread(target=self._check_account, args=(email, password), daemon=True).start()
            return
        self.send("⚠️ Unknown request from the app.", self.main_kb())

    def _check_account(self, email, password):
        c = eticket.Client(email, password)
        try:
            c.login()
            friends = c.friends()
        except eticket.EticketError as e:
            return self.send(f"❌ Login failed: {H(str(e))}", self.main_kb())
        self.store.set_railway_login(email, password)
        self.engine.account_changed()
        self.engine._ensure_client()
        names = ", ".join(H(f"{f.get('firstname', '')} {f.get('lastname', '')}") for f in friends) or "none yet"
        rec.ev("account_saved", passengers=len(friends))
        self.send(f"✅ Connected as <b>{H(email)}</b>.\nSaved passengers: {names}\n\n"
                  "Now open 🚆 Trips & settings and add a trip.", self.main_kb())

    # ---------------------------------------------------------------- text messages
    def _text(self, msg):
        text = msg["text"].strip()
        low = text.lower()
        if low.startswith("/start"):
            arg = text.split(maxsplit=1)[1] if " " in text else ""
            if arg.startswith("paid_"):
                self.engine.user_returned_from_bank(arg[5:])
                self.send("🔎 Checking your payment with the railway… I'll send the ticket as soon as it's "
                          "confirmed.", self.main_kb())
            elif arg.startswith("payfail_"):
                self.send("The payment wasn't completed. The seats are still held; tap <b>Pay</b> on the "
                          "reservation message to try again.", self.main_kb())
            else:
                self.send(self.welcome(), self.main_kb())
            return
        if self.wiz and not text.startswith("/") and text not in ("➕ New trip", "🗂 My trips", "📊 Status",
                                                                  "⏸ Pause", "▶️ Resume"):
            return self._wiz_text(msg, text)
        if low in ("/new", "➕ new trip"):
            return self.start_wizard()
        if low in ("/trips", "🗂 my trips"):
            return self.show_trips()
        if low in ("/status", "📊 status"):
            return self.show_status()
        if low in ("/pause", "⏸ pause"):
            self.store.cfg["paused"] = True
            self.store.save()
            rec.ev("user_pause")
            return self.send("⏸ Paused. Nothing is watched or booked until you resume.", self.main_kb())
        if low in ("/resume", "▶️ resume"):
            self.store.cfg["paused"] = False
            self.store.save()
            rec.ev("user_resume")
            return self.send("▶️ Watching again.", self.main_kb())
        if low == "/account":
            self.wiz = {"kind": "account", "step": "email"}
            return self.send("Send the <b>email</b> you use on eticket.uzrailpass.uz:")
        if low in ("/cancel", "cancel"):
            self.wiz = None
            return self.send("OK, cancelled.", self.main_kb())
        self.send(self.welcome(), self.main_kb())

    def welcome(self):
        email, pw = self.store.railway_login()
        acc = f"Railway account: <b>{H(email)}</b>" if email and pw else "⚠️ Railway account not set yet: send /account"
        return ("🚆 <b>UzRail Ticket Bot</b>\n\n"
                "I watch eticket.uzrailpass.uz every second. In <b>auto-buy</b> mode I hold the seats the moment "
                "they appear and send you a <b>Pay</b> button (Hamkorbank page, 12 minutes to pay). "
                "In <b>notify</b> mode I just tell you.\n\n"
                f"{acc}\n\n➕ New trip · 🗂 My trips · 📊 Status · /account · /pause")

    # ---------------------------------------------------------------- account dialog
    def _account_text(self, msg, text):
        if self.wiz["step"] == "email":
            if "@" not in text:
                return self.send("That doesn't look like an email. Try again, or /cancel.")
            self.wiz.update(email=text, step="password")
            return self.send("Now send the <b>password</b>. I delete your message right after reading it; "
                             "it is stored encrypted on this PC only.")
        self._delete(msg)
        email = self.wiz["email"]
        self.wiz = None
        c = eticket.Client(email, text)
        try:
            c.login()
            friends = c.friends()
        except eticket.EticketError as e:
            return self.send(f"❌ Login failed: {H(str(e))}\nSend /account to try again.")
        self.store.set_railway_login(email, text)
        self.engine.account_changed()
        names = ", ".join(H(f"{f.get('firstname', '')} {f.get('lastname', '')}") for f in friends) or "none"
        rec.ev("account_saved", passengers=len(friends))
        self.send(f"✅ Logged in as <b>{H(email)}</b>.\nSaved passengers on the site: {names}\n\n"
                  "Add more passengers on eticket.uzrailpass.uz (My fellow travelers) if needed.", self.main_kb())

    def _delete(self, msg):
        try:
            self.api.call("deleteMessage", chat_id=msg["chat"]["id"], message_id=msg["message_id"])
        except Exception:
            pass

    # ---------------------------------------------------------------- trip wizard
    def start_wizard(self):
        email, pw = self.store.railway_login()
        if not (email and pw):
            self.wiz = {"kind": "account", "step": "email"}
            return self.send("First, your railway site login. Send the <b>email</b> you use on eticket.uzrailpass.uz:")
        self.wiz = {"kind": "trip", "step": "from", "trip": {"dates": [], "classes": [], "passengers": [],
                                                             "maxPrice": 0, "speed": 1}}
        self.send("🚉 <b>From</b> which station? Type at least 4 letters (e.g. <i>Tosh</i>, <i>Samarqand</i>, "
                  "<i>Urganch</i>). /cancel to stop.")

    def _wiz_text(self, msg, text):
        if self.wiz["kind"] == "account":
            return self._account_text(msg, text)
        step = self.wiz["step"]
        if step in ("from", "to"):
            if len(text) < 3:
                return self.send("Please type at least 4 letters of the station name.")
            try:
                found = find_stations(text)
            except eticket.EticketError as e:
                return self.send(f"Station search failed: {H(str(e))}")
            if not found:
                return self.send("No station found. Try another spelling (English, Uzbek or Russian).")
            self.wiz["found"] = {s["code"]: s["name"] for s in found[:10]}
            rows = [[{"text": s["name"].title(), "callback_data": f"w:st:{s['code']}"}] for s in found[:10]]
            return self.send("Tap the right station:", {"inline_keyboard": rows})
        if step == "price":
            digits = "".join(ch for ch in text if ch.isdigit())
            if not digits:
                return self.send("Send a number like 250000, or tap a button.")
            self.wiz["trip"]["maxPrice"] = int(digits)
            return self._ask_mode()
        self.send("Use the buttons above, or /cancel.")

    def _cal_kb(self, ym: str):
        y, m = map(int, ym.split("-"))
        today = dt.date.today()
        chosen = set(self.wiz["trip"]["dates"])
        rows = [[{"text": "‹", "callback_data": f"w:cal:{(dt.date(y, m, 1) - dt.timedelta(days=1)).strftime('%Y-%m')}"},
                 {"text": f"{calendar.month_name[m]} {y}", "callback_data": "w:nop"},
                 {"text": "›", "callback_data": f"w:cal:{(dt.date(y, m, 28) + dt.timedelta(days=4)).strftime('%Y-%m')}"}],
                [{"text": d, "callback_data": "w:nop"} for d in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")]]
        for week in calendar.monthcalendar(y, m):
            row = []
            for d in week:
                if not d:
                    row.append({"text": " ", "callback_data": "w:nop"})
                    continue
                day = dt.date(y, m, d)
                iso = day.isoformat()
                if day < today or day > today + dt.timedelta(days=90):
                    row.append({"text": "·", "callback_data": "w:nop"})
                else:
                    row.append({"text": f"✅{d}" if iso in chosen else str(d), "callback_data": f"w:day:{iso}"})
            rows.append(row)
        rows.append([{"text": f"Done ({len(chosen)} date{'s' if len(chosen) != 1 else ''})", "callback_data": "w:datesok"}])
        return {"inline_keyboard": rows}

    def _cls_kb(self):
        chosen = set(self.wiz["trip"]["classes"])
        rows = [[{"text": ("✅ " if c in chosen else "") + label(c), "callback_data": f"w:cls:{c}"}] for c in CLASSES]
        rows.append([{"text": "Any class" if not chosen else f"Done ({len(chosen)})", "callback_data": "w:clsok"}])
        return {"inline_keyboard": rows}

    def _ask_mode(self):
        self.wiz["step"] = "mode"
        self.send("What should I do when seats appear?", {"inline_keyboard": [
            [{"text": "🛒 Auto-buy: hold the seats + Pay button", "callback_data": "w:mode:buy"}],
            [{"text": "🔔 Notify me only", "callback_data": "w:mode:notify"}]]})

    def _pax_kb(self):
        chosen = self.wiz["trip"]["passengers"]
        rows = []
        for p in self.engine.passenger_list():
            mark = f"{chosen.index(p['id']) + 1}️⃣ " if p["id"] in chosen else ""
            rows.append([{"text": mark + p["name"], "callback_data": f"w:pax:{p['id']}"}])
        rows.append([{"text": f"Done ({len(chosen)})", "callback_data": "w:paxok"}])
        return {"inline_keyboard": rows}

    def _summary(self, t):
        cls = ", ".join(label(c) for c in t["classes"]) or "any class"
        price = f"up to {fmt_sum(t['maxPrice'])} so'm" if t["maxPrice"] else "any price"
        mode = "🛒 auto-buy" if t["mode"] == "buy" else "🔔 notify"
        names = {p["id"]: p["name"] for p in self.engine.passenger_list()}
        pax = ", ".join(H(names.get(p, p)) for p in t.get("passengers", [])) if t["mode"] == "buy" else ""
        dates = ", ".join(dt.date.fromisoformat(d).strftime("%d %b") for d in t["dates"])
        speed = {1: "every 0.8 s", 2: "every 0.4 s", 3: "every 0.27 s", 4: "every 0.2 s"}.get(t.get("speed", 1))
        return (f"🚆 <b>{H(t['from']['name'].title())} → {H(t['to']['name'].title())}</b>\n📅 {dates}\n🎫 {cls}, {price}\n"
                f"{mode}" + (f" for {pax}" if pax else "") + f"\n⚡ checks {speed}")

    def _callback(self, cb):
        data = cb.get("data", "")
        try:
            self.api.call("answerCallbackQuery", callback_query_id=cb["id"])
        except Exception:
            pass
        mid = cb["message"]["message_id"]
        if data.startswith("w:"):
            return self._wiz_cb(data[2:], mid)
        kind, _, arg = data.partition(":")
        if kind == "cancel":
            ok = self.engine.cancel_hold(arg)
            rec.ev("user_cancel_hold", order=arg, ok=ok)
            self.edit(mid, "❌ Reservation cancelled; the seats are released. I paused that trip so it "
                           "doesn't grab them again (resume it in 🗂 My trips).")
        elif kind == "paylink":
            h = self.engine.new_pay_link(arg)
            self.send("🔁 New payment link ready." if h else "Couldn't make a new link; the hold may have ended.",
                      self._hold_kb(h) if h else None)
        elif kind == "tpause":
            self.store.set_trip_active(arg, False)
            self.show_trips(mid)
        elif kind == "tresume":
            self.store.set_trip_active(arg, True)
            self.show_trips(mid)
        elif kind == "tdel":
            self.store.delete_trip(arg)
            self.show_trips(mid)

    def _wiz_cb(self, data, mid):
        if not self.wiz or self.wiz.get("kind") != "trip":
            return self.send("That setup has expired. Tap ➕ New trip to start again.")
        t = self.wiz["trip"]
        cmd, _, arg = data.partition(":")
        if cmd == "nop":
            return
        if cmd == "st":
            name = self.wiz.get("found", {}).get(arg, arg)
            if self.wiz["step"] == "from":
                t["from"] = {"code": arg, "name": name}
                self.wiz["step"] = "to"
                self.edit(mid, f"From: <b>{H(name.title())}</b>")
                return self.send("🏁 <b>To</b> which station? Type at least 4 letters.")
            if arg == t["from"]["code"]:
                return self.send("That's the departure station. Pick a different arrival.")
            t["to"] = {"code": arg, "name": name}
            self.wiz["step"] = "dates"
            self.edit(mid, f"To: <b>{H(name.title())}</b>")
            return self.send("📅 Pick one or more <b>dates</b>, then tap Done:", self._cal_kb(dt.date.today().strftime("%Y-%m")))
        if cmd == "cal":
            return self.edit(mid, "📅 Pick one or more <b>dates</b>, then tap Done:", self._cal_kb(arg))
        if cmd == "day":
            if arg in t["dates"]:
                t["dates"].remove(arg)
            else:
                t["dates"].append(arg)
            t["dates"].sort()
            return self.edit(mid, "📅 Pick one or more <b>dates</b>, then tap Done:", self._cal_kb(arg[:7]))
        if cmd == "datesok":
            if not t["dates"]:
                return self.send("Pick at least one date first.")
            self.wiz["step"] = "classes"
            self.edit(mid, "📅 " + ", ".join(dt.date.fromisoformat(d).strftime("%d %b") for d in t["dates"]))
            return self.send("🎫 Which <b>ticket classes</b>? Tap to select, then Done (nothing selected = any).",
                             self._cls_kb())
        if cmd == "cls":
            t["classes"].remove(arg) if arg in t["classes"] else t["classes"].append(arg)
            return self.edit(mid, "🎫 Which <b>ticket classes</b>? Tap to select, then Done (nothing selected = any).",
                             self._cls_kb())
        if cmd == "clsok":
            self.wiz["step"] = "price"
            self.edit(mid, "🎫 " + (", ".join(label(c) for c in t["classes"]) or "Any class"))
            return self.send("💰 Maximum price per ticket? Send a number (e.g. <i>250000</i>) or tap:",
                             {"inline_keyboard": [[{"text": "No limit", "callback_data": "w:price:0"}]]})
        if cmd == "price":
            t["maxPrice"] = int(arg)
            self.edit(mid, "💰 No price limit")
            return self._ask_mode()
        if cmd == "mode":
            t["mode"] = arg
            self.edit(mid, "🛒 Auto-buy" if arg == "buy" else "🔔 Notify only")
            if arg == "buy":
                if self.engine.client is None:
                    self.engine._ensure_client()
                else:
                    self.engine.refresh_friends()
                if not self.engine.passenger_list():
                    return self.send("You have no saved passengers on the railway site yet. Add them on "
                                     "eticket.uzrailpass.uz (My fellow travelers), then tap ➕ New trip again.")
                self.wiz["step"] = "pax"
                return self.send(f"👥 Who travels? Tap in <b>priority order</b> (max {MAX_PASSENGERS}); if fewer "
                                 "seats appear, the first ones get them.", self._pax_kb())
            return self._ask_speed()
        if cmd == "pax":
            if arg in t["passengers"]:
                t["passengers"].remove(arg)
            elif len(t["passengers"]) < MAX_PASSENGERS:
                t["passengers"].append(arg)
            return self.edit(mid, f"👥 Who travels? Tap in <b>priority order</b> (max {MAX_PASSENGERS}).", self._pax_kb())
        if cmd == "paxok":
            if not t["passengers"]:
                return self.send("Pick at least one passenger.")
            names = {p["id"]: p["name"] for p in self.engine.passenger_list()}
            self.edit(mid, "👥 " + ", ".join(H(names.get(p, p)) for p in t["passengers"]))
            return self._ask_speed()
        if cmd == "speed":
            t["speed"] = int(arg)
            self.edit(mid, f"⚡ Speed {arg}")
            self.wiz["step"] = "confirm"
            return self.send(self._summary(t) + "\n\nSave and start watching?", {"inline_keyboard": [
                [{"text": "✅ Save & start", "callback_data": "w:save"}, {"text": "✖️ Discard", "callback_data": "w:discard"}]]})
        if cmd == "save":
            try:
                saved = self.store.upsert_trip(t)
            except ValueError as e:
                return self.send(f"⚠️ {H(str(e))}. Tap ➕ New trip to start again.")
            self.wiz = None
            rec.ev("trip_saved", trip=saved["id"], mode=saved["mode"], dates=saved["dates"], classes=saved["classes"],
                   max_price=saved["maxPrice"], passengers=len(saved["passengers"]), speed=saved["speed"],
                   route=[saved["from"]["code"], saved["to"]["code"]])
            self.edit(mid, "✅ Saved. Watching now.\n\n" + self._summary(saved))
            return self.send("I'll message you the moment something happens.", self.main_kb())
        if cmd == "discard":
            self.wiz = None
            return self.edit(mid, "Discarded.")

    def _ask_speed(self):
        self.wiz["step"] = "speed"
        self.send("⚡ How often should I check? Faster = more requests to the railway site.", {"inline_keyboard": [
            [{"text": "Normal · every 0.8 s", "callback_data": "w:speed:1"}],
            [{"text": "Fast · every 0.4 s", "callback_data": "w:speed:2"}],
            [{"text": "Max · every 0.2 s", "callback_data": "w:speed:4"}]]})

    # ---------------------------------------------------------------- lists
    def show_trips(self, edit_mid=None):
        trips = self.store.trips()
        if not trips:
            return self.send("No trips yet. Tap ➕ New trip.", self.main_kb())
        parts, rows = [], []
        for i, t in enumerate(trips, 1):
            state = "🟢" if t["active"] else "⏸"
            parts.append(f"{state} <b>{i}.</b> " + self._summary(t))
            rows.append([{"text": f"{'⏸ Pause' if t['active'] else '▶️ Resume'} {i}",
                          "callback_data": f"{'tpause' if t['active'] else 'tresume'}:{t['id']}"},
                         {"text": f"🗑 Delete {i}", "callback_data": f"tdel:{t['id']}"}])
        text = "🗂 <b>Your trips</b>\n\n" + "\n\n".join(parts)
        if edit_mid:
            self.edit(edit_mid, text, {"inline_keyboard": rows})
        else:
            self.send(text, {"inline_keyboard": rows})

    def show_status(self):
        s = self.engine.snapshot()
        age = time.time() - s["last_check"] if s["last_check"] else None
        lines = [f"📊 <b>Status:</b> {H(s['state'])}" + (f" ({H(s['detail'])})" if s["detail"] else ""),
                 f"Last check: {f'{age:.1f} s ago' if age is not None else 'not yet'} · checks so far: {s['checks']}",
                 f"Live order events: {'connected' if s['live'] else 'not connected'}",
                 f"Active trips: {sum(1 for t in s['trips'] if t['active'])} of {len(s['trips'])}"]
        for h in s["holds"]:
            lines.append(f"⏳ Holding train {H(h['train'])} car {H(h['car'])} seats {', '.join(h['seats']) or '?'}: "
                         f"{fmt_left(h['left'])} left to pay")
        self.send("\n".join(lines), self.main_kb())

    # ---------------------------------------------------------------- engine notifications
    def _hold_kb(self, h):
        rows = []
        if h.get("pay_url"):
            rows.append([{"text": f"💳 Pay {fmt_sum(h['total'])} so'm", "url": h["pay_url"]}])
        else:
            rows.append([{"text": "🔁 Get a payment link", "callback_data": f"paylink:{h['order']}"}])
        rows.append([{"text": "❌ Cancel reservation", "callback_data": f"cancel:{h['order']}"}])
        return {"inline_keyboard": rows}

    def _hold_text(self, h, head="✅ <b>Seats reserved!</b>"):
        seats = []
        for i, name in enumerate(h["names"]):
            seat = h["seats"][i] if i < len(h["seats"]) else "?"
            types = h.get("seat_types") or []
            berth = {"В": " (upper)", "Н": " (lower)"}.get(types[i] if i < len(types) else None, "")
            seats.append(f"   • {H(name)}: seat {H(str(seat))}{berth}")
        dep = h.get("dep") or ""
        return (f"{head}\n\n🚆 Train <b>{H(h['train'])}</b> · {H(h['from'].title())} → {H(h['to'].title())}\n"
                f"🕒 {H(dep)}\n🚃 Car <b>{H(h['car'])}</b> · {label(h['cls'])}\n" + "\n".join(seats) +
                f"\n💰 Total <b>{fmt_sum(h['total'])} so'm</b>"
                + f"\n<i>held {h.get('took', '?')} s after the seats appeared · order {H(h['order'])}</i>")

    def _pay_text(self, h):
        left = h["expires_at"] - time.time()
        until = time.strftime("%H:%M", time.localtime(h["expires_at"]))
        renew = (f"\nRenewal {h['renewals']}/{self.store.cfg.get('max_renewals', 3)}" if h.get("renewals") else "")
        return (f"💳 <b>Pay {fmt_sum(h['total'])} so'm</b> for train {H(h['train'])}, car {H(h['car'])}, "
                f"seat{'s' if len(h['seats']) > 1 else ''} {H(', '.join(h['seats']) or '?')}\n"
                f"⏳ <b>{fmt_left(left)}</b> left (until {until}). Unpaid seats are released.{renew}")

    def reserved(self, h):
        # message 2 of 3 (success), then message 3 of 3 (payment with the Pay button)
        self.send(self._hold_text(h))

        def remember(mid):
            if h["order"] in self.store.state["holds"]:
                self.store.state["holds"][h["order"]]["msg_id"] = mid
                self.store.save()
        self.send(self._pay_text(h), self._hold_kb(h), on_sent=remember)

    def missed(self, trip, date, hit, why):
        self.send(f"❌ <b>Not this time</b> · train {H(hit['number'])}, {label(hit['cls'])}, "
                  f"{dt.date.fromisoformat(date).strftime('%d %b')}: {H(why)}.\nStill watching.")

    def hold_update(self, h):
        if h.get("msg_id"):
            self.edit(h["msg_id"], self._pay_text(h), self._hold_kb(h))

    def renewed(self, old, new):
        if old.get("msg_id"):
            self.edit(old["msg_id"], "🔁 This hold was renewed; use the newest message to pay.")
        def remember(mid):
            if new["order"] in self.store.state["holds"]:
                self.store.state["holds"][new["order"]]["msg_id"] = mid
                self.store.save()
        self.send(self._hold_text(new, head="🔁 <b>Hold renewed</b> (seats may have changed)"))
        self.send(self._pay_text(new), self._hold_kb(new), on_sent=remember)

    def expired(self, h):
        if h.get("msg_id"):
            self.edit(h["msg_id"], "⌛ This reservation expired unpaid; the seats were released.")
        self.send(f"⌛ The hold on train {H(h['train'])} expired unpaid. I keep watching for seats.")

    def released(self, h, reason):
        if h.get("msg_id"):
            self.edit(h["msg_id"], f"ℹ️ {H(reason)}")
        self.send(f"ℹ️ {H(reason)} I keep watching for seats.")

    def payment_failed(self, h):
        self.send("⚠️ The bank reported a failed payment. The seats are still held; tap Pay to try again.",
                  self._hold_kb(h))

    def paid(self, h, pdf):
        if h.get("msg_id"):
            self.edit(h["msg_id"], self._hold_text(h, head="🎉 <b>Paid!</b>").split("\n\n⏳")[0])
        text = (f"🎉 <b>Payment confirmed!</b> Train {H(h['train'])}, car {H(h['car'])}, "
                f"seats {', '.join(h['seats'])}.")
        if pdf:
            self.send(text, document=pdf, filename=f"ticket-{h['order']}.pdf")
        else:
            self.send(text + "\nI couldn't download the ticket PDF; get it on eticket.uzrailpass.uz → My orders.")

    def seats_found(self, trip, date, hits):
        # message 1 of 3
        buying = trip.get("mode") == "buy"
        lines = [f"🎫 <b>Tickets appeared!</b> {H(trip['from']['name'].title())} → {H(trip['to']['name'].title())} · "
                 f"{dt.date.fromisoformat(date).strftime('%d %b')}"]
        for h in hits[:12]:
            lines.append(f"• Train {H(h['number'])} {H(h['train'].get('departureDate', '')[-5:])} · {label(h['cls'])}: "
                         f"<b>{h['free']}</b> free · {fmt_sum(h['price'])} so'm")
        if buying:
            lines.append("\n⚡ Booking now…")
            return self.send("\n".join(lines))
        self.send("\n".join(lines), {"inline_keyboard": [[{"text": "Open the railway site",
                                                           "url": "https://eticket.uzrailpass.uz/en/pages/trains-page"}]]})

    def problem(self, text):
        self.send(f"⚠️ {H(text)}")

    def info(self, text):
        self.send(f"ℹ️ {H(text)}")
