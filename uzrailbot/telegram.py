"""Telegram side: long polling (no server needed), owner-only control, guided trip setup in chat,
and every notification the engine produces (reservation with Pay link, countdown, transfer, PDF)."""
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
from . import telemetry
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


def fmt_day(iso: str) -> str:
    """'2026-11-08' -> 'Sun 8 Nov'."""
    try:
        d = dt.date.fromisoformat(iso)
    except (TypeError, ValueError):
        return str(iso)
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def fmt_trip_time(dep, arr) -> str:
    """'07.11.2026 21:05', '08.11.2026 07:40' -> 'Sat 7 Nov 21:05 → Sun 8 Nov 07:40'."""
    def one(stamp):
        try:
            d = dt.datetime.strptime(str(stamp), "%d.%m.%Y %H:%M")
        except ValueError:
            return None
        return d
    a, b = one(dep), one(arr)
    if not a:
        return ""
    out = f"{a.strftime('%a')} {a.day} {a.strftime('%b')} {a.strftime('%H:%M')}"
    if b:
        out += " → " + (b.strftime("%H:%M") if b.date() == a.date() else
                        f"{b.strftime('%a')} {b.day} {b.strftime('%b')} {b.strftime('%H:%M')}")
    return out


def short_label(code: str) -> str:
    return {"3П": "Plaskart", "2К": "Kupe", "1Л": "SV", "1А": "Lux", "2Е": "Sit eco", "1С": "Sit biz",
            "2В": "Sit 2nd", "1В": "Sit 1st"}.get(code, code)


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
        self.found = {}               # (trip, date) -> message id of the "tickets appeared" note
        self.remote = None            # the Mini App's live link
        self.stop_ev = threading.Event()
        self.healthy = True
        self.last_error = ""

    # ---------------------------------------------------------------- lifecycle
    def start(self, engine):
        self.engine = engine
        threading.Thread(target=self._sender, name="tg-send", daemon=True).start()
        threading.Thread(target=self._poller, name="tg-poll", daemon=True).start()
        try:
            from .remote import Remote
            self.remote = Remote(self.store, self)
            self.remote.start()
        except Exception:                  # the bot must work without the live link
            log.exception("live link not started")

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
            if kind == "delfound":        # the "tickets appeared" note of a booking that has finished
                mid = self.found.pop(extra, None)
                if mid:
                    try:
                        self.api.call("deleteMessage", chat_id=chat, message_id=mid)
                    except (TgError, requests.RequestException) as e:
                        log.info("could not remove a note: %s", e)
                continue
            auto_kb = kind in ("msg", "quiet") and markup is None
            if markup is False:           # on purpose no keyboard: this message gets deleted later, and deleting
                markup = None             # the message that carries the reply keyboard could remove the keyboard
            elif auto_kb:
                # The Mini App reads its trips/passengers from the keyboard link, which Telegram only updates when a
                # message carries it: give every plain message the fresh keyboard so the Mini App never goes stale.
                try:
                    markup = self.main_kb()
                except Exception:
                    log.exception("building the keyboard")
            for attempt in range(60):
                try:
                    if kind in ("msg", "quiet"):
                        res = self.api.call("sendMessage", chat_id=chat, text=text, parse_mode="HTML", reply_markup=markup,
                                            disable_notification=True if kind == "quiet" else None,
                                            link_preview_options={"is_disabled": True})
                        if extra:
                            extra(res["message_id"])
                    elif kind == "edit":
                        self.api.call("editMessageText", chat_id=chat, message_id=extra, text=text, parse_mode="HTML",
                                      reply_markup=markup, link_preview_options={"is_disabled": True})
                    else:
                        self.api.call("sendDocument", http_timeout=60, files={"document": (fname, doc, "application/pdf")},
                                      chat_id=chat, caption=text, parse_mode="HTML",
                                      reply_markup=json.dumps(self.main_kb()))
                    self.healthy = True
                    break
                except TgError as e:
                    if e.retry_after:
                        time.sleep(e.retry_after)
                        continue
                    if auto_kb and e.code == 400 and "not modified" not in str(e):
                        log.warning("telegram refused the keyboard, sending without it: %s", e)
                        markup, auto_kb = None, False
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
                {"command": "passengers", "description": "Reload my saved passengers"},
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
    def miniapp_state(self) -> dict:
        """What the Mini App shows: sent in its link, and again every few seconds over the live link."""
        email, pw = self.store.railway_login()
        state = {
            "v": 4, "bot": self.store.cfg["telegram"].get("bot_username", ""), "email": email, "hasPw": bool(pw),
            "key": crypto.public_key_b64(self.store.transport_key()),
            "pax": [{"i": p["id"], "n": p["name"]} for p in (self.engine.passenger_list() if self.engine else [])],
            "trips": [dict({k: t[k] for k in ("id", "from", "to", "dates", "classes", "maxPrice", "mode", "passengers",
                                              "speed", "trains", "together", "active")},
                           done=self.store.done_passengers(t["id"]),
                           tk=[{"t": x["train"], "c": x["car"], "s": x["seats"]} for x in self.store.tickets(t["id"])],
                           kt=[[r["number"], r["dep"], r["arr"]] for r in (self.engine.scan_trains(t, fetch=False)
                                                                           if self.engine else [])][:12])
                      for t in self.store.trips()],
        }
        state["holds"] = [{"o": h["order"], "cls": h.get("cls"), "dep": h.get("dep"), "arr": h.get("arr"), "st": h.get("seat_types") or [],
                           "trip": h["trip"], "p": h["passengers"], "t": h["train"], "c": h["car"], "s": h.get("seats") or [], "f": h["from"], "to": h["to"],
                           "d": h.get("date"), "sum": h["total"], "exp": int(h["expires_at"]), "pay": h.get("pay_url") or "",
                           "n": h.get("names") or []} for h in list(self.store.state.get("holds", {}).values())]
        return state

    def miniapp_url(self) -> str:
        """The Mini App link, with the current state in the #fragment."""
        state = self.miniapp_state()
        if self.remote:
            state["live"] = self.remote.link()
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
            # no train picker here: the Mini App has its own (the chat wizard /new still offers it)
            self.send("✅ <b>Saved. Watching now.</b>\n\n" + self._summary(t), self.main_kb())
            return
        if act == "toggle":
            self.store.set_trip_active(str(d.get("id")), bool(d.get("active")))
            return self.send("▶️ Trip resumed." if d.get("active") else "⏸ Trip paused.", self.main_kb())
        if act == "batch":
            ops = [o for o in (d.get("ops") or []) if isinstance(o, dict)][:50]
            for op in ops:
                if op.get("a") == "del":
                    self.store.delete_trip(str(op.get("id")))
                elif op.get("a") == "toggle":
                    self.store.set_trip_active(str(op.get("id")), bool(op.get("active")))
            return self.send(f"✅ Saved {len(ops)} change{'' if len(ops) == 1 else 's'} to your trips.", self.main_kb())
        if act == "pause":
            self.store.cfg["paused"] = bool(d.get("on"))
            self.store.save()
            rec.ev("user_pause" if self.store.cfg["paused"] else "user_resume", source="miniapp")
            return self.send("⏸ Paused." if self.store.cfg["paused"] else "▶️ Watching again.", self.main_kb())
        if act == "cancel":
            oid = str(d.get("order"))
            h = dict(self.store.state["holds"].get(oid) or {})
            ok = self.engine.cancel_hold(oid)
            rec.ev("user_cancel_hold", order=oid, ok=ok)
            if h.get("msg_id"):
                self.edit(h["msg_id"], "❌ Cancelled in the Mini App.")
            return self.send(f"❌ Reservation on train {H(h.get('train', '?'))} cancelled. The seats went back on sale, and I "
                             "paused that trip so it doesn't grab them again.", self.main_kb())
        if act == "transfer":
            oid, frm, to = str(d.get("order")), str(d.get("from")), str(d.get("to"))

            def move():
                h = dict(self.store.state["holds"].get(oid) or {})
                ok, msg = self.engine.transfer_hold(oid, frm, to)
                rec.ev("user_transfer", order=oid, ok=ok, source="miniapp")
                if h:
                    name = next((p["name"] for p in self.engine.passenger_list() if p["id"] == to), "?")
                    self.transfer_done(h, name, ok, msg)
            threading.Thread(target=move, daemon=True).start()
            return
        if act == "report":
            threading.Thread(target=telemetry.report, args=(self.store, str(d.get("note") or "")), daemon=True).start()
            return
        if act in ("sharelogs", "autoupdate"):
            self.store.cfg["share_logs" if act == "sharelogs" else "auto_update"] = bool(d.get("on"))
            self.store.save()
            return
        if act == "paylink":
            threading.Thread(target=self.new_link, args=(str(d.get("order")),), daemon=True).start()
            return
        if act == "paytap":           # Pay was tapped in the Mini App: a log then shows a pay page that didn't work
            rec.ev("pay_opened", order=str(d.get("order")), source="miniapp")
            return
        if act == "tickets":
            threading.Thread(target=self.send_tickets, args=(str(d.get("id")),), daemon=True).start()
            return self.send("📄 Getting your tickets…", self.main_kb())
        if act == "pax":
            return self.refresh_passengers()
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

    def new_link(self, oid):
        """A fresh bank page for a held seat, for when the old one stopped working. May take a few seconds."""
        h = self.engine.new_pay_link(oid)
        rec.ev("user_new_pay_link", order=oid, ok=bool(h))
        if not h:
            return self.send("⚠️ I couldn't get a new payment link. The hold may have ended, or the bank isn't "
                             "answering. Try again in a moment.")
        h["last_update"] = time.time()
        self.hold_update(h)                       # the Pay button on the reservation message opens the new page
        self.send("🔁 New payment link ready. Tap <b>Pay</b> on the reservation message.",
                  None if h.get("msg_id") else self._hold_kb(h))

    def send_tickets(self, trip_id):
        """The Completed tab's "Get tickets": send the PDF of every order paid on this watch again."""
        for x in self.store.tickets(trip_id):
            try:
                pdf = self.engine.client.ticket_pdf(x["order"]) if self.engine.client else None
            except Exception as e:
                log.info("ticket %s: %s", x["order"], type(e).__name__)
                pdf = None
            where = f"train {H(x['train'])}, car {H(x['car'])}, seat {H(', '.join(x['seats']) or '?')}"
            if pdf:
                self.send(f"🎟 Your ticket: {where}.", document=pdf, filename=f"ticket-{x['order']}.pdf")
            else:
                self.send(f"⚠️ I couldn't download the ticket for {where}. Get it on eticket.uzrailpass.uz → My orders.")

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
        if low in ("/passengers", "/pax"):
            return self.refresh_passengers()
        if low == "/account":
            self.wiz = {"kind": "account", "step": "email"}
            return self.send("Send the <b>email</b> you use on eticket.uzrailpass.uz:")
        if low in ("/cancel", "cancel"):
            self.wiz = None
            return self.send("OK, cancelled.", self.main_kb())
        self.send(self.welcome(), self.main_kb())

    def welcome(self):
        email, pw = self.store.railway_login()
        acc = (f"Railway account: <b>{H(email)}</b>" if email and pw
               else "⚠️ Railway account not connected yet: 🚆 Trips & settings → Account")
        return ("🚆 <b>UzRail Ticket Bot</b>\n\n"
                "I watch eticket.uzrailpass.uz every second. In <b>auto-buy</b> mode I hold the seats the moment "
                "they appear and send you a <b>Pay</b> button (Hamkorbank page, 12 minutes to pay). "
                "In <b>notify</b> mode I just tell you.\n\n"
                f"{acc}\n\nTap <b>🚆 Trips & settings</b> to connect your account and add trips. "
                "<b>📊 Status</b> shows what the railway has right now.")

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
        price = f"up to {fmt_sum(t['maxPrice'])} so'm per ticket" if t["maxPrice"] else "any price"
        mode = "🛒 auto-buy" if t["mode"] == "buy" else "🔔 notify"
        names = {p["id"]: p["name"] for p in self.engine.passenger_list()}
        pax = ", ".join(H(names.get(p, p)) for p in t.get("passengers", [])) if t["mode"] == "buy" else ""
        dates = ", ".join(fmt_day(d) for d in sorted(t["dates"]))
        speed = {1: "every 0.8 s", 2: "every 0.4 s", 3: "every 0.27 s", 4: "every 0.2 s"}.get(t.get("speed", 1))
        trains = ("only train " + ", ".join(H(n) for n in t["trains"])) if t.get("trains") else "all trains"
        group = ""
        if t["mode"] == "buy" and len(t.get("passengers", [])) > 1:
            group = ("\n👥 everyone together (one compartment or bay), or nobody" if t.get("together")
                     else "\n👥 may split across cars, classes or trains (first passengers first)")
        return (f"🚆 <b>{H(t['from']['name'].title())} → {H(t['to']['name'].title())}</b>\n📅 {dates}\n"
                f"🎫 {cls}, {price} · {trains}\n"
                f"{mode}" + (f" for {pax}" if pax else "") + group + f"\n⚡ checks {speed}")

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
            trip_id = (self.store.state["holds"].get(arg) or {}).get("trip")
            ok = self.engine.cancel_hold(arg)
            rec.ev("user_cancel_hold", order=arg, ok=ok)
            kb = {"inline_keyboard": [[{"text": "▶️ Keep watching this trip", "callback_data": f"tresume1:{trip_id}"}]]} \
                if trip_id and self.store.trip(trip_id) else None
            self.edit(mid, "❌ <b>Reservation cancelled.</b> The seats went back on sale.\n"
                           "I paused that trip so it doesn't grab the same seats again.", kb)
        elif kind == "tresume1":
            self.store.set_trip_active(arg, True)
            self.edit(mid, "▶️ Watching that trip again.")
        elif kind == "tr":
            self._train_pick(arg, mid)
        elif kind in ("xfer", "xfrom", "xto", "xgo", "xback"):
            self._transfer_cb(kind, arg, mid)
        elif kind == "paylink":
            threading.Thread(target=self.new_link, args=(arg,), daemon=True).start()
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
            return self.send("That setup has expired. Send /new to start again, or use 🚆 Trips & settings.")
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
            return self.send("🎫 Which <b>ticket classes</b>? Tap to select, then Done (none selected means any class).",
                             self._cls_kb())
        if cmd == "cls":
            t["classes"].remove(arg) if arg in t["classes"] else t["classes"].append(arg)
            return self.edit(mid, "🎫 Which <b>ticket classes</b>? Tap to select, then Done (none selected means any class).",
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
                                     "eticket.uzrailpass.uz (My fellow travelers), then send /new again.")
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
                return self.send(f"⚠️ {H(str(e))}. Send /new to start again.")
            self.wiz = None
            rec.ev("trip_saved", trip=saved["id"], mode=saved["mode"], dates=saved["dates"], classes=saved["classes"],
                   max_price=saved["maxPrice"], passengers=len(saved["passengers"]), speed=saved["speed"],
                   route=[saved["from"]["code"], saved["to"]["code"]])
            self.edit(mid, "✅ Saved. Watching now.\n\n" + self._summary(saved))
            self.send("I'll message you the moment something happens.", self.main_kb())
            threading.Thread(target=self.offer_trains, args=(saved["id"],), daemon=True).start()
            return
        if cmd == "discard":
            self.wiz = None
            return self.edit(mid, "Discarded.")

    def _ask_speed(self):
        self.wiz["step"] = "speed"
        self.send("⚡ How often should I check? Faster checks send more requests to the railway site.", {"inline_keyboard": [
            [{"text": "Normal · every 0.8 s", "callback_data": "w:speed:1"}],
            [{"text": "Fast · every 0.4 s", "callback_data": "w:speed:2"}],
            [{"text": "Max · every 0.2 s", "callback_data": "w:speed:4"}]]})

    # ---------------------------------------------------------------- lists
    def show_trips(self, edit_mid=None):
        trips = self.store.trips()
        if not trips:
            return self.send("No trips yet. Tap 🚆 Trips & settings to add one.", self.main_kb())
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

    def refresh_passengers(self):
        if not self.engine:
            return
        self.engine.refresh_friends()
        pax = self.engine.passenger_list()
        rec.ev("passengers_refreshed", count=len(pax))
        names = "\n".join(f"• {H(p['name'])}" for p in pax) or "none yet"
        self.send(f"👥 <b>Saved passengers: {len(pax)}</b>\n{names}\n\nOpen 🚆 Trips & settings again to use the new list. "
                  "To add someone, use eticket.uzrailpass.uz → My fellow travelers, then send /passengers.", self.main_kb())

    def show_status(self):
        self.send(self.status_text(), self.main_kb())

    def status_text(self) -> str:
        """Per trip: every passenger and what is happening for them (held where and until when, ticket bought,
        searching), then what the railway has right now on its dates."""
        s = self.engine.snapshot()
        age = time.time() - s["last_check"] if s["last_check"] else None
        head = {"watching": "🟢 <b>Watching</b>", "paused": "⏸ <b>Paused</b>", "idle": "⚪ <b>No active trips</b>",
                "offline": "🔴 <b>No connection</b>", "setup": "🟡 <b>Almost ready</b>", "error": "🔴 <b>Problem</b>",
                "maintenance": "🟠 <b>Railway site in maintenance</b>"}.get(s["state"], H(s["state"]))
        lines = [head + (f" · last check {age:.1f} s ago" if age is not None and age < 120 else "")]
        if s["state"] != "watching" and s["detail"]:
            lines.append(H(s["detail"]))
        names = {p["id"]: p["name"] for p in self.engine.passenger_list()}
        today = dt.datetime.now(dt.timezone(dt.timedelta(hours=5))).date().isoformat()
        holds = list(s["holds"])
        shown_holds = set()
        trips = sorted(s["trips"], key=lambda t: not t["active"])
        for t in trips:
            future = [d for d in sorted(t["dates"]) if d >= today]
            state = "" if t["active"] else " · ⏸ paused"
            lines.append(f"\n🚆 <b>{H(t['from']['name'].title())} → {H(t['to']['name'].title())}</b>{state}")
            lines.append("📅 " + (", ".join(fmt_day(d) for d in future) or "no dates left"))
            cls = ", ".join(short_label(c) for c in t["classes"]) or "any class"
            price = f"up to {fmt_sum(t['maxPrice'])} per ticket" if t["maxPrice"] else "any price"
            trains = "only " + ", ".join(H(n) for n in t["trains"]) if t.get("trains") else "all trains"
            lines.append(f"🎫 {cls} · {price} · {trains} · {'auto-buy' if t['mode'] == 'buy' else 'notify only'}")
            if t["mode"] == "buy":
                done = set((self.store.state.get("done") or {}).get(t["id"]) or [])
                route = (t["from"]["code"], t["to"]["code"])
                for i, pid in enumerate(t["passengers"], 1):
                    who = H(names.get(pid, "Passenger"))
                    h = next((h for h in holds if pid in (h.get("passengers") or []) and (h.get("trip") == t["id"] or (
                        (self.store.trip(h.get("trip")) or {}).get("from", {}).get("code"),
                        (self.store.trip(h.get("trip")) or {}).get("to", {}).get("code")) == route)), None)
                    if pid in done:
                        what = "✅ ticket bought"
                    elif h:
                        shown_holds.add(h["order"])
                        k = h["passengers"].index(pid)
                        seat = (h.get("seats") or [])[k] if k < len(h.get("seats") or []) else "?"
                        berth = {"В": " upper", "Н": " lower"}.get(((h.get("seat_types") or []) + [None] * 9)[k], "")
                        when = fmt_trip_time(h.get("dep"), h.get("arr")) or fmt_day(h.get("date", ""))
                        what = (f"⏳ <b>seat held</b>: train {H(h['train'])}, car {H(h['car'])}, seat {H(str(seat))}{berth}"
                                f" · {when} · <b>pay within {fmt_left(h['left'])}</b>")
                    elif t["active"] and future:
                        what = "🔎 searching"
                    else:
                        what = "⏸ not searching"
                    lines.append(f"   {i}. {who}: {what}")
            if t["active"]:
                for d in future[:3]:
                    b = next((b for b in s.get("board", []) if (b["from"], b["to"], b["date"]) ==
                              (t["from"]["code"], t["to"]["code"], d)), None)
                    if not b:
                        continue
                    rows = []
                    for tr in sorted(b["trains"], key=lambda r: str(r["dep"])[-5:]):
                        if t.get("trains") and tr["number"] not in t["trains"]:
                            continue
                        cells = []
                        for c, v in tr["classes"].items():
                            if t["classes"] and c not in t["classes"]:
                                continue
                            pricey = t["maxPrice"] and v["price"] > t["maxPrice"]
                            cells.append(f"{short_label(c)} <b>{v['free']}</b>" + (" (over max price)" if pricey and v["free"] else ""))
                        rows.append(f"<code>{H(tr['number']):<5}</code> {H(str(tr['dep'])[-5:])}→{H(str(tr['arr'])[-5:])}  "
                                    + (" · ".join(cells) or ("sold out" if not tr["classes"] else "no seats in your classes")))
                    lines.append(f"   <i>Right now, {fmt_day(d)}:</i>")
                    lines.extend("   " + r for r in rows[:8]) if rows else lines.append("   no trains on this date")
                    if len(rows) > 8:
                        lines.append(f"   … and {len(rows) - 8} more trains (see the PC app)")
        for h in holds:                                   # holds whose trip was deleted
            if h["order"] not in shown_holds:
                lines.append(f"\n⏳ <b>Held</b>: {', '.join(H(n) for n in h.get('names') or [])} · train {H(h['train'])}, "
                             f"car {H(h['car'])}, seat {H(', '.join(h.get('seats') or []) or '?')} · "
                             f"<b>pay within {fmt_left(h['left'])}</b>")
        if not trips:
            lines.append("\nNo trips yet: tap 🚆 Trips & settings to add one.")
        st = s.get("stats") or {}
        if st.get("detections") or st.get("holds"):
            fast = f" · fastest hold {st['fastest']} s" if st.get("fastest") else ""
            lines.append(f"\n📈 Since the bot started: seats showed up {st['detections']} times · held {st['holds']} · missed {st['missed']}{fast}")
        out, size = [], 0                               # Telegram allows 4096 characters; cut at a line
        for line in lines:
            if size + len(line) > 3900:
                out.append("… (more in the PC app)")
                break
            out.append(line)
            size += len(line) + 1
        return "\n".join(out)

    # ---------------------------------------------------------------- which trains (after a trip is saved)
    def offer_trains(self, trip_id, edit_mid=None):
        t = self.store.trip(trip_id)
        if not t or not self.engine:
            return
        rows = self.engine.scan_trains(t)
        if not rows:
            return
        self._trains_cache = getattr(self, "_trains_cache", {})
        self._trains_cache[trip_id] = rows
        text, kb = self._trains_msg(t, rows)
        if edit_mid:
            self.edit(edit_mid, text, kb)
        else:
            self.send(text, kb)

    def _trains_msg(self, t, rows):
        picked = t.get("trains") or []
        dates = len([d for d in t["dates"]])
        text = (f"🚆 <b>Live seats on your {'date' if dates == 1 else 'dates'}</b> · "
                f"{H(t['from']['name'].title())} → {H(t['to']['name'].title())}\n")
        if picked:
            text += f"Watching <b>only {', '.join(H(n) for n in picked)}</b>. Tap a train to add or remove it."
        else:
            text += "Watching <b>all of them</b>. Free seats per train right now; tap a train to watch only that one."
        kb = [[{"text": ("✅ " if not picked else "") + "All trains", "callback_data": f"tr:{t['id']}:*"}]]
        for r in rows[:12]:
            seats = sum(r["classes"].values())
            mark = "✅ " if r["number"] in picked else ""
            when = "" if len(r["dates"]) == len(t["dates"]) else f" · {len(r['dates'])}/{len(t['dates'])} days"
            kb.append([{"text": f"{mark}{r['number']}  {r['dep']} → {r['arr']}  · {seats} free{when}",
                        "callback_data": f"tr:{t['id']}:{r['number']}"}])
        kb.append([{"text": "Done", "callback_data": f"tr:{t['id']}:."}])
        return text, {"inline_keyboard": kb}

    def _train_pick(self, arg, mid):
        trip_id, _, number = arg.partition(":")
        t = self.store.trip(trip_id)
        if not t:
            return self.edit(mid, "That trip no longer exists.")
        if number == ".":
            what = "only " + ", ".join(t["trains"]) if t.get("trains") else "all trains"
            return self.edit(mid, f"🚆 Watching {H(what)} for {H(t['from']['name'].title())} → "
                                  f"{H(t['to']['name'].title())}.")
        trains = list(t.get("trains") or [])
        if number == "*":
            trains = []
        elif number in trains:
            trains.remove(number)
        else:
            trains.append(number)
        t = self.store.upsert_trip(dict(t, trains=trains))
        rec.ev("trip_trains", trip=trip_id, trains=trains)
        rows = getattr(self, "_trains_cache", {}).get(trip_id)
        if rows is None:
            return self.offer_trains(trip_id, mid)
        text, kb = self._trains_msg(t, rows)
        self.edit(mid, text, kb)

    # ---------------------------------------------------------------- engine notifications
    def _hold_kb(self, h):
        new = {"text": "🔁 Get a payment link", "callback_data": f"paylink:{h['order']}"}
        rows = [[{"text": f"💳 Pay {fmt_sum(h['total'])} so'm", "url": h["pay_url"]}] if h.get("pay_url") else [new],
                [{"text": "🔄 Transfer", "callback_data": f"xfer:{h['order']}"},
                 {"text": "❌ Cancel", "callback_data": f"cancel:{h['order']}"}]]
        if h.get("pay_url"):        # the bank page can stop working: a new one beats cancelling a good hold
            rows.append([dict(new, text="🔁 Pay page not opening? New link")])
        return {"inline_keyboard": rows}

    def _transfer_cb(self, kind, arg, mid):
        """Give a held seat to another saved passenger: pick who gives it up (if several), who gets it, confirm."""
        parts = arg.split(":")
        oid = parts[0]
        h = self.store.state["holds"].get(oid)
        if kind == "xback":
            return self.edit(mid, self._pay_text(h), self._hold_kb(h)) if h else self.edit(mid, "This reservation ended.")
        if not h:
            return self.edit(mid, "This reservation is no longer active.")
        names = {p["id"]: p["name"] for p in self.engine.passenger_list()}
        back = [{"text": "↩️ Back", "callback_data": f"xback:{oid}"}]
        known = list(names) + list(h["passengers"])      # ids travel as 8-character prefixes (64-byte button limit)
        parts = [oid] + [next((i for i in known if i.startswith(x)), x) for x in parts[1:]]
        seat_of = {pid: (h["seats"][i] if i < len(h.get("seats") or []) else "?") for i, pid in enumerate(h["passengers"])}
        if kind == "xfer" and len(h["passengers"]) > 1:
            rows = [[{"text": f"{names.get(pid, 'Passenger')} (seat {seat_of[pid]})", "callback_data": f"xfrom:{oid}:{pid[:8]}"}]
                    for pid in h["passengers"]] + [back]
            return self.edit(mid, "🔄 <b>Transfer a seat</b>\nWhose seat should go to someone else?", {"inline_keyboard": rows})
        if kind in ("xfer", "xfrom"):
            frm = parts[1] if kind == "xfrom" else h["passengers"][0]
            others = self.engine.transfer_targets(oid)
            if not others:
                return self.edit(mid, "No other saved passenger can take this seat (everyone else already has a seat "
                                      "or a ticket). Add people on eticket.uzrailpass.uz (My fellow travelers), then "
                                      "send /passengers.", {"inline_keyboard": [back]})
            rows = [[{"text": f"→ {p['name']} · " + (f"#{p['pos']} in this trip" if p["pos"] else "not in this trip"),
                      "callback_data": f"xto:{oid}:{frm[:8]}:{p['id'][:8]}"}] for p in others[:12]] + [back]
            return self.edit(mid, f"🔄 <b>Transfer seat {H(str(seat_of.get(frm, '?')))}</b> of "
                                  f"{H(names.get(frm, 'this passenger'))}\nWho should get it?", {"inline_keyboard": rows})
        frm, to = parts[1], parts[2]
        if kind == "xto":
            return self.edit(mid, (
                f"🔄 Move seat <b>{H(str(seat_of.get(frm, '?')))}</b> (train {H(h['train'])}, car {H(h['car'])}) from "
                f"<b>{H(names.get(frm, '?'))}</b> to <b>{H(names.get(to, '?'))}</b>?\n\n"
                "I cancel this reservation and at once re-book the same seat for the new passenger. This usually works, "
                "because everyone else sees a freed seat only 2 to 9 s later. If the same seat doesn't come back I take "
                f"another seat in the car or class. In the trip, {H(names.get(to, 'the new passenger'))} takes "
                f"{H(names.get(frm, 'their'))}'s place."
                + (f"\n\n⚠️ This reservation has {len(h['passengers'])} passengers: all its seats are released and "
                   "re-booked together, so seat numbers may change." if len(h["passengers"]) > 1 else "")),
                {"inline_keyboard": [[{"text": "✅ Move the seat", "callback_data": f"xgo:{oid}:{frm[:8]}:{to[:8]}"}], back]})
        # xgo: do it (takes a few seconds), without blocking the chat
        self.edit(mid, f"🔄 Moving the seat to <b>{H(names.get(to, '?'))}</b>…")

        def go():
            ok, msg = self.engine.transfer_hold(oid, frm, to)
            rec.ev("user_transfer", order=oid, ok=ok, source="telegram")
            self.transfer_done(h, names.get(to, "?"), ok, msg, also_edit=mid)
        threading.Thread(target=go, daemon=True).start()

    def transfer_done(self, h, to_name, ok, msg, also_edit=None):
        """After a transfer from Telegram or the PC app: update the old Pay message and say what happened."""
        still = self.store.state["holds"].get(h["order"])      # the transfer stopped before releasing the seat
        text = (f"🔄 Seat moved to <b>{H(to_name)}</b>. " + (f"{H(msg)} " if msg else "")
                + "The new reservation is below: pay there.") if ok else f"⚠️ The seat didn't go to <b>{H(to_name)}</b>. {H(msg)}"
        for mid in {h.get("msg_id"), also_edit} - {None}:
            if still:
                self.edit(mid, self._pay_text(still), self._hold_kb(still))
            else:
                self.edit(mid, text)
        if not ok:
            self.send(text)          # a new message, so it is seen even when the transfer started in the PC app

    def _hold_text(self, h, head="✅ <b>Seats reserved!</b>"):
        seats = []
        for i, name in enumerate(h["names"]):
            seat = h["seats"][i] if i < len(h["seats"]) else "?"
            types = h.get("seat_types") or []
            berth = {"В": " (upper)", "Н": " (lower)"}.get(types[i] if i < len(types) else None, "")
            seats.append(f"   • {H(name)}: seat {H(str(seat))}{berth}")
        when = fmt_trip_time(h.get("dep"), h.get("arr")) or H(h.get("date") or "")
        return (f"{head}\n\n🚆 Train <b>{H(h['train'])}</b> · {H(h['from'].title())} → {H(h['to'].title())}\n"
                f"🕒 {when}\n🚃 Car <b>{H(h['car'])}</b> · {label(h['cls'])}\n" + "\n".join(seats) +
                f"\n💰 Total <b>{fmt_sum(h['total'])} so'm</b>"
                + f"\n<i>held {h.get('took', '?')} s after the seats appeared · order {H(h['order'])}</i>")

    def _pay_text(self, h):
        left = h["expires_at"] - time.time()
        until = time.strftime("%H:%M", time.localtime(h["expires_at"]))
        return (self._hold_text(h) + f"\n\n⏳ <b>{fmt_left(left)}</b> left to pay (until {until}). Not paid in time: "
                "the seats go back on sale and I start searching again."
                + ("\n\n⚠️ The bank closed its payment page. Tap <b>Get a payment link</b> for a new one."
                   if h.get("link_dead") else ""))

    def reserved(self, h):
        # One card with everything; the "tickets appeared" note goes away. A silent line follows because only a
        # plain message can refresh the Mini App's data (a message with Pay buttons can't carry the keyboard).
        self.out.put(("delfound", None, None, (h["trip"], h.get("date")), None, None))

        def remember(mid):
            if h["order"] in self.store.state["holds"]:
                self.store.state["holds"][h["order"]]["msg_id"] = mid
                self.store.save()
        self.send(self._pay_text(h), self._hold_kb(h), on_sent=remember)
        self.out.put(("quiet", "🚆 This seat is also listed in Trips &amp; settings.", None, None, None, None))

    def missed(self, trip, date, hit, why):
        self.out.put(("delfound", None, None, (trip["id"], date), None, None))
        self.send(f"❌ <b>Not this time</b> · train {H(hit['number'])}, {label(hit['cls'])}, {fmt_day(date)}: "
                  f"{H(why)}.\nStill watching.")

    def hold_update(self, h):
        if h.get("msg_id"):
            self.edit(h["msg_id"], self._pay_text(h), self._hold_kb(h))

    def expired(self, h):
        if h.get("msg_id"):
            self.edit(h["msg_id"], "⌛ This reservation expired unpaid; the seats were released.")
        who = ", ".join(H(n) for n in h.get("names") or []) or "these passengers"
        self.send(f"⌛ The hold on train {H(h['train'])} (car {H(h['car'])}, seat{'s' if len(h.get('seats') or []) > 1 else ''} {H(', '.join(h.get('seats') or []) or '?')}) "
                  f"expired unpaid and went back on sale. I'm searching again for {who}, and I'll grab the seat if it "
                  "shows up again.")

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
                 f"{fmt_day(date)}"]
        for h in hits[:12]:
            lines.append(f"• Train {H(h['number'])} {H(h['train'].get('departureDate', '')[-5:])}→"
                         f"{H(h['train'].get('arrivalDate', '')[-5:])} · {label(h['cls'])}: "
                         f"<b>{h['free']}</b> free · {fmt_sum(h['price'])} so'm")
        if buying:
            lines.append("\n⚡ Booking now…")
            key = (trip["id"], date)
            return self.send("\n".join(lines), False, on_sent=lambda mid: self.found.__setitem__(key, mid))
        self.send("\n".join(lines), {"inline_keyboard": [[{"text": "Open the railway site",
                                                           "url": "https://eticket.uzrailpass.uz/en/pages/trains-page"}]]})

    def problem(self, text):
        self.send(f"⚠️ {H(text)}")

    def info(self, text):
        self.send(f"ℹ️ {H(text)}")
