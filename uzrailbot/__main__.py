"""python -m uzrailbot                 desktop app (status window + tray); --background starts it hidden
python -m uzrailbot run             console mode for servers without a desktop (setup happens in the terminal)
python -m uzrailbot setup | report [N] | live | where | watch ..."""
from __future__ import annotations

import getpass
import json
import logging
import logging.handlers
import sys
import time

from . import __version__, eticket, paths
from .recorder import rec
from .store import Store
from .telegram import TgApi, TgError


def setup_logging(console: bool = True):
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(paths.log_path(), maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console and sys.stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        root.addHandler(sh)
    for noisy in ("websocket", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------- setup (bot token + owner id)

def console_setup(store: Store) -> bool:
    print(f"\nUzRail Ticket Bot {__version__} - setup\n")
    print("1) Create a bot: open Telegram, talk to @BotFather, send /newbot, and copy the token it gives you.")
    while True:
        token = input("   Paste the bot token: ").strip()
        try:
            me = TgApi(token).call("getMe")
            break
        except (TgError, Exception) as e:
            print(f"   That token didn't work ({e}). Try again.")
    api = TgApi(token)
    print(f"   OK: your bot is @{me['username']}")
    try:
        api.call("deleteWebhook", drop_pending_updates=True)
    except TgError:
        pass
    print(f"\n2) Open https://t.me/{me['username']} on your phone and press START (or send any message).")
    print("   Waiting for you...")
    offset = 0
    owner = None
    while owner is None:
        try:
            ups = api.call("getUpdates", http_timeout=35, timeout=25, offset=offset)
        except Exception as e:
            print("   (network problem, retrying)", e)
            time.sleep(3)
            continue
        for u in ups:
            offset = u["update_id"] + 1
            m = u.get("message")
            if m and m.get("from"):
                owner = m["from"]
    name = " ".join(x for x in (owner.get("first_name"), owner.get("last_name")) if x)
    print(f"   Got a message from {name} (Telegram id {owner['id']}).")
    if input("   Is this you? [Y/n] ").strip().lower() in ("n", "no"):
        print("   Setup cancelled; run it again.")
        return False
    api.call("getUpdates", offset=offset, timeout=0)          # confirm what we read
    store.set_telegram(token, owner["id"], name, me["username"])
    api.call("sendMessage", chat_id=owner["id"], text="✅ This bot is now linked to your PC. Send /account to "
                                                       "connect your railway site login, then ➕ New trip.")
    print("\n3) Railway site login (eticket.uzrailpass.uz). You can also do this later in Telegram with /account.")
    email = input("   Email (Enter to skip): ").strip()
    if email:
        pw = getpass.getpass("   Password (hidden): ")
        try:
            c = eticket.Client(email, pw)
            c.login()
            n = len(c.friends())
            store.set_railway_login(email, pw)
            print(f"   OK, logged in. Saved passengers on the site: {n}")
        except eticket.EticketError as e:
            print(f"   Login failed ({e}). Use /account in Telegram later.")
    print("\nSetup done. Starting the bot...\n")
    return True


# ---------------------------------------------------------------- run

def run():
    from .engine import Engine
    from .telegram import Bot

    setup_logging()
    store = Store()
    if not store.is_configured():
        if not (sys.stdin and sys.stdin.isatty()):
            logging.error("Not set up yet. Run 'uzrailbot setup' in a terminal first.")
            time.sleep(60)            # a service manager would otherwise restart us in a tight loop
            sys.exit(1)
        if not console_setup(store):
            return
    folder = rec.open()
    rec.ev("start", version=__version__)
    logging.info("UzRail Ticket Bot %s - recording this session to %s", __version__, folder)
    bot = Bot(store)
    engine = Engine(store, bot)
    bot.start(engine)
    engine.start()
    bot.send("🟢 Bot started on your PC. " + ("Watching your trips." if store.trips() else "Tap ➕ New trip."),
             bot.main_kb())
    last = None
    try:
        while True:
            time.sleep(1)
            s = engine.status
            line = (s["state"], s["detail"])
            if line != last:
                logging.info("status: %s %s", *line)
                last = line
    except KeyboardInterrupt:
        logging.info("stopping")
        rec.ev("stop")
        engine.stop()
        bot.stop()


# ---------------------------------------------------------------- console test mode (no Telegram)

class ConsoleNotifier:
    """Notifier for `watch`: logs every event, beeps, and opens the Pay page in the browser."""

    def __init__(self, open_pay: bool = True):
        self.open_pay = open_pay
        self.log = logging.getLogger("uzrailbot.console")

    def _beep(self, times=6):
        try:
            import winsound
            for _ in range(times):
                winsound.Beep(1800, 250)
                winsound.Beep(1200, 250)
        except Exception:
            print("\a")

    def reserved(self, h):
        self.log.warning("RESERVED order %s train %s car %s seats %s for %s, total %s, %ds to pay. PAY LINK: %s",
                         h["order"], h["train"], h["car"], h["seats"], h["names"], h["total"],
                         int(h["expires_at"] - time.time()), h.get("pay_url"))
        rec.ev("notify", what="reserved", order=h["order"], pay_url=h.get("pay_url"))
        if self.open_pay and h.get("pay_url"):
            import webbrowser
            webbrowser.open(h["pay_url"])
        self._beep()

    def renewed(self, old, new):
        self.log.warning("RENEWED %s -> %s seats %s. NEW PAY LINK: %s", old["order"], new["order"], new["seats"],
                         new.get("pay_url"))
        rec.ev("notify", what="renewed", old=old["order"], new=new["order"])
        if self.open_pay and new.get("pay_url"):
            import webbrowser
            webbrowser.open(new["pay_url"])
        self._beep(3)

    def hold_update(self, h):
        self.log.info("hold %s: %d s left", h["order"], int(h["expires_at"] - time.time()))

    def expired(self, h):
        self.log.warning("EXPIRED unpaid: %s", h["order"])

    def released(self, h, reason):
        self.log.warning("RELEASED %s: %s", h["order"], reason)

    def payment_failed(self, h):
        self.log.warning("PAYMENT FAILED at the bank for %s", h["order"])
        self._beep(2)

    def paid(self, h, pdf):
        from . import paths as _p
        path = None
        if pdf:
            path = _p.tickets_dir() / f"ticket-{h['order']}.pdf"
            path.write_bytes(pdf)
        self.log.warning("PAID %s. Ticket PDF: %s", h["order"], path or "not downloaded")
        self._beep(2)

    def missed(self, trip, date, hit, why):
        self.log.warning("MISSED %s train %s %s: %s", date, hit["number"], hit["cls"], why)

    def seats_found(self, trip, date, hits):
        self.log.warning("SEATS FOUND %s: %s", date, [(h["number"], h["cls"], h["free"], h["price"]) for h in hits])

    def problem(self, text):
        self.log.warning("PROBLEM: %s", text)

    def info(self, text):
        self.log.warning("INFO: %s", text)


def watch(args):
    """python -m uzrailbot watch FROM_CODE TO_CODE DATE[,DATE] CLASS[,CLASS] MAX_PRICE "Passenger Name"[,"Name"]
    Railway login comes from saved settings, or env UZRAIL_EMAIL / UZRAIL_PASSWORD."""
    import os
    from .engine import Engine

    setup_logging()
    frm, to, dates, classes, max_price, names = args[:6]
    store = Store()
    if os.environ.get("UZRAIL_EMAIL"):
        store.set_railway_login(os.environ["UZRAIL_EMAIL"], os.environ.get("UZRAIL_PASSWORD", ""))
    folder = rec.open()
    if os.name == "nt":       # keep the PC awake while watching
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    engine = Engine(store, ConsoleNotifier(), headless=True)
    if not engine._ensure_client():
        logging.error("railway login failed: %s", engine.status["detail"])
        return
    wanted = [n.strip().lower() for n in names.split(",")]
    pax = [p["id"] for p in engine.passenger_list() if p["name"].lower() in wanted
           or " ".join(reversed(p["name"].split())).lower() in wanted]
    if len(pax) != len(wanted):
        logging.error("passengers not found among saved passengers: %s (have %s)", names,
                      [p["name"] for p in engine.passenger_list()])
        return
    for t in store.trips():
        store.delete_trip(t["id"])
    trip = store.upsert_trip({"id": "watch", "from": {"code": frm, "name": frm}, "to": {"code": to, "name": to},
                              "dates": dates.split(","), "classes": classes.split(","), "maxPrice": int(max_price),
                              "mode": "buy", "passengers": pax, "speed": 1})
    logging.info("WATCH START %s -> %s on %s, classes %s, max %s, passengers %s; active orders now: %s; "
                 "recording to %s", frm, to, trip["dates"], trip["classes"], trip["maxPrice"], names,
                 engine.client.active_orders(), folder)
    rec.ev("watch_start", trip=trip, passengers=len(pax))
    engine.start()
    last, beat = None, time.time()
    try:
        while True:
            time.sleep(1)
            s = engine.status
            line = (s["state"], s["detail"])
            if line != last:
                logging.info("status: %s %s", *line)
                last = line
            if time.time() - beat >= 600:
                beat = time.time()
                logging.info("heartbeat: %d checks, last %.1f s ago, live events %s, requests %d, holds %d",
                             s["checks"], time.time() - (s["last_check"] or time.time()), s["live"],
                             engine.client.calls if engine.client else 0, len(store.state["holds"]))
                rec.ev("heartbeat", checks=s["checks"], live=s["live"], state=s["state"])
    except KeyboardInterrupt:
        rec.ev("stop")
        engine.stop()


# ---------------------------------------------------------------- report

EXPLAIN = {
    "missed": "seats were taken by someone else before our hold settled",
    "seats_gone": "the car lookup showed no matching seats any more (taken, or the lookup's 3-5 s cache lagged)",
    "reserve_refused": "the railway refused the reserve request (see message)",
}


def report(n: int = 1):
    root = paths.app_dir() / "logs" / "sessions"
    sessions = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
    if not sessions:
        print("No recorded sessions yet.")
        return
    folder = sessions[-n]
    rows = [json.loads(line) for line in open(folder / "events.jsonl", encoding="utf-8") if line.strip()]
    http = [r for r in rows if r["kind"] == "http"]
    checks = [r for r in http if r["path"].endswith("trains/list")]
    print(f"Session {folder.name}: {len(rows)} events, {len(http)} requests")
    if checks:
        ms = sorted(r["ms"] for r in checks)
        print(f"Train-list checks: {len(checks)}, median {ms[len(ms) // 2]} ms, slowest {ms[-1]} ms")
    codes = {}
    for r in http:
        codes[r["status"]] = codes.get(r["status"], 0) + 1
    print("HTTP status counts:", codes)
    fails = [r for r in rows if r["kind"] == "http_fail"]
    if fails:
        print(f"Connection failures: {len(fails)} (resent automatically: {sum(1 for r in fails if r.get('retry'))})")
    print("\nTimeline (availability changes, detections, bookings, holds, payments, errors):")
    keep = {"availability", "detect", "fanout", "plan", "cars_retry", "reserve_sent", "reserve_refused", "settled",
            "fanout_extra_cancelled", "seats_gone", "missed", "held", "pay_link", "pay_link_fail", "hold_end",
            "renew_attempt", "paid", "pdf_fail", "book_error", "state", "trip_saved", "user_cancel_hold",
            "returned_from_bank", "recover_cancel", "recover_hold", "check_fail"}
    t_detect = None
    for r in rows:
        if r["kind"] == "ws" and r.get("state") in ("PAYMENT_STARTED", "ORDER_FAILED", "PAYMENT_SUCCESS",
                                                    "ORDER_COMPLETED_SUCCESSFULLY", "PAYMENT_FAIL"):
            pass
        elif r["kind"] not in keep:
            continue
        if r["kind"] == "detect":
            t_detect = r["ts"]
        rel = f"+{(r['ts'] - t_detect) * 1000:6.0f} ms" if t_detect and r["kind"] != "availability" else " " * 10
        extra = {k: v for k, v in r.items() if k not in ("t", "ts", "kind")}
        why = EXPLAIN.get(r["kind"], "")
        print(f"{r['t']} {rel}  {r['kind']:<22} {json.dumps(extra, ensure_ascii=False)[:180]}" + (f"\n{'':36}-> {why}" if why else ""))


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # Cyrillic train numbers, emoji
        except (AttributeError, ValueError):
            pass
    args = sys.argv[1:]
    cmd = args[0] if args else "app"
    if cmd in ("app", "--background"):
        from .gui import App
        setup_logging(console=cmd == "app")
        App(background=cmd == "--background").run()
    elif cmd in ("run", "--headless", "headless"):
        run()
    elif cmd == "autostart":
        from . import autostart
        if len(args) > 1:
            autostart.set_enabled(args[1] in ("on", "1", "yes"))
        print("autostart:", "on" if autostart.is_enabled() else "off", "-", autostart.command())
    elif cmd == "setup":
        setup_logging(console=False)
        console_setup(Store())
    elif cmd == "report":
        report(int(args[1]) if len(args) > 1 else 1)
    elif cmd == "where":
        print(paths.app_dir())
    elif cmd == "watch":
        watch(args[1:])
    elif cmd == "live":
        from .live import main as live_main
        live_main()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
