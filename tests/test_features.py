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
assert state["v"] >= 2 and state["holds"][0]["pay"] == hold["pay_url"] and state["holds"][0]["t"] == "056Ж", state
assert state["holds"][0]["trip"] == trip["id"] and state["holds"][0]["p"] == [ID_A], "the Mini App needs who holds what"
store.mark_done(trip["id"], [ID_B])
assert ID_B in json.loads(base64.urlsafe_b64decode((f := bot.miniapp_url().split("#s=")[1]) + "=" * (-len(f) % 4)))["trips"][0]["done"]
store.delete_trip(trip["id"])
assert store.done_passengers(trip["id"]) == [], "paid highlights end with the watch"
trip = store.upsert_trip(dict(trip))
hold["trip"] = trip["id"]

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

# ---- the Completed tab's Get tickets: sends the PDF of each paid order again; tickets end with the watch
calls.clear()
bot.engine.client = types.SimpleNamespace(ticket_pdf=lambda order: b"%PDF-1" if order == "UO1" else None)
store.add_ticket({"trip": trip["id"], "order": "UO1", "train": "056Ж", "car": "10", "seats": ["12"]})
store.add_ticket({"trip": trip["id"], "order": "UO2", "train": "056Ж", "car": "11", "seats": ["3"]})
bot.send_tickets(trip["id"])
wait(2)
assert len(by("sendDocument")) == 1 and "couldn't download" in by("sendMessage")[0]["text"], calls
store.delete_trip(trip["id"])
assert store.tickets(trip["id"]) == []
trip = store.upsert_trip(dict(trip))

# ---- pay links: the card always offers a new link; asking for one updates the card in place
from uzrailbot import eticket  # noqa: E402
kb = bot._hold_kb(hold)["inline_keyboard"]
assert kb[0][0]["url"] == hold["pay_url"] and kb[-1][0]["callback_data"] == "paylink:" + hold["order"], kb
assert bot._hold_kb(dict(hold, pay_url=""))["inline_keyboard"][0][0]["callback_data"] == "paylink:" + hold["order"]
assert "bank closed its payment page" in bot._pay_text(dict(hold, pay_url="", link_dead=True)) and "bank closed" not in bot._pay_text(hold)
calls.clear()
store.state["holds"][hold["order"]] = dict(hold, pay_url="")
bot.engine.new_pay_link = lambda oid: store.state["holds"][oid].update(pay_url="https://pay.example/new") or store.state["holds"][oid]
bot._webapp({"web_app_data": {"data": json.dumps({"a": "paylink", "order": hold["order"]})}})
wait(2)
edit = by("editMessageText")[0]
assert edit["message_id"] == 99 and edit["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://pay.example/new", edit
assert "New payment link ready" in by("sendMessage")[0]["text"]
calls.clear()
bot.engine.new_pay_link = lambda oid: None
bot.new_link(hold["order"])
wait(1)
assert "couldn't get a new payment link" in by("sendMessage")[0]["text"]

# ---- the bank check: only a clear "gone" from the bank counts; no answer never takes the Pay button away
BANK_LINK = "https://acq-invoice.hamkorbank.uz/?key=aaaaaaaa-1111-1111-1111-111111111111&inv_type=4"
real_get = eticket.requests.get
answer = [{"status": "Failure", "error_code": -30, "error_note": "1056 Url is expired", "data": None}]
eticket.requests.get = lambda url, timeout=0: types.SimpleNamespace(json=lambda: answer[0])
assert eticket.pay_link_dead(BANK_LINK) is True
answer[0] = {"status": "Failure", "error_code": -30, "error_note": "sql: no rows in result set", "data": None}
assert eticket.pay_link_dead(BANK_LINK) is True
answer[0] = {"status": "Success", "data": {"amount": 1}}
assert eticket.pay_link_dead(BANK_LINK) is False
answer[0] = {"status": "Failure", "error_note": "something new"}
assert eticket.pay_link_dead(BANK_LINK) is False, "an answer we don't know is not proof the page is gone"
assert eticket.pay_link_dead("https://pay.example/x") is False, "another kind of link is never called dead"


def no_bank(url, timeout=0):
    raise eticket.requests.ConnectionError("down")


eticket.requests.get = no_bank
assert eticket.pay_link_dead(BANK_LINK) is False
eticket.requests.get = real_get

# ---- the link is written the way browsers write it ("/" before "?")
events = [{"state": "SELECT_PAYMENT_SUCCEEDED", "data": {"orderId": "UO9", "paymentSystemId": "P"}},
          {"state": "PREPARE_PAYMENT_SUCCEEDED", "data": {"orderId": "UO9", "payUrl": "https://acq-invoice.hamkorbank.uz?key=k&inv_type=4"}}]
fake_client = types.SimpleNamespace(select_payment=lambda oid, kind: None, hamkor_prepare=lambda psid, ok, bad: None)
fake_live = types.SimpleNamespace(wait_for=lambda pred, timeout, since=0: next((e for e in events if pred(e)), None))
assert eticket.make_pay_link(fake_client, fake_live, "UO9", "ok", "bad") == ("P", "https://acq-invoice.hamkorbank.uz/?key=k&inv_type=4")

# ---- live link: encrypted both ways; only fresh, known commands from someone with the key run
from uzrailbot import remote  # noqa: E402
assert {"paylink", "paytap"} <= remote.COMMANDS
live = remote.Remote(store, bot)
assert remote.unseal(live.key, remote.seal(live.key, {"a": 1})) == {"a": 1}
ran = []
bot._webapp, real_webapp = (lambda m: ran.append(json.loads(m["web_app_data"]["data"]))), bot._webapp
cmd = lambda **k: remote.seal(live.key, {"ts": time.time(), "n": os.urandom(4).hex(), **k})
assert live.command(cmd(a="pause", on=True)) and ran == [{"a": "pause", "on": True}]
same = cmd(a="pause", on=False)
assert live.command(same) and not live.command(same), "the same command must not run twice"
assert not live.command(cmd(a="account", email="x", pw="y")), "the password never goes over the live link"
assert not live.command(remote.seal(os.urandom(32), {"a": "pause", "ts": time.time(), "n": "z"})), "wrong key"
assert not live.command(remote.seal(live.key, {"a": "pause", "ts": time.time() - 3600, "n": "old"})), "too old"
assert len(ran) == 2
bot._webapp = real_webapp
link = live.link()
assert len(link["i"]) == 32 and "token" not in link, "the link never carries the write token"
assert remote.Remote(store, bot).id == live.id, "the same PC keeps the same link"

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
