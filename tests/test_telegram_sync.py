"""Telegram stays in sync with the PC app (offline: Telegram and the railway are faked).
Run: python tests/test_telegram_sync.py"""
import os
import sys
import tempfile
import threading
import time

os.environ["UZRAILBOT_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from uzrailbot import gui, telegram  # noqa: E402
from uzrailbot.store import Store  # noqa: E402

calls = []


class FakeEngine:
    booking, moving_pax, friends = set(), set(), {}

    def __init__(self, store):
        self.store = store

    def passenger_list(self):
        return [{"id": "2", "name": "Test Passenger"}]

    def scan_trains(self, t, fetch=True):
        return []

    def cancel_hold(self, oid):
        self.store.state["holds"].pop(oid, None)
        return True

    def transfer_hold(self, oid, frm, to):
        self.store.state["holds"].pop(oid, None)          # released, but nobody got it back
        return False, "Someone else took the seat first."


def make():
    store = Store()
    store.cfg["telegram"]["owner_id"] = 42
    store.cfg["webapp_url"] = "https://example.org/app"
    bot = telegram.Bot(store)
    bot.api.call = lambda method, **kw: calls.append((method, kw)) or {"message_id": 7}
    bot.engine = FakeEngine(store)
    threading.Thread(target=bot._sender, daemon=True).start()
    return store, bot


def wait_calls(n):
    end = time.time() + 3
    while len(calls) < n and time.time() < end:
        time.sleep(0.02)


store, bot = make()
trip = store.upsert_trip({"from": {"code": "2900790", "name": "URGENCH"}, "to": {"code": "2900002", "name": "TASHKENT SOUTH"},
                          "dates": ["2099-01-01"], "classes": ["3П"], "passengers": ["1"], "mode": "buy"})
hold = {"order": "UO1", "trip": trip["id"], "train": "056Ж", "car": "10", "seats": ["12"], "total": 245140,
        "expires_at": time.time() + 600, "pay_url": "https://pay", "msg_id": 99, "passengers": ["1"], "names": ["A"],
        "cls": "3П", "from": "URGENCH", "to": "TASHKENT"}

# 1. a plain message carries the keyboard, so the Mini App link is refreshed with the current trips
bot.send("hello")
wait_calls(1)
m, kw = calls[-1]
assert m == "sendMessage" and kw["reply_markup"]["keyboard"][0][0]["web_app"]["url"].startswith("https://example.org/app/#s="), kw

# 2. a failed transfer started in the PC app: the old Pay message is closed and a new message says why
app = gui.App.__new__(gui.App)
app.store, app.bot, app.engine = store, bot, bot.engine
store.state["holds"]["UO1"] = dict(hold)
calls.clear()
res = app.handle("POST", "/api/transfer", {"order": "UO1", "from": "1", "to": "2"})
wait_calls(2)
assert res == {"ok": False, "error": "Someone else took the seat first."}, res
edits = [kw for m, kw in calls if m == "editMessageText"]
sends = [kw for m, kw in calls if m == "sendMessage"]
assert edits and edits[0]["message_id"] == 99 and "didn't go to" in edits[0]["text"] and not edits[0].get("reply_markup"), edits
assert sends and "Test Passenger" in sends[0]["text"], sends

# 3. a transfer that stopped before releasing the seat keeps the Pay buttons
store.state["holds"]["UO1"] = dict(hold)
calls.clear()
bot.transfer_done(hold, "Test Passenger", False, "That passenger already holds a seat.")
wait_calls(2)
edit = next(kw for m, kw in calls if m == "editMessageText")
assert edit["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://pay", edit

# 4. cancel / pause / trip toggle / delete in the PC app all show up in Telegram
calls.clear()
app.handle("POST", "/api/cancel", {"order": "UO1"})
app.handle("POST", "/api/pause", {"paused": True})
app.handle("POST", "/api/trip", {"id": trip["id"], "active": False})
app.handle("POST", "/api/trip/delete", {"id": trip["id"]})
wait_calls(6)
texts = [kw["text"] for m, kw in calls]
assert any("Cancelled in the PC app" in t for t in texts), texts
assert any("cancelled the reservation on train 056Ж" in t for t in texts), texts
assert any("Paused from the PC app" in t for t in texts), texts
assert any("Paused the trip Urgench → Tashkent South" in t for t in texts), texts
assert any("Deleted the trip Urgench → Tashkent South" in t for t in texts), texts

print("telegram sync: all checks passed")
