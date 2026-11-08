"""Telegram booking card, transfer buttons, Mini App data, report a problem, sleep blocking, desktop notification
(offline: Telegram and the upload are faked). Run: python tests/test_features.py"""
import base64
import json
import os
import sys
import tempfile
import threading
import time
import types

os.environ["UZRAILBOT_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from uzrailbot import gui, paths, power, telegram, telemetry  # noqa: E402
from uzrailbot.recorder import rec  # noqa: E402
from uzrailbot.store import Store  # noqa: E402

calls = []
ID_A, ID_B = "aaaaaaaa-1111-1111-1111-111111111111", "bbbbbbbb-2222-2222-2222-222222222222"


class FakeEngine:
    booking, moving_pax, friends = set(), set(), {}

    def passenger_list(self):
        return [{"id": ID_A, "name": "Test One"}, {"id": ID_B, "name": "Test Two"}]

    def scan_trains(self, t, fetch=True):
        return []

    def transfer_targets(self, oid):
        return [{"id": ID_B, "name": "Test Two", "pos": None}]


def fake_call(method, **kw):
    calls.append((method, kw))
    return {"message_id": 100 + len(calls)}


def wait(n):
    end = time.time() + 3
    while len(calls) < n and time.time() < end:
        time.sleep(0.02)


def by(method):
    return [kw for m, kw in calls if m == method]


store = Store()
store.cfg["telegram"]["owner_id"] = 42
store.cfg["webapp_url"] = "https://example.org/app"
bot = telegram.Bot(store)
bot.api.call = fake_call
bot.engine = FakeEngine()
threading.Thread(target=bot._sender, daemon=True).start()

trip = store.upsert_trip({"from": {"code": "2900790", "name": "URGENCH"}, "to": {"code": "2900002", "name": "TASHKENT"},
                          "dates": ["2099-01-01"], "classes": ["3П"], "passengers": [ID_A], "mode": "buy"})
hold = {"order": "UO781FQE195RDN", "trip": trip["id"], "date": "2099-01-01", "train": "056Ж", "car": "10", "cls": "3П",
        "seats": ["12"], "seat_types": ["В"], "total": 245140, "expires_at": time.time() + 600, "pay_url": "https://pay.example/x",
        "msg_id": 99, "passengers": [ID_A], "names": ["Test One"], "from": "URGENCH", "to": "TASHKENT", "took": 1.5}

# ---- a catch: the note goes away, one card has everything, a silent line refreshes the Mini App
hit = {"number": "056Ж", "cls": "3П", "free": 1, "price": 245140, "train": {"departureDate": "01.01.2099 19:23", "arrivalDate": "02.01.2099 08:22"}}
bot.seats_found(trip, "2099-01-01", [hit])
wait(1)
note_id = 100 + len(calls)
assert "reply_markup" not in calls[0][1] or calls[0][1]["reply_markup"] is None, "the note must not carry the reply keyboard"
store.state["holds"][hold["order"]] = dict(hold)
bot.reserved(store.state["holds"][hold["order"]])
wait(4)
assert [kw["message_id"] for kw in by("deleteMessage")] == [note_id], by("deleteMessage")
card, quiet = by("sendMessage")[1], by("sendMessage")[2]
assert "Seats reserved" in card["text"] and "left to pay" in card["text"] and card["reply_markup"]["inline_keyboard"][0][0]["url"]
assert quiet["disable_notification"] is True and quiet["reply_markup"]["keyboard"], "the silent line carries the keyboard"
fragment = quiet["reply_markup"]["keyboard"][0][0]["web_app"]["url"].split("#s=")[1]
state = json.loads(base64.urlsafe_b64decode(fragment + "=" * (-len(fragment) % 4)))
assert state["v"] == 2 and state["holds"][0]["pay"] == hold["pay_url"] and state["holds"][0]["t"] == "056Ж", state

# ---- a miss removes the note too
calls.clear()
bot.seats_found(trip, "2099-01-01", [hit])
wait(1)
bot.missed(trip, "2099-01-01", hit, "someone else got the seats first")
wait(3)
assert len(by("deleteMessage")) == 1 and "Not this time" in by("sendMessage")[1]["text"]

# ---- a message is never lost because Telegram refuses the keyboard
calls.clear()
original = bot.api.call


def refuse_keyboard(method, **kw):
    if kw.get("reply_markup"):
        raise telegram.TgError("Bad Request: too long", 400)
    return fake_call(method, **kw)


bot.api.call = refuse_keyboard
bot.send("seat held!")
wait(1)
bot.api.call = original
assert by("sendMessage")[0]["text"] == "seat held!", calls

# ---- transfer buttons fit Telegram's 64 bytes, and the short ids resolve to the right people
calls.clear()
bot._transfer_cb("xfer", hold["order"], 99)
wait(1)
data = by("editMessageText")[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
assert len(data.encode()) <= 64, data
calls.clear()
bot._transfer_cb("xto", data.split(":", 1)[1], 99)
wait(1)
go = by("editMessageText")[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
assert len(go.encode()) <= 64 and "Test One" in by("editMessageText")[0]["text"] and "Test Two" in by("editMessageText")[0]["text"], go

# ---- several Mini App changes in one save
calls.clear()
other = store.upsert_trip({"from": {"code": "2900790", "name": "A"}, "to": {"code": "2900000", "name": "B"},
                           "dates": ["2099-01-02"], "classes": [], "passengers": [], "mode": "notify"})
msg = {"web_app_data": {"data": json.dumps({"a": "batch", "ops": [{"a": "toggle", "id": trip["id"], "active": False}, {"a": "del", "id": other["id"]}]})}}
bot._webapp(msg)
wait(1)
assert store.trip(trip["id"])["active"] is False and store.trip(other["id"]) is None and "2 changes" in by("sendMessage")[0]["text"]

# ---- report a problem: works with sharing off, keeps the normal upload at close
rec.open()
posted = []


class R:
    status_code = 204


telemetry.requests.post = lambda url, data, timeout, headers: posted.append(headers["X-Session"]) or R()
store.cfg["share_logs"] = False
assert telemetry.report(store, "the seat was free") is True and posted == [rec.folder.name]
assert not (rec.folder / ".sent").exists(), "a report must not stop the full log at close"
assert "user_report" in (paths.app_dir() / "logs" / "sessions" / rec.folder.name / "events.jsonl").read_text(encoding="utf-8")
store.cfg["share_logs"] = True

# ---- sleep blocking: ties itself to our process on Linux and macOS; off stops it
started = []


class Proc:
    terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


proc = Proc()
power.subprocess = types.SimpleNamespace(Popen=lambda cmd, **k: started.append(cmd) or proc, DEVNULL=None)
power.os = types.SimpleNamespace(name="posix", getpid=lambda: 321)
power.sys = types.SimpleNamespace(platform="linux")
power.keep_awake(True)
power.keep_awake(True)
assert len(started) == 1 and started[0][0] == "systemd-inhibit" and "--pid=321" in started[0], started
power.keep_awake(False)
assert proc.terminated
power.sys = types.SimpleNamespace(platform="darwin")
power.keep_awake(True)
assert started[1][:2] == ["caffeinate", "-i"] and started[1][-1] == "321"

# ---- desktop notification when a seat is held (Windows path; the others shell out)
if os.name == "nt":
    app = gui.App.__new__(gui.App)
    shown = []
    app.tray = types.SimpleNamespace(HAS_NOTIFICATION=True, notify=lambda text, title: shown.append((title, text)))
    app._notify({"train": "056Ж", "car": "10", "seats": ["12"]})
    assert shown and "056Ж" in shown[0][1] and "12" in shown[0][1], shown

print("features: all checks passed")
