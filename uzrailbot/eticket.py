"""Client for eticket.uzrailpass.uz (the Uzbek Railways ticket site).

The site has no public API; everything here mirrors the requests its own web app sends.
See docs/RESEARCH.md for how each call was found and measured.
"""
from __future__ import annotations

import base64
import collections
import datetime as dt
import json
import logging
import re
import threading
import time

import requests
import websocket
from requests.adapters import HTTPAdapter

from .recorder import rec

BASE = "https://eticket.uzrailpass.uz"
WS_URL = "wss://eticket.uzrailpass.uz/ws"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TOKEN_REFRESH_LEFT = 25 * 60   # log in again when the 60-minute token has less than this left

# Request budgets. The site rate-limits per endpoint (HTTP 429, no Retry-After); get-snapshot is the
# strictest. Measured 2026-11-06: ~47 req/s overall tripped it.
GLOBAL_RATE = 10.0             # requests per second, sustained
GLOBAL_BURST = 20
ENDPOINT_MIN_GAP = {"/api/v3/universal-orders/get-snapshot": 1.0}   # seconds between calls
COOLDOWN_429 = 30.0            # pause an endpoint this long after a 429

log = logging.getLogger("uzrailbot.eticket")


class _Budget:
    """Token bucket for all calls plus per-endpoint spacing and 429 cooldowns."""

    def __init__(self):
        self.lock = threading.Lock()
        self.tokens = float(GLOBAL_BURST)
        self.stamp = time.monotonic()
        self.last_call = {}
        self.cooldown_until = {}

    def take(self, path: str, priority: bool = False):
        key = path.split("?")[0]
        if not key.startswith("/api/v1/document/") and "/payment-end-time/" not in key:
            key = re.sub(r"/[A-Z0-9_]{10,}$", "", key)
        while True:
            with self.lock:
                now = time.monotonic()
                if self.cooldown_until.get(key, 0) > now:
                    raise RateLimited(f"waiting after a rate limit on {key}", 429)
                self.tokens = min(GLOBAL_BURST, self.tokens + (now - self.stamp) * GLOBAL_RATE)
                self.stamp = now
                gap = ENDPOINT_MIN_GAP.get(key, 0) - (now - self.last_call.get(key, -1e9))
                if (self.tokens >= 1 or priority) and gap <= 0:
                    self.tokens -= 1
                    self.last_call[key] = now
                    return
                wait = max(gap, (1 - self.tokens) / GLOBAL_RATE, 0.01)
            time.sleep(min(wait, 0.5))

    def hit_429(self, path: str):
        with self.lock:
            self.cooldown_until[path.split("?")[0]] = time.monotonic() + COOLDOWN_429


class EticketError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class LoginError(EticketError):
    pass


class Maintenance(EticketError):
    pass


class RateLimited(EticketError):
    pass


def jwt_exp(token: str) -> float:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return float(json.loads(base64.urlsafe_b64decode(part))["exp"])
    except Exception:
        return 0.0


def split_dmy(value: str):
    """'12.11.2026 21:45' -> ('2026-11-12', '21:45:00')"""
    day, clock = value.split()
    dd, mm, yy = day.split(".")
    return f"{yy}-{mm}-{dd}", clock + ":00"


def _message(r: requests.Response) -> str:
    try:
        j = r.json()
        for k in ("message", "detail", "error", "messageEn"):
            if isinstance(j, dict) and j.get(k):
                return str(j[k])[:300]
    except ValueError:
        pass
    return (r.text or f"HTTP {r.status_code}")[:300]


