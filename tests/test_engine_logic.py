"""Booking logic (offline: the railway is faked). Run: python tests/test_engine_logic.py"""
import os
import sys
import tempfile
import threading
import time

os.environ["UZRAILBOT_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from uzrailbot import eticket  # noqa: E402
from uzrailbot.engine import Engine, match_train, pick_seats  # noqa: E402
from uzrailbot.store import Store  # noqa: E402

# ---- matching: price is per ticket, classes and chosen trains filter
train = {"number": "056Ж", "departureDate": "08.11.2099 19:23", "arrivalDate": "09.11.2099 08:22",
         "cars": [{"freeSeats": 3, "type": "ПЛАЦ", "tariffs": [{"classServiceType": "3П", "tariff": 245140}]},
                  {"freeSeats": 2, "type": "КУПЕ", "tariffs": [{"classServiceType": "2К", "tariff": 390000}]}]}
trip = {"trains": [], "classes": ["3П", "2К"], "maxPrice": 250000}
assert [m["cls"] for m in match_train(trip, train)] == ["3П"], "a 390 000 ticket is over a 250 000 limit"
assert match_train(dict(trip, maxPrice=0), train)[1]["free"] == 2
assert match_train(dict(trip, trains=["058З"]), train) == []

# ---- seats: the tightest block, and never more than are free
assert pick_seats(["3", "10", "11", "12", "40"], 3) == [10, 11, 12]
assert pick_seats(["5"], 2) == []

# ---- request budget: one pause for parallel 429s; repeated 429s double the pause; a normal answer resets it
b = eticket._Budget()
R = eticket.RESERVE_PATH
assert b.hit_429(R) == 30 and 29 < b.cooling(R) <= 30
assert b.hit_429(R) <= 30, "a second 429 from the same burst must not double the pause"
b.cooldown_until[R] = time.monotonic() - 1           # the pause just ended ...
assert b.hit_429(R) == 60                            # ... and the railway still says 429
b.cooldown_until[R] = time.monotonic() - 1
assert b.hit_429(R) == 120
b.ok(R)
b.cooldown_until[R] = time.monotonic() - 1
assert b.hit_429(R) == 30
try:
    b.take(R, priority=True)
    raise AssertionError("take() must refuse during a pause")
except eticket.RateLimited:
    pass

# ---- saving never crashes while another thread adds keys (it did with json.dump + indent)
store = Store()
stop = threading.Event()


def churn():                         # add and remove keys (the dict keeps changing size, but stays bounded)
    cars = store.state.setdefault("cars", {})
    cars.update({f"K|3П|{i}": {"carType": "ПЛАЦ", "service": "3П"} for i in range(2000)})   # old code: ~5% crashed
    i = 0
    while not stop.is_set():
        cars[f"X|3П|{i % 400}"] = {"carType": "ПЛАЦ"}
        cars.pop(f"X|3П|{(i + 200) % 400}", None)
        i += 1


t = threading.Thread(target=churn)
t.start()
try:
    for _ in range(100):
        store.save()
finally:
    stop.set()
    t.join()
store.state["cars"] = {}
from uzrailbot import paths  # noqa: E402
assert "\n" not in paths.state_path().read_text(encoding="utf-8"), "state must be encoded in one go (no indent)"


# ---- a seat the list shows but the railway won't give: one reserve, then wait for the list to change
class FakeLive:
    connected = True

    def stop(self):
        pass

    def seen(self, pred, since=0):
        ev = {"state": "ORDER_FAILED", "data": {"orderId": FakeClient.last}, "_t": time.time()}
        return ev if pred(ev) else None


class FakeClient:
    last = ""

    def __init__(self):
        self.budget, self.user_id, self.email, self.calls, self.reserves = eticket._Budget(), "u", "e", 0, 0

    def cars(self, date, frm, to, number):
        return [{"type": "ПЛАЦ", "tariff": 245140,
                 "cars": [{"number": "12", "schema": {"service": "3П"}, "places": ["25"]}]}]

    def reserve(self, payload):
        self.budget.take(R, priority=True)
        self.reserves += 1
        FakeClient.last = f"UO{self.reserves}"
        return FakeClient.last


class Notes:
    def __init__(self):
        self.seen = []

    def __getattr__(self, name):
        return lambda *a, **k: self.seen.append(name)


notes = Notes()
eng = Engine(store, notes)
eng.client, eng.live = FakeClient(), FakeLive()
t = store.upsert_trip({"from": {"code": "2900790", "name": "URGENCH"}, "to": {"code": "2900002", "name": "TASHKENT"},
                       "dates": ["2099-11-08"], "classes": ["3П"], "passengers": ["1"], "mode": "buy"})
person = {"friendId": 1, "firstname": "A", "lastname": "B", "birthDay": "01.01.1990", "doc": "X", "docType": "PASSPORT"}
hit = {"number": "056Ж", "cls": "3П", "price": 245140, "free": 1, "train": train}

got = eng._book_hit(t, "2099-11-08", hit, [person], fresh=False)
assert got == set() and eng.client.reserves == 1, f"expected 1 reserve, got {eng.client.reserves}"
assert eng._effective_free("2099-11-08", hit) == 0, "the next check must not book the same failed seat again"
assert eng._effective_free("2099-11-08", dict(hit, free=2)) == 2, "a change in the list books again"
assert "missed" in notes.seen

# ---- rate-limited: no false "missed", one alert, no reserve sent
notes.seen.clear()
eng.stuck.clear()
eng.bad_seats.clear()
eng.client.budget.hit_429(R)
eng.client.reserves = 0
eng._book_hit(t, "2099-11-08", hit, [person], fresh=False)
assert eng.client.reserves == 0 and "missed" not in notes.seen and notes.seen.count("problem") == 1, notes.seen

# ---- two trips with the same passenger never book them at the same time
eng.booking_pax.add("1")
assert eng._remaining(t) == [], "a passenger being booked by another trip must be skipped"
eng.booking_pax.clear()
assert eng._remaining(t) == ["1"]

eng.stop()
print("engine logic: all checks passed")


# ---- transfer: the new passenger takes the giver's place in the trip, but only once the old seat is released
import types  # noqa: E402
from uzrailbot import engine as engine_mod  # noqa: E402
engine_mod.time = types.SimpleNamespace(time=time.time, monotonic=time.monotonic, perf_counter=time.perf_counter,
                                        sleep=lambda s: None)   # no waiting


class XClient(FakeClient):
    def __init__(self, list_ok=True):
        super().__init__()
        self.list_ok, self.issued = list_ok, []

    def trains(self, *a):
        if not self.list_ok:
            raise eticket.EticketError("offline")
        return [train]

    def cancel(self, oid):
        return True

    def reserve(self, payload):
        self.issued.append(super().reserve(payload))
        return self.issued[-1]

    def cars(self, *a):
        return []


class XLive:                         # the railway refuses every re-booking (someone else got the seat)
    connected = True

    def __init__(self, client):
        self.client = client

    def seen(self, pred, since=0):
        for oid in ["UOX"] + self.client.issued:
            ev = {"state": "ORDER_FAILED", "data": {"orderId": oid}, "_t": time.time()}
            if pred(ev):
                return ev
        return None

    def wait_for(self, pred, timeout, since=0):
        return self.seen(pred, since)

    def stop(self):
        pass


def trip_after_transfer(passengers, frm, to, list_ok=True):
    e = Engine(store, Notes())
    tr = store.upsert_trip({"from": {"code": "2900790", "name": "URGENCH"}, "to": {"code": "2900002", "name": "TASHKENT"},
                            "dates": ["2099-11-08"], "classes": ["3П"], "passengers": passengers, "mode": "buy"})
    store.state["holds"]["UOX"] = {"order": "UOX", "trip": tr["id"], "date": "2099-11-08", "train": "056Ж", "cls": "3П",
                                   "car": "10", "car_info": {"carType": "ПЛАЦ", "service": "3П"}, "seats": ["12"],
                                   "passengers": [frm], "total": 245140}
    e.client = XClient(list_ok)
    e.live = XLive(e.client)
    e.friends = {p: dict(person, friendId=p) for p in ("A", "B", "C", "D")}
    e.passenger_list = lambda: [{"id": p, "name": p} for p in ("A", "B", "C", "D")]
    ok, _ = e.transfer_hold("UOX", frm, to)
    still_held = "UOX" in store.state["holds"]
    store.state["holds"].pop("UOX", None)
    e.stop()
    return store.trip(tr["id"])["passengers"], still_held


assert trip_after_transfer(["A"], "A", "B") == (["B"], False)
assert trip_after_transfer(["A", "C"], "A", "B") == (["B", "C"], False)
assert trip_after_transfer(["C", "A"], "A", "B") == (["C", "B"], False), "the new passenger keeps the giver's priority"
assert trip_after_transfer(["A"], "A", "B", list_ok=False) == (["A"], True), "stopped before the release: no trip edit"
print("transfer trip edit: all checks passed")
