"""The watcher/booker. Polls the railway, books the moment seats appear, holds them for payment.

Timing facts this design rests on (measured 2026-11-06, see docs/RESEARCH.md):
- the server closes idle connections after ~1 s -> poll every 0.8 s so connections stay warm;
- a freed seat shows in the train list ~1.8 s after it is freed; the car lookup lags ~1.6 s more
  (3-5 s cache) -> when seats JUST appeared, reserve straight on the train's known cars ("fan-out");
- reserve answers in ~80 ms, the railway decides ~1.1-2 s later (websocket PAYMENT_STARTED carries
  the whole order with seat numbers, ORDER_FAILED = not held);
- get-snapshot is rate-limited hard -> never polled;
- a hold lasts 12 minutes.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import eticket
from .eticket import EticketError, LoginError, Maintenance, RateLimited
from .recorder import rec

log = logging.getLogger("uzrailbot.engine")

BASE_INTERVAL = 0.8          # seconds between checks of one route+date at speed 1
MAX_FANOUT = 6               # parallel reserves when seats just appeared
ATTEMPTS_PER_DETECTION = 3
HOLD_UPDATE_EVERY = 60       # countdown message refresh
PAID_STATES = ("PAYMENT_SUCCESS", "ORDER_COMPLETED_SUCCESSFULLY", "ORDER_COMPLETED")
TZ_TASHKENT = dt.timezone(dt.timedelta(hours=5))


def today_tashkent() -> str:
    return dt.datetime.now(TZ_TASHKENT).date().isoformat()


def hhmm(stamp) -> str:
    """'07.11.2026 21:05' -> '21:05'."""
    return str(stamp or "")[-5:]


def board_row(tr) -> dict:
    """One train of a check, for the live board: free seats and lowest price per class."""
    classes = {}
    for car in tr.get("cars") or []:
        tfs = car.get("tariffs") or []
        for tf in tfs:
            c = tf.get("classServiceType")
            free = int(car.get("freeSeats") or 0) if len(tfs) == 1 else int(tf.get("freeSeats") or 0)
            price = int(tf.get("tariff") or 0)
            if c in classes:
                classes[c]["free"] += free
                classes[c]["price"] = min(classes[c]["price"], price) if classes[c]["price"] else price
            else:
                classes[c] = {"free": free, "price": price}
    return {"number": tr.get("number"), "dep": tr.get("departureDate"), "arr": tr.get("arrivalDate"),
            "brand": tr.get("brand"), "classes": classes}


def match_train(trip: dict, train: dict) -> list:
    """Classes on this train that match the trip: [{cls, price, free, carType}]."""
    if trip.get("trains") and str(train.get("number", "")) not in trip["trains"]:
        return []
    out = []
    for car in train.get("cars") or []:
        tariffs = car.get("tariffs") or []
        for tf in tariffs:
            cls, price = tf.get("classServiceType"), int(tf.get("tariff") or 0)
            if trip["classes"] and cls not in trip["classes"]:
                continue
            if trip["maxPrice"] and price > trip["maxPrice"]:
                continue
            free = int(car.get("freeSeats") or 0) if len(tariffs) == 1 else int(tf.get("freeSeats") or 0)
            if free > 0:
                out.append({"cls": cls, "price": price, "free": free, "carType": car.get("type")})
    return out


def pick_seats(places: list, k: int) -> list:
    places = sorted(int(p) for p in places)
    best = None
    for i in range(len(places) - k + 1):
        span = places[i + k - 1] - places[i]
        if best is None or span < best[0]:
            best = (span, places[i:i + k])
    return best[1] if best else []


def _name(f: dict) -> str:
    return f"{f.get('firstname', '')} {f.get('lastname', '')}".strip()


class Engine:
    def __init__(self, store, notifier, headless: bool = False):
        self.store = store
        self.ui = notifier
        self.headless = headless       # console test mode: no Telegram bot configured
        self.client: eticket.Client | None = None
        self.live: eticket.Live | None = None
        self.pool = ThreadPoolExecutor(max_workers=24, thread_name_prefix="uzrail")
        self.stop_ev = threading.Event()
        self.lock = threading.RLock()
        self.friends = {}
        self.friends_at = 0.0
        self.inflight = set()          # poll keys being fetched
        self.next_due = {}             # poll key -> monotonic time
        self.backoff = {}              # poll key -> monotonic time to resume
        self.booking = set()           # trip ids with a booking in progress
        self.prev_free = {}            # (trip, date, train, cls) -> free seats at the previous check
        self.polled = set()            # (trip, date) checked at least once
        self.last_notice = {}          # (trip, date) -> time of last "seats found" message
        self.learn_at = {}             # (train, cls) -> last learn attempt
        self.moving_pax = set()        # passengers in the middle of a transfer: never book them elsewhere
        self.hold_mono = {}            # order id -> monotonic deadline (immune to PC clock changes)
        self.ours = set()              # orders we cancel/replace ourselves (ignore their ORDER_FAILED)
        self.status = {"state": "starting", "detail": "", "last_check": 0.0, "checks": 0,
                       "account": "", "live": False}
        self.board = {}                # (from, to, date) -> what the last check saw: trains, classes, free, price
        self.recent_held = {}          # (date, train, cls) -> [(time, seats)] we just took (the list lags)
        self.fail_streak = 0
        self.offline_since = 0.0
        self.offline_alerted = False
        self.stats = {"since": time.time(), "detections": 0, "holds": 0, "missed": 0, "fastest": None}
        self.client_lock = threading.RLock()   # one login at a time (the poller, Telegram and the app all call it)
        self.warm_at = 0.0                     # last time we logged in just to load the saved passengers
        self._alerted = {}

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        threading.Thread(target=self._poll_loop, name="poller", daemon=True).start()
        threading.Thread(target=self._hold_loop, name="holds", daemon=True).start()

    def stop(self):
        self.stop_ev.set()
        if self.live:
            self.live.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _submit(self, fn, *args):
        if self.stop_ev.is_set():
            return None
        try:
            return self.pool.submit(fn, *args)
        except RuntimeError:
            return None

    def _set(self, state: str, detail: str = ""):
        if (state, detail) != (self.status["state"], self.status["detail"]):
            rec.ev("state", state=state, detail=detail)
        self.status.update(state=state, detail=detail)

    def _alert_once(self, key: str, text: str, every: float = 1800):
        now = time.time()
        if now - self._alerted.get(key, 0) > every:
            self._alerted[key] = now
            self.ui.problem(text)

    def account_changed(self):
        with self.lock:
            if self.live:
                self.live.stop()
            self.client, self.live, self.friends, self.friends_at = None, None, {}, 0.0

    def _ensure_client(self) -> bool:
        with self.client_lock:
            return self._ensure_client_locked()

    def _ensure_client_locked(self) -> bool:
        email, password = self.store.railway_login()
        if not email or not password:
            self._set("setup", "connect your railway account in Telegram: 🚆 Trips & settings → Account")
            return False
        if self.client is None or self.client.email != email:
            self.client = eticket.Client(email, password)
            self.live = None
        try:
            self.client.ensure_login()
        except LoginError as e:
            self._set("error", str(e))
            self._alert_once("login", f"I can't log in to the railway site: {e}. Send /account to fix it.")
            return False
        except EticketError as e:
            self._set("error", str(e))
            return False
        if self.live is None:
            self.live = eticket.Live(self.client, on_event=self._on_live)
            self.live.start()
            self._recover()
        self.status.update(account=email, live=self.live.connected)
        if time.time() - self.friends_at > 900:
            self.refresh_friends()
        return True

    def refresh_friends(self):
        try:
            self.friends = {str(f["friendId"]): f for f in self.client.friends()}
            self.friends_at = time.time()
        except EticketError as e:
            log.warning("saved passengers: %s", e)
        return self.friends

    def passenger_list(self) -> list:
        """Saved passengers ("My fellow travelers"). Loads them first if the account is set but they aren't
        loaded yet (e.g. right after a restart with no active trips), so the Mini App never opens empty."""
        email, password = self.store.railway_login()
        if not self.friends and email and password and time.time() - self.warm_at > 20:
            self.warm_at = time.time()
            self._ensure_client()
        return [{"id": k, "name": _name(f)} for k, f in self.friends.items()]

    def wait_account(self, timeout: float = 8) -> bool:
        """At start-up: give the login + saved passengers a few seconds before the first message goes out."""
        email, password = self.store.railway_login()
        end = time.time() + timeout
        while email and password and not self.friends and time.time() < end and not self.stop_ev.is_set():
            time.sleep(0.25)
        return bool(self.friends)

    def _warm(self):
        """Log in and load the saved passengers even when nothing is being watched (no trips / paused)."""
        email, password = self.store.railway_login()
        if email and password and (not self.friends or time.time() - self.friends_at > 900) \
                and time.time() - self.warm_at > 60:
            self.warm_at = time.time()
            self._submit(self._ensure_client)

    def _trip_live(self, trip_id: str):
        if self.stop_ev.is_set() or self.store.cfg.get("paused"):
            return None
        t = self.store.trip(trip_id)
        return t if t and t["active"] else None

    # ---------------------------------------------------------------- crash recovery
    def _recover(self):
        st = self.store.state
        for oid in list(st.get("pending", [])):
            if oid not in st["holds"]:
                try:
                    self.client.cancel(oid)
                    rec.ev("recover_cancel", order=oid)
                    log.info("cancelled leftover order %s from an earlier run", oid)
                except EticketError:
                    pass
        st["pending"] = []
        for oid, h in list(st["holds"].items()):
            try:
                left = self.client.seconds_left(oid)
            except EticketError:
                left = None
            if not left or left <= 0:
                st["holds"].pop(oid, None)
                rec.ev("recover_drop_hold", order=oid)
            else:
                h["expires_at"] = time.time() + left
                self.hold_mono[oid] = time.monotonic() + left
                rec.ev("recover_hold", order=oid, left=left)
        self.store.save()

    def _remember_pending(self, oid: str, add: bool = True):
        with self.store.lock:
            p = self.store.state.setdefault("pending", [])
            if add and oid not in p:
                p.append(oid)
            elif not add and oid in p:
                p.remove(oid)
            self.store.save()

    # ---------------------------------------------------------------- polling
    def _poll_loop(self):
        from .power import keep_awake
        awake_at = 0.0
        while not self.stop_ev.is_set():
            try:
                if time.time() - awake_at > 60:          # keep the PC awake while watching
                    awake_at = time.time()
                    keep_awake(self.status["state"] == "watching")
                if not self.headless and not self.store.is_configured():
                    self._set("setup", "finish setup first")
                    time.sleep(1)
                    continue
                self._warm()
                if self.store.cfg.get("paused"):
                    self._set("paused", "paused")
                    time.sleep(0.5)
                    continue
                trips = [t for t in self.store.trips() if t["active"]]
                if not trips:
                    self._set("idle", "no active trips: add one in Telegram (🚆 Trips & settings)")
                    time.sleep(1)
                    continue
                if not self._ensure_client():
                    time.sleep(15)
                    continue
                self._finish_old_trips(trips)
                keys = {}
                today = today_tashkent()
                for t in trips:
                    if t["mode"] == "buy" and not self._remaining(t):
                        continue
                    for d in t["dates"]:
                        if d >= today:
                            keys.setdefault((t["from"]["code"], t["to"]["code"], d), []).append(t)
                now = time.monotonic()
                for k, ts in keys.items():
                    interval = BASE_INTERVAL / max(t["speed"] for t in ts)
                    if k in self.inflight or now < self.next_due.get(k, 0) or now < self.backoff.get(k, 0):
                        continue
                    self.next_due[k] = now + interval
                    self.inflight.add(k)
                    if not self._submit(self._check, k, ts):
                        self.inflight.discard(k)
                if self.status["state"] in ("starting", "idle", "setup", "paused", "error", "maintenance"):
                    self._set("watching", f"{len(keys)} route-date(s)")
                self.status["live"] = bool(self.live and self.live.connected)
                time.sleep(0.02)
            except Exception:
                log.exception("poll loop")
                time.sleep(1)

    def _finish_old_trips(self, trips):
        today = today_tashkent()
        for t in trips:
            if all(d < today for d in t["dates"]):
                self.store.set_trip_active(t["id"], False)
                self.ui.info(f"Trip {t['from']['name']} → {t['to']['name']} has no future dates left, so I stopped it.")

    def _check(self, key, trips):
        frm, to, date = key
        t0 = time.perf_counter()
        try:
            trains = self.client.trains(date, frm, to)
            self.status["last_check"] = time.time()
            self.status["checks"] += 1
            self.fail_streak = 0
            if self.offline_alerted:
                self.offline_alerted = False
                self.ui.info("Connection to the railway site is back. Watching again.")
            self.offline_since = 0.0
            if self.status["state"] != "watching":
                self._set("watching", "")
        except Maintenance as e:
            self.backoff[key] = time.monotonic() + 60
            self._set("maintenance", str(e))
            self._alert_once("maint", "The railway site is in maintenance; I'll keep trying every minute.", 3600)
            return
        except RateLimited:
            self.backoff[key] = time.monotonic() + 30
            self._set("error", "the railway site asked us to slow down (30 s pause)")
            return
        except Exception as e:
            self.backoff[key] = time.monotonic() + 2
            self.status["detail"] = f"check failed: {type(e).__name__}"
            rec.ev("check_fail", key=list(key), error=f"{type(e).__name__}: {e}"[:200])
            self.fail_streak += 1
            if self.fail_streak >= 3:
                self.offline_since = self.offline_since or time.time()
                self._set("offline", "no answer from the railway site: check the internet connection")
                if not self.offline_alerted and time.time() - self.offline_since > 120:
                    self.offline_alerted = True
                    self.ui.problem("No answer from the railway site for 2 minutes. Is this PC's internet "
                                    "working? I keep trying and tell you when it's back.")
            return
        finally:
            self.inflight.discard(key)
        ms = round((time.perf_counter() - t0) * 1000)
        seen = {}
        for tr in trains:
            seen[tr["number"]] = {tf.get("classServiceType"): (int(car.get("freeSeats") or 0) if len(car.get("tariffs") or []) == 1
                                                                 else int(tf.get("freeSeats") or 0))
                                  for car in tr.get("cars") or [] for tf in car.get("tariffs") or []}
        rec.ev("check", date=date, route=[frm, to], ms=ms, n=self.status["checks"], trains=seen)
        self.board[key] = {"at": time.time(), "ms": ms, "trains": [board_row(tr) for tr in trains]}
        for trip in trips:
            hits = []
            for tr in trains:
                for m in match_train(trip, tr):
                    hits.append(dict(m, train=tr, number=tr["number"]))
            first = (trip["id"], date) not in self.polled
            self.polled.add((trip["id"], date))
            fresh = []
            now_keys = set()
            for h in hits:
                k = (trip["id"], date, h["number"], h["cls"])
                now_keys.add(k)
                before = self.prev_free.get(k, 0)
                if before != h["free"]:
                    rec.ev("availability", trip=trip["id"], date=date, train=h["number"], cls=h["cls"],
                           free=h["free"], before=before, price=h["price"], check_ms=ms, first_check=first)
                if before == 0 and not first:
                    fresh.append(h)
                self.prev_free[k] = h["free"]
            for k in [k for k in self.prev_free if k[0] == trip["id"] and k[1] == date and k not in now_keys]:
                if self.prev_free[k]:
                    rec.ev("availability", trip=trip["id"], date=date, train=k[2], cls=k[3], free=0,
                           before=self.prev_free[k], check_ms=ms)
                self.prev_free[k] = 0
            if trip["mode"] == "notify":
                if (fresh or (first and hits)) and time.time() - self.last_notice.get((trip["id"], date), 0) > 20:
                    self.last_notice[(trip["id"], date)] = time.time()
                    self.ui.seats_found(trip, date, hits)
            elif any(self._effective_free(date, h) > 0 for h in hits):
                hits = [dict(h, free=self._effective_free(date, h)) for h in hits if self._effective_free(date, h) > 0]
                with self.lock:
                    if trip["id"] in self.booking:
                        continue
                    self.booking.add(trip["id"])
                self.stats["detections"] += 1
                rec.ev("detect", trip=trip["id"], date=date, fresh=bool(fresh), check_ms=ms,
                       hits=[{"train": h["number"], "cls": h["cls"], "free": h["free"], "price": h["price"]} for h in hits])
                if not self._submit(self._book, trip["id"], date, hits, bool(fresh)):
                    with self.lock:
                        self.booking.discard(trip["id"])
                elif (fresh or first) and time.time() - self.last_notice.get((trip["id"], date), 0) > 30:
                    # message 1 of 3: "tickets appeared" (sent after booking already started: no delay)
                    self.last_notice[(trip["id"], date)] = time.time()
                    self.ui.seats_found(trip, date, hits)
            elif not hits:
                self._maybe_learn(trip, date, trains)

    def _effective_free(self, date, hit) -> int:
        """Free seats minus the ones we took in the last 30 s. The railway's train list keeps counting a seat
        we just held as free for 2-9 s; booking it again only fails (and used to send a 'missed')."""
        now = time.time()
        k = (date, hit["number"], hit["cls"])
        taken = [(t, n) for t, n in self.recent_held.get(k, []) if now - t < 30]
        self.recent_held[k] = taken
        return hit["free"] - sum(n for _, n in taken)

    # ---------------------------------------------------------------- car numbers, learned ahead of time
    def _cars_cache(self, number: str, cls: str, date: str = "") -> dict:
        """Known cars of a train+class. Car numbers change by date and route, so cars seen on THIS date
        come first, then cars seen on other dates."""
        cache = self.store.state.get("cars", {})
        out = dict(cache.get(f"{number}|{cls}|{date}", {})) if date else {}
        for k, v in cache.get(f"{number}|{cls}", {}).items():
            out.setdefault(k, v)
        return out

    def _learn(self, number: str, groups: list, date: str = ""):
        cache = self.store.state.setdefault("cars", {})
        for g in groups or []:
            for car in g.get("cars") or []:
                cls = (car.get("schema") or {}).get("service") or (g.get("services") or {}).get("type")
                if cls:
                    info = {"carType": g.get("type"), "service": cls}
                    cache.setdefault(f"{number}|{cls}", {})[str(car["number"])] = info
                    if date:
                        cache.setdefault(f"{number}|{cls}|{date}", {})[str(car["number"])] = info

    def _maybe_learn(self, trip, date, trains):
        """Learn car numbers of watched trains in the background (one try per train per 10 min)."""
        if trip["mode"] != "buy":
            return
        for tr in trains:
            for cls in (trip["classes"] or [None]):
                if cls and self._cars_cache(tr["number"], cls):
                    continue
                key = (tr["number"], cls)
                if time.time() - self.learn_at.get(key, 0) < 600:
                    continue
                self.learn_at[key] = time.time()
                self._submit(self._learn_train, trip, date, tr["number"])
                return

    def _learn_train(self, trip, date, number):
        base = dt.date.fromisoformat(date)
        for k in (0, 1, 2, 3, 7, -1, -2):
            d = (base + dt.timedelta(days=k)).isoformat()
            if d < today_tashkent() or self.stop_ev.is_set():
                continue
            try:
                groups = self.client.cars(d, trip["from"]["code"], trip["to"]["code"], number)
            except EticketError:
                return
            if groups:
                self._learn(number, groups, d)
                self.store.save()
                rec.ev("learned_cars", train=number, from_date=d,
                       cars={k: list(v) for k, v in self.store.state["cars"].items() if k.startswith(number + "|")})
                # this train has cars, but none of the wanted classes (e.g. a fast sitting-only train when the
                # trip wants plaskart): leave it alone for 6 hours instead of asking again every 10 minutes
                for cls in trip["classes"]:
                    if not self._cars_cache(number, cls):
                        self.learn_at[(number, cls)] = time.time() + 6 * 3600
                return
            time.sleep(0.5)
        for cls in (trip["classes"] or [None]):     # sold out on every nearby date: try again in 30 min
            self.learn_at[(number, cls)] = time.time() + 1200

    # ---------------------------------------------------------------- booking
    def _remaining(self, trip) -> list:
        """Passengers of this trip still without a seat: not paid, not held (on this trip, or on another trip
        for the same route and date), and not in the middle of a transfer."""
        done = set(self.store.done_passengers(trip["id"]))
        held = set(self.moving_pax)
        route = (trip["from"]["code"], trip["to"]["code"])
        for h in self.store.state["holds"].values():
            if h["trip"] == trip["id"]:
                held.update(h["passengers"])
                continue
            other = self.store.trip(h["trip"])
            if other and (other["from"]["code"], other["to"]["code"]) == route and h.get("date") in trip["dates"]:
                held.update(h["passengers"])
        return [p for p in trip["passengers"] if p not in done and p not in held]

    def _book(self, trip_id, date, hits, fresh):
        try:
            trip = self._trip_live(trip_id)
            if not trip:
                return
            remaining = self._remaining(trip)
            if not self.friends:
                self.refresh_friends()
            people = [self.friends[p] for p in remaining if p in self.friends]
            missing = [p for p in remaining if p not in self.friends]
            if missing:
                self._alert_once(f"missing-{trip_id}", "Some passengers of a trip are no longer in your saved "
                                 "passengers on the railway site; I skip them.")
            if not people:
                return
            hits.sort(key=lambda h: (h["train"]["departureDate"], -h["free"]))
            for h in hits:
                if not people or not self._trip_live(trip_id):
                    break
                got = self._book_hit(trip, date, h, people, fresh)
                people = [p for p in people if str(p["friendId"]) not in got]
        except Exception as e:
            log.exception("booking failed")
            rec.ev("book_error", trip=trip_id, error=f"{type(e).__name__}: {e}"[:300])
        finally:
            with self.lock:
                self.booking.discard(trip_id)

    def _book_hit(self, trip, date, hit, people, fresh) -> set:
        """Try to hold seats for `people` on one train/class. Returns the friendIds held."""
        frm, to = trip["from"]["code"], trip["to"]["code"]
        number, cls = hit["number"], hit["cls"]
        party = people[:4]
        held = set()
        for attempt in range(ATTEMPTS_PER_DETECTION):
            if not self._trip_live(trip["id"]):
                break
            t0 = time.time()
            known = list(self._cars_cache(number, cls, date).items())[:MAX_FANOUT]
            orders = {}                      # order id -> (car, who, path)
            fan = bool(fresh and known and attempt == 0)
            if fan:
                # The most likely car (seen for this date) goes FIRST, on the connection the check just used
                # (warm, ~50 ms). Measured 2026-11-07: six parallel reserves on fresh connections took ~1 s.
                rec.ev("fanout", train=number, cls=cls, cars=[k for k, _ in known])
                first = {"number": known[0][0], **known[0][1]}
                oid = self._reserve(trip, hit, first, "1-99", party)
                if oid:
                    orders[oid] = (first, party, "fanout")
            cars_future = self._submit(self._cars_fresh, date, frm, to, number, hit["free"])
            if fan and len(known) > 1:
                for oid, car in self._fanout_fire(trip, hit, party, known[1:]):
                    orders[oid] = (car, party, "fanout")
            # Don't wait for the fan-out to fail: book the car the lookup names as soon as it answers.
            groups = cars_future.result() if cars_future is not None else []
            if groups:
                self._learn(number, groups, date)
            plan = self._plan(groups, cls, trip, party)
            rec.ev("plan", train=number, cls=cls, attempt=attempt + 1,
                   car=plan[0]["number"] if plan else None, seats=plan[1] if plan else None,
                   ms=round((time.time() - t0) * 1000), fanout_orders=len(orders))
            if plan and plan[0]["number"] not in {c["number"] for c, _, _ in orders.values()}:
                car, seats, who = plan
                oid = self._reserve(trip, hit, car, f"{min(seats)}-{max(seats)}", who)
                if oid:
                    orders[oid] = (car, who, "cars")
            if not orders:
                rec.ev("seats_gone", train=number, cls=cls, attempt=attempt + 1,
                       why="car lookup shows no matching seats")
                continue
            winner = self._settle_many(orders, t0)
            if winner:
                oid, car, who, data, path = winner
                self._finalize(trip, date, hit, oid, car, who, data, time.time() - t0, path)
                held.update(str(p["friendId"]) for p in who)
                party = [p for p in party if str(p["friendId"]) not in held]
                if not party:
                    return held
        if not held:
            self.stats["missed"] += 1
            rec.ev("missed", trip=trip["id"], train=number, cls=cls, why="seats taken before our hold settled")
            if time.time() - self.last_notice.get(("missed", trip["id"]), 0) > 60:
                self.last_notice[("missed", trip["id"])] = time.time()
                # message 2 of 3 (failure)
                self.ui.missed(trip, date, hit, "someone else got the seats first")
        return held

    def _cars_fresh(self, date, frm, to, number, expected_free):
        """Car lookup; its cache can lag the train list by ~1.6 s, so retry 'no seats' for up to 4 s."""
        end = time.time() + 4
        tries = 0
        while True:
            tries += 1
            try:
                groups = self.client.cars(date, frm, to, number)
            except EticketError:
                groups = []
            if groups or time.time() > end or not expected_free or self.stop_ev.is_set():
                if tries > 1:
                    rec.ev("cars_retry", train=number, tries=tries, found=bool(groups))
                return groups
            time.sleep(0.6)

    def _plan(self, groups, cls, trip, party):
        best = None
        for g in groups or []:
            if trip["maxPrice"] and int(g.get("tariff") or 0) > trip["maxPrice"]:
                continue
            for car in g.get("cars") or []:
                service = (car.get("schema") or {}).get("service")
                if service != cls or not car.get("places"):
                    continue
                n = min(len(party), len(car["places"]))
                if trip["together"] and n < len(party):
                    continue
                score = (n, -len(car["places"]))
                if best is None or score > best[0]:
                    best = (score, {"number": str(car["number"]), "carType": g.get("type"), "service": service},
                            pick_seats(car["places"], n), party[:n])
        return best[1:] if best else None

    def _reserve(self, trip, hit, car, seats_range, who):
        payload = eticket.build_reserve_payload(self.client, hit["train"], car, seats_range, who,
                                                trip["from"]["code"], trip["to"]["code"])
        t0 = time.perf_counter()
        try:
            oid = self.client.reserve(payload)
        except EticketError as e:
            msg = str(e)
            rec.ev("reserve_refused", train=hit["number"], car=car["number"], range=seats_range,
                   status=e.status, message=msg[:300])
            if e.status == 400:
                self._alert_once(f"refused-{trip['id']}-{msg[:40]}", f"The railway refused a booking: {msg}")
            return None
        rec.ev("reserve_sent", order=oid, train=hit["number"], car=car["number"], range=seats_range,
               passengers=len(who), ms=round((time.perf_counter() - t0) * 1000))
        self._remember_pending(oid)
        return oid

    def _fanout_fire(self, trip, hit, party, known):
        """Send a reserve to every given car of the class at once (seat range 1-99: the railway picks)."""
        fired = list(self.pool.map(
            lambda kv: (kv[0], kv[1], self._reserve(trip, hit, {"number": kv[0], **kv[1]}, "1-99", party)), known))
        return [(oid, {"number": no, **info}) for no, info, oid in fired if oid]

    def _settle_many(self, orders: dict, t0: float):
        """Wait for every order to be decided; keep the first that holds, cancel any extra holds."""
        decided = {}
        end = time.time() + 10
        while len(decided) < len(orders) and time.time() < end:
            for oid in orders:
                if oid in decided:
                    continue
                if self.live is not None and self.live.connected:
                    ev = self.live.seen(lambda d: eticket._ev_order(d) == oid
                                        and d.get("state") in eticket.HELD_STATES + eticket.FAILED_STATES, since=t0 - 1)
                    if ev:
                        decided[oid] = ("RESERVED" if ev["state"] in eticket.HELD_STATES else "FAILED",
                                        ev.get("data") or {}, ev["_t"])
                else:
                    res, data = eticket.settle(self.client, oid, None, timeout=10, since=t0)
                    decided[oid] = (res, data, time.time())
            time.sleep(0.03)
        winner = None
        for oid, (res, data, at) in sorted(decided.items(), key=lambda kv: kv[1][2]):
            car, who, path = orders[oid]
            rec.ev("settled", order=oid, car=car["number"], result=res, ms=round((at - t0) * 1000), path=path)
            if res == "RESERVED" and winner is None:
                winner = (oid, car, who, data, path)
            elif res == "RESERVED":
                self.ours.add(oid)
                self.client.cancel(oid)
                rec.ev("extra_hold_cancelled", order=oid, car=car["number"])
                self._remember_pending(oid, add=False)
            else:
                self._remember_pending(oid, add=False)
        for oid in orders:
            if oid not in decided:
                rec.ev("settled", order=oid, result="TIMEOUT", path=orders[oid][2])
                self.ours.add(oid)
                self.client.cancel(oid)          # unknown outcome: cancel to be safe
                self._remember_pending(oid, add=False)
        return winner

    def _finalize(self, trip, date, hit, oid, car, who, data, took, path):
        info = eticket.snapshot_summary(data) if data.get("items") else eticket.order_details(self.client, oid)
        try:
            left = self.client.seconds_left(oid) or 720
        except EticketError:
            left = 720
        hold = {
            "order": oid, "trip": trip["id"], "date": date, "train": hit["number"], "cls": hit["cls"],
            "car": car["number"], "car_info": {"carType": car.get("carType"), "service": car.get("service")},
            "passengers": [str(p["friendId"]) for p in who], "names": [_name(p) for p in who],
            "seats": info.get("seats") or [], "seat_types": info.get("seat_types") or [],
            "total": info.get("total") or hit["price"] * len(who),
            "dep": hit["train"].get("departureDate"), "arr": hit["train"].get("arrivalDate"),
            "from": trip["from"]["name"], "to": trip["to"]["name"],
            "expires_at": time.time() + left, "pay_url": "", "psid": "",
            "took": round(took, 2), "path": path, "msg_id": None, "last_update": time.time(),
        }
        with self.store.lock:                  # record first: from now on nothing books these passengers again
            self.store.state["holds"][oid] = hold
            self.hold_mono[oid] = time.monotonic() + left
            # car numbers change by date and route: remember the car that held for THIS date
            self.store.state.setdefault("cars", {}).setdefault(f"{hit['number']}|{hit['cls']}|{date}", {})[
                str(car["number"])] = {"carType": car.get("carType"), "service": car.get("service") or hit["cls"]}
            self.store.save()
        self._remember_pending(oid, add=False)
        if not self._attach_pay_link(hold, attempts=1):   # the Hamkorbank page, ~0.5 s normally
            self._submit(self._pay_link_later, oid)       # bank slow or failing: don't hold back the message
        self.store.save()
        self.recent_held.setdefault((date, hit["number"], hit["cls"]), []).append((time.time(), len(who)))
        self.stats["holds"] += 1
        if self.stats["fastest"] is None or took < self.stats["fastest"]:
            self.stats["fastest"] = round(took, 2)
        rec.ev("held", order=oid, train=hold["train"], car=hold["car"], seats=hold["seats"], took_s=hold["took"],
               path=path, passengers=len(who), total=hold["total"], left_s=left, pay_link=bool(hold["pay_url"]))
        log.info("HELD %s train %s car %s seats %s (%.2f s, %s)", oid, hold["train"], hold["car"], hold["seats"],
                 took, path)
        self.ui.reserved(hold)

    def _attach_pay_link(self, hold, attempts: int = 3):
        bot = self.store.cfg["telegram"].get("bot_username") or ""
        ok = f"https://t.me/{bot}?start=paid_{hold['order']}" if bot else eticket.BASE
        bad = f"https://t.me/{bot}?start=payfail_{hold['order']}" if bot else eticket.BASE
        for attempt in range(attempts):
            try:
                t0 = time.perf_counter()
                hold["psid"], hold["pay_url"] = eticket.make_pay_link(self.client, self.live, hold["order"], ok, bad)
                rec.ev("pay_link", order=hold["order"], ms=round((time.perf_counter() - t0) * 1000))
                return True
            except EticketError as e:
                rec.ev("pay_link_fail", order=hold["order"], attempt=attempt + 1, error=str(e)[:200])
                time.sleep(1)
        return False

    # ---------------------------------------------------------------- holds: countdown, expire, paid
    def _pay_link_later(self, oid):
        """Keep asking the bank for the payment page for about a minute; then put the Pay button on the message."""
        for wait in (3, 5, 10, 15, 25):
            time.sleep(wait)
            h = self.store.state["holds"].get(oid)
            if not h or h.get("pay_url"):
                return
            if self._attach_pay_link(h, attempts=1):
                self.store.save()
                h["last_update"] = time.time()
                self.ui.hold_update(h)                    # edits the Pay message: the button now opens the bank
                return

    def _hold_loop(self):
        while not self.stop_ev.is_set():
            try:
                for oid, h in list(self.store.state["holds"].items()):
                    if oid in self.hold_mono:
                        h["expires_at"] = time.time() + (self.hold_mono[oid] - time.monotonic())
                    left = h["expires_at"] - time.time()
                    if left <= 0:
                        # No renewals: the seat goes back on sale and these passengers are searched for again,
                        # so the watcher simply catches it (or another seat) when it shows up.
                        self._drop_hold(oid, "expired")
                        self.ui.expired(h)
                    elif time.time() - h.get("last_update", 0) >= HOLD_UPDATE_EVERY:
                        h["last_update"] = time.time()
                        self.ui.hold_update(h)
            except Exception:
                log.exception("hold loop")
            time.sleep(1)

    def _drop_hold(self, oid, why):
        with self.store.lock:
            h = self.store.state["holds"].pop(oid, None)
            self.store.save()
        if h:
            rec.ev("hold_end", order=oid, why=why)
        return h

    def transfer_hold(self, oid, from_pid: str, to_pid: str):
        """Give a held seat to another saved passenger: cancel the hold and at once re-book the very same
        seat(s) with `to_pid` in place of `from_pid` (everyone else sees a freed seat only after the railway's
        2-9 s list delay, so we are first). `to_pid` also moves to the top of the trip's priority list, so if
        the instant re-book fails the watcher looks for them first. `from_pid` stays in the trip.
        Returns (ok, message)."""
        h = self.store.state["holds"].get(oid)
        if not h or not self.client:
            return False, "That reservation is no longer active."
        if from_pid not in h["passengers"]:
            return False, "That passenger isn't on this reservation."
        if to_pid in h["passengers"]:
            return False, "That passenger is already on this reservation."
        if not self.friends:
            self.refresh_friends()
        if to_pid not in self.friends:
            return False, "That passenger isn't in your saved passengers any more."
        trip = self.store.trip(h["trip"])
        if not trip:
            return False, "The trip of this reservation was deleted."
        new_ids = [to_pid if p == from_pid else p for p in h["passengers"]]
        people = [self.friends[p] for p in new_ids if p in self.friends]
        # the new passenger goes first in the trip's priority; the old one stays (searched for again)
        order = [to_pid] + [p for p in trip["passengers"] if p != to_pid]
        if len(order) > 4:
            order = [p for p in order if p != from_pid][:4]
        trip = self.store.upsert_trip(dict(trip, passengers=order))
        trains = self.client.trains(h["date"], trip["from"]["code"], trip["to"]["code"])
        train = next((t for t in trains if t["number"] == h["train"]), None)
        if not train:
            return False, f"Train {h['train']} is no longer listed, so the seat can't be moved."
        price = int((h.get("total") or 0) / max(1, len(h["passengers"])))
        hit = {"number": h["train"], "cls": h["cls"], "price": price, "free": len(new_ids), "train": train}
        car = {"number": h["car"], **(h.get("car_info") or {})}
        seats = sorted(int(x) for x in h.get("seats") or [] if str(x).isdigit())
        rng = f"{seats[0]}-{seats[-1]}" if seats else "1-99"
        self.moving_pax.update(h["passengers"] + new_ids)
        try:
            self.ours.add(oid)
            t0 = time.time()
            self.client.cancel(oid)
            self._drop_hold(oid, "transferred")
            rec.ev("transfer_start", order=oid, train=h["train"], car=h["car"], range=rng)
            # re-booking before the railway has released the seat just fails (measured: the release came 70 ms
            # after the cancel; a reserve sent at 49 ms failed and cost a whole extra round of ~1.8 s)
            if self.live is not None and self.live.connected:
                self.live.wait_for(lambda d: eticket._ev_order(d) == oid and d.get("state") in eticket.FAILED_STATES,
                                   timeout=1.5, since=t0 - 1)
            # The railway confirms the release within ~0.1 s but the seat is bookable only ~1-2 s later
            # (measured), so send staggered attempts and keep the first that holds; _settle_many cancels any
            # extra hold. Two rounds at most.
            winner = None
            for rnd in range(2):
                orders, ts = {}, time.time()
                for k, delay in enumerate((0.1, 0.5, 0.9, 1.3, 1.8)):
                    time.sleep(max(0.0, ts + delay - time.time()))
                    new_oid = self._reserve(trip, hit, car, rng, people)
                    if new_oid:
                        orders[new_oid] = (car, people, "transfer")
                winner = self._settle_many(orders, ts) if orders else None
                rec.ev("transfer_attempt", attempt=rnd + 1, sent=len(orders), result="RESERVED" if winner else "FAILED",
                       ms=round((time.time() - t0) * 1000))
                if winner:
                    break
            if not winner:
                return False, ("The seat was released but someone else took it first. I'm searching for both "
                               "passengers again, the new one first.")
            new_oid, wcar, who, data, path = winner
            self._finalize(trip, h["date"], hit, new_oid, wcar, who, data, time.time() - t0, "transfer")
            return True, ""
        except EticketError as e:
            rec.ev("transfer_error", order=oid, error=str(e)[:200])
            return False, f"The railway refused: {e}"
        finally:
            self.moving_pax.difference_update(h["passengers"] + new_ids)

    def _on_live(self, d):
        oid = eticket._ev_order(d)
        state = d.get("state")
        h = self.store.state["holds"].get(oid)
        if not h:
            return
        if state in PAID_STATES and not h.get("paid"):
            h["paid"] = True
            self.store.mark_done(h["trip"], h["passengers"])
            self._drop_hold(oid, "paid")
            self._submit(self._deliver_ticket, h)
        elif state == "PAYMENT_FAIL":
            self.ui.payment_failed(h)
        elif state == "ORDER_FAILED" and oid not in self.ours:
            self._drop_hold(oid, "cancelled or expired on the railway side")
            self.ui.released(h, "The railway ended this hold (expired or cancelled on the site).")

    def _deliver_ticket(self, h):
        pdf = None
        for attempt in range(4):
            try:
                pdf = self.client.ticket_pdf(h["order"])
            except EticketError as e:
                rec.ev("pdf_fail", order=h["order"], attempt=attempt + 1, error=str(e)[:200])
            if pdf:
                break
            time.sleep(15)
        rec.ev("paid", order=h["order"], pdf=bool(pdf))
        self.ui.paid(h, pdf)

    # ---------------------------------------------------------------- actions from Telegram / tray
    def cancel_hold(self, oid) -> bool:
        h = self.store.state["holds"].get(oid)
        if not h or not self.client:
            return False
        self.store.set_trip_active(h["trip"], False)      # pause first, so nothing re-books the seat
        self.ours.add(oid)
        ok = self.client.cancel(oid)
        self._drop_hold(oid, "cancelled by the user")
        return ok

    def new_pay_link(self, oid):
        h = self.store.state["holds"].get(oid)
        if h and self._attach_pay_link(h):
            self.store.save()
            return h
        return None

    def user_returned_from_bank(self, oid):
        h = self.store.state["holds"].get(oid)
        if h and h.get("psid") and self.client:
            rec.ev("returned_from_bank", order=oid)
            self.client.hamkor_callback(h["psid"])

    def snapshot(self) -> dict:
        holds = []
        for h in self.store.state["holds"].values():
            holds.append({k: h.get(k) for k in ("order", "trip", "passengers", "train", "car", "seats", "seat_types",
                                                "names", "total", "from", "to", "date", "dep", "arr", "cls", "pay_url")}
                         | {"left": max(0, int(h["expires_at"] - time.time()))})
        active = {(t["from"]["code"], t["to"]["code"], d) for t in self.store.trips() if t["active"] for d in t["dates"]}
        board = [{"from": k[0], "to": k[1], "date": k[2], **v} for k, v in list(self.board.items()) if k in active]
        return dict(self.status, holds=holds, trips=self.store.trips(), board=board, stats=dict(self.stats),
                    offline_since=self.offline_since, calls=self.client.calls if self.client else 0)

    # ---------------------------------------------------------------- train list for the "which trains" picker
    def scan_trains(self, trip, fetch: bool = True) -> list:
        """Trains running on the trip's dates (from the latest checks, or asked now if fetch): one row per
        train number with departure/arrival times and the dates it was seen on."""
        if fetch and not self._ensure_client():
            return []
        out = {}
        today = today_tashkent()
        for d in [d for d in sorted(trip["dates"]) if d >= today][:6]:
            key = (trip["from"]["code"], trip["to"]["code"], d)
            b = self.board.get(key)
            if not b or (fetch and time.time() - b["at"] > 120):
                if not fetch:
                    continue
                try:
                    trains = self.client.trains(d, *key[:2])
                except EticketError:
                    continue
                b = {"at": time.time(), "ms": 0, "trains": [board_row(tr) for tr in trains]}
                self.board[key] = b
            for tr in b["trains"]:
                row = out.setdefault(tr["number"], {"number": tr["number"], "dep": hhmm(tr["dep"]),
                                                    "arr": hhmm(tr["arr"]), "dates": [], "classes": {}})
                row["dates"].append(d)
                for c, v in tr["classes"].items():
                    if not trip["classes"] or c in trip["classes"]:
                        row["classes"][c] = row["classes"].get(c, 0) + v["free"]
        return sorted(out.values(), key=lambda r: r["dep"])