class Client:
    def __init__(self, email: str = "", password: str = "", lang: str = "en"):
        self.email = email
        self.password = password
        self.s = requests.Session()
        self.s.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=32, max_retries=0))
        self.s.headers.update({
            "User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json",
            "device-type": "BROWSER", "Accept-Language": lang, "Origin": BASE, "Referer": BASE + "/",
        })
        self.token = ""
        self.user_id = ""
        self.token_exp = 0.0
        self._lock = threading.RLock()
        self._csrf_ready = False
        self.budget = _Budget()
        self.calls = 0

    # ---------------------------------------------------------------- plumbing
    def _csrf(self):
        self.s.get(BASE + "/api/v1/csrf-token", timeout=15)
        x = self.s.cookies.get("XSRF-TOKEN")
        if x:
            self.s.headers["X-XSRF-TOKEN"] = x
        self._csrf_ready = True

    def request(self, method: str, path: str, body=None, auth: bool = False, timeout: float = 15,
                _retry: bool = True, priority: bool = False) -> requests.Response:
        if not self._csrf_ready:
            with self._lock:
                if not self._csrf_ready:
                    self._csrf()
        if auth:
            self.ensure_login()
        self.budget.take(path, priority)
        headers = {"Authorization": "Bearer " + self.token} if self.token else {}
        self.calls += 1
        t0 = time.perf_counter()
        try:
            r = self.s.request(method, BASE + path, json=body, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            rec.ev("http_fail", path=path, ms=round((time.perf_counter() - t0) * 1000), error=type(e).__name__,
                   retry=_retry and isinstance(e, requests.ConnectionError))
            # The server closes idle keep-alive connections after ~1 s; the first request on such a
            # connection fails at once. Resend once on a fresh connection.
            if not _retry or not isinstance(e, requests.ConnectionError):
                raise
            return self.request(method, path, body, auth, timeout, _retry=False, priority=priority)
        rec.ev("http", m=method, path=path, status=r.status_code, ms=round((time.perf_counter() - t0) * 1000),
               bytes=len(r.content))
        if r.status_code == 401 and auth and _retry:
            self.login()
            return self.request(method, path, body, auth, timeout, _retry=False, priority=priority)
        if r.status_code == 403 and _retry:
            self._csrf()
            return self.request(method, path, body, auth, timeout, _retry=False, priority=priority)
        if r.status_code == 424:
            raise Maintenance("the railway site is in maintenance: " + _message(r), 424, r.text)
        if r.status_code == 429:
            self.budget.hit_429(path)
            raise RateLimited("the railway site is limiting requests on " + path.split("?")[0], 429, r.text)
        return r

    def _json(self, r: requests.Response, what: str):
        if r.status_code == 204 or not r.content:
            return None
        if r.status_code >= 400:
            raise EticketError(f"{what}: {_message(r)}", r.status_code, r.text)
        try:
            return r.json()
        except ValueError:
            raise EticketError(f"{what}: unexpected answer", r.status_code, r.text[:300])

    # ---------------------------------------------------------------- account
    def login(self):
        with self._lock:
            if not self.email or not self.password:
                raise LoginError("railway email/password not set")
            if not self._csrf_ready:
                self._csrf()
            r = self.s.post(BASE + "/api/v1/auth/login", json={"username": self.email, "password": self.password},
                            timeout=20)
            try:
                j = r.json()
            except ValueError:
                j = {}
            if r.status_code != 200 or not j.get("token"):
                raise LoginError("railway login failed: " + _message(r), r.status_code, r.text[:300])
            self.token, self.user_id = j["token"], str(j.get("userId") or "")
            self.token_exp = jwt_exp(self.token)
            log.info("logged in to the railway site (token valid %d min)", (self.token_exp - time.time()) / 60)

    def ensure_login(self, min_left: float = TOKEN_REFRESH_LEFT):
        if not self.token or self.token_exp - time.time() < min_left:
            with self._lock:
                if not self.token or self.token_exp - time.time() < min_left:
                    self.login()

    def friends(self) -> list:
        self.ensure_login()
        j = self._json(self.request("POST", "/api/v1/users/friend/list", {"userId": self.user_id}, auth=True),
                       "saved passengers")
        return j or []

    # ---------------------------------------------------------------- lookups
    def stations(self, query: str) -> list:
        j = self._json(self.request("POST", "/api/v1/handbook/stations/list", {"name": query}), "station search")
        return ((j or {}).get("data") or {}).get("stations") or []

    def trains(self, date: str, frm: str, to: str, timeout: float = 10) -> list:
        body = {"directions": {"forward": {"date": date, "depStationCode": frm, "arvStationCode": to}},
                "routeType": "INTERCITY"}
        j = self._json(self.request("POST", "/api/v3/handbook/trains/list", body, timeout=timeout), "train list")
        return ((((j or {}).get("data") or {}).get("directions") or {}).get("forward") or {}).get("trains") or []

    def cars(self, date: str, frm: str, to: str, train_no: str, timeout: float = 10) -> list:
        body = {"depDate": date, "depStationCode": frm, "arvStationCode": to, "trainNumber": train_no, "trainId": None}
        j = self._json(self.request("POST", "/api/v1/handbook/trains", body, timeout=timeout), "car list")
        return ((((j or {}).get("data") or {}).get("train") or {}).get("carGroup")) or []

    def train_route(self, number: str) -> list:
        j = self._json(self.request("POST", "/api/v1/handbook/trains/route", {"number": number}, auth=True), "route")
        return (((j or {}).get("data") or {}).get("stopStations")) or []

    def station_schedule(self, code: str) -> dict:
        j = self._json(self.request("GET", f"/api/v1/handbook/stations/schedule/{code}", auth=True), "schedule")
        return (j or {}).get("data") or {}

    # ---------------------------------------------------------------- orders
    def reserve(self, payload: dict) -> str:
        r = self.request("POST", "/api/v3/universal-orders/reserve", payload, auth=True, timeout=20, priority=True)
        m = re.search(r'"response"\s*:\s*"(UO[\w-]+)"', r.text or "")
        if r.status_code == 200 and m:
            return m.group(1)
        raise EticketError("reserve refused: " + _message(r), r.status_code, r.text[:500])

    def snapshot(self, order_id: str) -> dict | None:
        r = self.request("POST", "/api/v3/universal-orders/get-snapshot",
                         {"orderId": order_id, "userId": self.user_id}, auth=True, timeout=10)
        if r.status_code != 200:
            return None
        try:
            return r.json().get("response")
        except (ValueError, AttributeError):
            return None

    def cancel(self, order_id: str) -> bool:
        body = {"orderId": order_id, "webCustomer": {"id": self.user_id, "username": self.email,
                                                     "ip": "eticket.uzrailpass.uz"}, "cashierOperator": None}
        r = self.request("POST", "/api/v3/universal-orders/cancel", body, auth=True, timeout=15)
        return r.status_code == 200

    def seconds_left(self, order_id: str) -> float | None:
        r = self.request("GET", f"/api/v3/universal-orders/query/get/payment-end-time/{order_id}", auth=True)
        try:
            inner = r.json()["response"]["response"]
            end = dt.datetime.fromisoformat(inner["expiryTime"][:19])
            now = dt.datetime.fromisoformat(inner["currentTime"][:19])
            return (end - now).total_seconds()
        except (ValueError, KeyError, TypeError):
            return None

    def orders(self) -> list:
        """Active (unfinished) orders on the account, newest first."""
        r = self.request("POST", "/api/v3/query/orders/list", {"page": 0, "length": 20}, auth=True)
        try:
            return (r.json() or {}).get("data") or []
        except ValueError:
            return []

    def active_orders(self) -> int:
        try:
            return int(self.request("GET", "/api/v3/query/orders/count", auth=True).json().get("count", 0))
        except (ValueError, AttributeError):
            return -1

    # ---------------------------------------------------------------- payment (Hamkorbank hosted page)
    def select_payment(self, order_id: str, kind: str = "HamkorbankWebView"):
        body = {"id": order_id, "paymentId": order_id + "_PAYMENT", "type": kind, "withLoyaltyProgram": None}
        r = self.request("POST", "/api/v1/payment/select-payment-type", body, auth=True)
        if r.status_code != 200:
            raise EticketError("could not select the payment method: " + _message(r), r.status_code, r.text)

    def hamkor_prepare(self, payment_system_id: str, success_url: str, failed_url: str):
        body = {"id": payment_system_id, "successUrl": success_url, "failedUrl": failed_url}
        r = self.request("POST", "/api/v1/hamkorbank-web-view/prepare-payment", body, auth=True)
        if r.status_code != 200:
            raise EticketError("could not prepare the bank page: " + _message(r), r.status_code, r.text)

    def hamkor_callback(self, payment_system_id: str):
        """What the website calls when the user comes back from the bank. Only call it after the user
        has been to the bank page: called before paying, the server drops the connection."""
        try:
            self.request("POST", "/api/v1/hamkorbank-web-view/callback-status", {"id": payment_system_id},
                         auth=True, timeout=10)
        except requests.RequestException:
            pass

    def ticket_pdf(self, order_id: str) -> bytes | None:
        """GET /api/v1/document/{orderId}/pdf. Accepts a raw PDF or JSON holding base64 PDF data."""
        r = self.request("GET", f"/api/v1/document/{order_id}/pdf", auth=True, timeout=30)
        if r.status_code != 200 or not r.content:
            return None
        if r.content[:5] == b"%PDF-":
            return r.content
        try:
            data = r.json()
        except ValueError:
            return None

        def find(o):
            if isinstance(o, str) and len(o) > 200:
                try:
                    raw = base64.b64decode(o.split(",")[-1])
                    if raw[:5] == b"%PDF-":
                        return raw
                except Exception:
                    return None
            if isinstance(o, dict):
                for v in o.values():
                    hit = find(v)
                    if hit:
                        return hit
            if isinstance(o, list):
                for v in o:
                    hit = find(v)
                    if hit:
                        return hit
            return None
        return find(data)


# ---------------------------------------------------------------- reserve payload

def build_reserve_payload(client: Client, train: dict, car: dict, seats_range: str, passengers: list,
                          frm_code: str, to_code: str) -> dict:
    """train: a train from trains(); car: {"number","carType","service"}; passengers: saved-passenger dicts."""
    dep_d, dep_t = split_dmy(train["departureDate"])
    arv_d, arv_t = split_dmy(train["arrivalDate"])
    sub = train.get("subRoute") or {}
    tickets = []
    for f in passengers:
        dd, mm, yy = str(f["birthDay"]).split(".")
        mid = (f.get("midname") or "").strip()
        tickets.append({
            "passengerInfo": {
                "fullName": f"{f['lastname']}={f['firstname']}={mid}",
                "documentId": f["doc"], "documentType": f["docType"],
                "gender": "MALE" if str(f.get("sex", "M")).upper().startswith("M") else "FEMALE",
                "citizenship": f.get("citizenship") or "UZB",
                "birthday": f"{yy}-{mm}-{dd}", "regionId": "03", "discountType": "REGULAR",
                "referenceDocument": None, "prefix": None, "tariffCode": None, "child": None,
            },
            "pricingMode": "FULL",
            "hasPatronymics": bool(mid),
        })
    return {
        "channel": "WEB", "orderKind": "RETAIL", "paymentMode": "CARD", "groupType": "INDIVIDUALS",
        "orderType": "INDIVIDUAL", "providerType": train.get("providerType") or "EXPRESS", "documentMode": "NAMED",
        "subItems": [{
            "trainInfo": {"trainNumber": train["number"], "carNumber": car["number"], "carType": car["carType"],
                          "serviceClass": car["service"], "directionSequence": 1, "brand": train.get("brand")},
            "departure": {"code": frm_code, "name": sub.get("depStationName"), "date": dep_d, "time": dep_t},
            "arrival": {"code": to_code, "name": sub.get("arvStationName"), "date": arv_d, "time": arv_t},
            "hasInsurance": False, "hasCateringOrder": None, "hasGreenTicket": False,
            "reserveSeatRequirements": {"seatsRange": seats_range},
            "tickets": tickets,
        }],
        "webCustomer": {"id": client.user_id, "username": client.email, "ip": "eticket.uzrailpass.uz"},
    }


HELD_STATES = ("PAYMENT_STARTED",)
FAILED_STATES = ("ORDER_FAILED",)


def settle(client: Client, order_id: str, live: "Live | None" = None, timeout: float = 12, since: float = 0):
    """Wait until a reservation is decided. Returns ('RESERVED'|'FAILED'|'TIMEOUT', order data or {}).

    Uses the websocket (PAYMENT_STARTED = held, ORDER_FAILED = failed); its message carries the whole
    order, seats included. get-snapshot is rate-limited hard, so it is only a slow fallback when the
    websocket is down."""
    end = time.time() + timeout
    since = since or (time.time() - 2)
    while time.time() < end:
        if live is not None and live.connected:
            ev = live.wait_for(lambda d: _ev_order(d) == order_id and d.get("state") in HELD_STATES + FAILED_STATES,
                               min(1.0, max(0.05, end - time.time())), since=since)
            if ev:
                return ("RESERVED" if ev["state"] in HELD_STATES else "FAILED"), (ev.get("data") or {})
            continue
        try:
            snap = client.snapshot(order_id) or {}
        except RateLimited:
            snap = {}
        items = snap.get("items") or []
        status = items[0].get("status") if items else None
        if status in ("RESERVED", "FAILED"):
            return status, snap
        time.sleep(1.0)
    return "TIMEOUT", {}


def order_details(client: Client, order_id: str) -> dict:
    """Seats, prices and train info for a held order: one snapshot call, orders-list fallback."""
    try:
        snap = client.snapshot(order_id)
        if snap and snap.get("items"):
            return snapshot_summary(snap)
    except RateLimited:
        pass
    for o in client.orders():
        if o.get("orderId") == order_id:
            it = (o.get("items") or [{}])[0]
            return {"train": (it.get("trainInfo") or {}).get("trainNumber"),
                    "car": (it.get("trainInfo") or {}).get("carNumber"),
                    "dep": it.get("departure") or {}, "arr": it.get("arrival") or {},
                    "seats": [str(t.get("seatNumber") or "").lstrip("0") for t in it.get("tickets") or []],
                    "seat_types": [], "names": [], "total": int(float(o.get("totalCost") or 0))}
    return {}


def _ev_order(d: dict) -> str:
    return str(((d.get("data") or {}) if isinstance(d.get("data"), dict) else {}).get("orderId") or "")


def snapshot_summary(snap: dict) -> dict:
    """Seats, prices and train info from a get-snapshot response."""
    items = (snap or {}).get("items") or []
    it = items[0] if items else {}
    tickets = it.get("tickets") or []
    return {
        "train": (it.get("trainInfo") or {}).get("trainNumber"),
        "car": (it.get("trainInfo") or {}).get("carNumber"),
        "dep": it.get("departure") or {},
        "arr": it.get("arrival") or {},
        "seats": [str(t.get("seatNumber") or "").lstrip("0") for t in tickets],
        "seat_types": [t.get("seatType") for t in tickets],
        "names": [str((t.get("passengerInfo") or {}).get("fullName") or "").replace("=", " ").strip() for t in tickets],
        "total": sum(int((t.get("tariff") or {}).get("amount") or 0) for t in tickets),
    }


# ---------------------------------------------------------------- live order/payment events

class Live:
    """The site's websocket: pushes order and payment states for the logged-in account."""

    def __init__(self, client: Client, on_event=None):
        self.client = client
        self.on_event = on_event
        self.cond = threading.Condition()
        self.recent = collections.deque(maxlen=300)
        self.connected = False
        self._stop = False
        self._ws = None
        self._thread = threading.Thread(target=self._run, name="eticket-live", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop = True
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def _run(self):
        backoff = 1.0
        while not self._stop:
            try:
                self.client.ensure_login()
                uid = self.client.user_id
                cookie = "; ".join(f"{c.name}={c.value}" for c in self.client.s.cookies)

                def on_open(ws):
                    nonlocal backoff
                    ws.send(json.dumps({"group": uid, "subscribe": 1}))
                    self.connected = True
                    backoff = 1.0
                    log.info("live order events connected")

                self._ws = websocket.WebSocketApp(
                    WS_URL, header=[f"Origin: {BASE}", f"User-Agent: {UA}"], cookie=cookie,
                    on_open=on_open, on_message=self._on_message,
                    on_error=lambda ws, e: log.debug("live events error: %s", e))
                self._ws.run_forever(ping_interval=25, ping_timeout=10)
            except Exception as e:
                log.warning("live events: %s", e)
            self.connected = False
            if self._stop:
                break
            time.sleep(backoff)
            backoff = min(30.0, backoff * 2)

    def _on_message(self, _ws, message):
        try:
            d = json.loads(message)
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        d["_t"] = time.time()
        with self.cond:
            self.recent.append(d)
            self.cond.notify_all()
        rec.ev("ws", status=d.get("status"), state=d.get("state"), order=_ev_order(d),
               seats=snapshot_summary(d["data"])["seats"] if isinstance(d.get("data"), dict) and d["data"].get("items") else None)
        log.debug("live event %s %s %s", d.get("status"), d.get("state"), _ev_order(d))
        if self.on_event:
            try:
                self.on_event(d)
            except Exception:
                log.exception("live event handler failed")

    def seen(self, pred, since: float = 0) -> dict | None:
        with self.cond:
            for d in self.recent:
                if d["_t"] >= since and pred(d):
                    return d
        return None

    def wait_for(self, pred, timeout: float, since: float = 0) -> dict | None:
        end = time.time() + timeout
        with self.cond:
            while True:
                for d in self.recent:
                    if d["_t"] >= since and pred(d):
                        return d
                left = end - time.time()
                if left <= 0:
                    return None
                self.cond.wait(left)


def make_pay_link(client: Client, live: Live, order_id: str, success_url: str, failed_url: str,
                  timeout: float = 15):
    """Select Hamkorbank and get its hosted payment page. Returns (paymentSystemId, payUrl)."""
    t0 = time.time() - 1
    client.select_payment(order_id, "HamkorbankWebView")
    ev = live.wait_for(lambda d: d.get("state") in ("SELECT_PAYMENT_SUCCEEDED", "SELECT_PAYMENT_FAILED")
                       and _ev_order(d) == order_id, timeout, since=t0)
    if not ev or ev.get("state") != "SELECT_PAYMENT_SUCCEEDED":
        raise EticketError("the railway did not accept the payment method")
    psid = ev["data"]["paymentSystemId"]
    client.hamkor_prepare(psid, success_url, failed_url)
    ev = live.wait_for(lambda d: d.get("state", "").startswith("PREPARE_PAYMENT") and _ev_order(d) == order_id,
                       timeout, since=t0)
    if not ev or not (ev.get("data") or {}).get("payUrl"):
        raise EticketError("the bank page was not prepared")
    return psid, ev["data"]["payUrl"]
