"""Desktop app: runs the bot in the background, shows a small status window (pywebview, falls back to the
browser) and a tray icon. The window talks to a local HTTP API on 127.0.0.1 guarded by a random key."""
from __future__ import annotations

import collections
import json
import logging
import os
import secrets
import socket
import sys
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from . import __version__, autostart, paths
from .recorder import rec
from .store import Store
from .telegram import TgApi, TgError
from .updater import Updater

log = logging.getLogger("uzrailbot.gui")
UI_DIR = Path(__file__).parent / "ui"
PORT = int(os.environ.get("UZRAILBOT_PORT") or 47613)   # fixed, so a second copy can find the first one
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}
QUIET_KINDS = {"http", "check", "ws"}


class _Server(ThreadingHTTPServer):
    """Exclusive port: also the single-instance lock. (Windows lets SO_REUSEADDR steal a bound port.)"""
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class App:
    def __init__(self, background: bool = False):
        self.background = background
        self.store = Store()
        self.engine = None
        self.bot = None
        self.key = secrets.token_urlsafe(24)
        self.activity = collections.deque(maxlen=400)
        self.checks = collections.deque(maxlen=2000)        # (ts, ms) of recent checks
        self.pending = {}                                   # setup in progress: token, bot, owner
        self.window = None
        self.tray = None
        self.url = ""
        self.server = None
        self.ui_files = {}                                   # served from memory: an update can't mix versions
        self.updater = Updater(self.store, self._idle, self._release_for_restart)
        rec.listeners.append(self._on_event)

    # ---------------------------------------------------------------- services
    def _on_event(self, row):
        if row["kind"] == "check":
            self.checks.append((row["ts"], row.get("ms", 0)))
        elif row["kind"] not in QUIET_KINDS:
            self.activity.append(row)

    def start_services(self):
        from .engine import Engine
        from .telegram import Bot
        if self.engine or not self.store.is_configured():
            return
        self.bot = Bot(self.store)
        self.engine = Engine(self.store, self.bot)
        self.bot.start(self.engine)
        self.engine.start()
        self.engine.wait_account()               # so the keyboard's Mini App link already lists the passengers
        was = os.environ.get("UZRAILBOT_JUST_UPDATED")
        self.bot.send(f"🔄 Updated to v{__version__} (was v{was}). Watching again." if was else
                      f"🟢 UzRail Bot v{__version__} started on your PC.", self.bot.main_kb())
        log.info("services started")

    def _idle(self) -> bool:
        """Safe to restart: no booking or transfer in flight, no seats held."""
        e = self.engine
        return not self.store.state.get("holds") and (e is None or (not e.booking and not e.moving_pax))

    def _release_for_restart(self):
        self.stop_services()
        if self.server:
            self.server.shutdown()
            self.server.server_close()                       # free the port for the new version
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass

    def stop_services(self):
        if self.engine:
            self.engine.stop()
        if self.bot:
            self.bot.stop()

    def state(self) -> dict:
        now = time.time()
        recent = [ms for ts, ms in self.checks if now - ts < 60]
        s = self.engine.snapshot() if self.engine else {"state": "setup" if not self.store.is_configured() else "starting",
                                                         "detail": "", "last_check": 0, "live": False, "holds": [],
                                                         "trips": self.store.trips(), "board": [], "stats": {}}
        holds = []
        for h in self.store.state.get("holds", {}).values():
            holds.append({k: h.get(k) for k in ("order", "trip", "passengers", "train", "car", "seats", "seat_types",
                                                "names", "total", "from", "to", "date", "dep", "arr", "pay_url", "cls")}
                         | {"left": max(0, int(h["expires_at"] - now))})
        email, _ = self.store.railway_login()
        tg = self.store.cfg["telegram"]
        return {
            "version": __version__, "configured": self.store.is_configured(),
            "paused": bool(self.store.cfg.get("paused")), "state": s["state"], "detail": s["detail"],
            "lastCheckAge": round(now - s["last_check"], 1) if s.get("last_check") else None,
            "checksPerMin": len(recent), "medianMs": sorted(recent)[len(recent) // 2] if recent else None,
            "live": bool(s.get("live")), "account": email, "bot": tg.get("bot_username", ""),
            "owner": tg.get("owner_name", ""), "holds": holds, "trips": self.store.trips(),
            "autostart": autostart.is_enabled(), "dataDir": str(paths.app_dir()),
            "telegramOk": bool(self.bot.healthy) if self.bot else None,
            "update": self.updater.status, "autoUpdate": bool(self.store.cfg.get("auto_update", True)),
            "board": s.get("board", []), "stats": s.get("stats", {}), "offlineSince": s.get("offline_since") or 0,
            "pax": {p["id"]: p["name"] for p in self.engine.passenger_list()} if self.engine else {},
            "done": self.store.state.get("done", {}),
            "targets": {h["order"]: self.engine.transfer_targets(h["order"]) for h in holds} if self.engine else {},
            "today": time.strftime("%Y-%m-%d", time.gmtime(now + 5 * 3600)),     # Tashkent date
        }

    # ---------------------------------------------------------------- setup (bot token + owner)
    def setup_token(self, token: str) -> dict:
        try:
            me = TgApi(token).call("getMe")
            TgApi(token).call("deleteWebhook", drop_pending_updates=True)
        except (TgError, requests.RequestException) as e:
            return {"ok": False, "error": f"That token didn't work: {e}"}
        self.pending = {"token": token, "bot": me["username"], "offset": 0, "owner": None}
        return {"ok": True, "bot": me["username"]}

    def setup_wait(self) -> dict:
        p = self.pending
        if not p.get("token"):
            return {"ok": False, "error": "Enter the bot token first."}
        try:
            ups = TgApi(p["token"]).call("getUpdates", http_timeout=30, timeout=20, offset=p["offset"])
        except (TgError, requests.RequestException) as e:
            return {"ok": False, "error": str(e)}
        for u in ups:
            p["offset"] = u["update_id"] + 1
            m = u.get("message")
            if m and m.get("from"):
                f = m["from"]
                p["owner"] = {"id": f["id"], "name": " ".join(x for x in (f.get("first_name"), f.get("last_name")) if x),
                              "username": f.get("username", "")}
        return {"ok": True, "owner": p["owner"]}

    def setup_confirm(self) -> dict:
        p = self.pending
        if not p.get("owner"):
            return {"ok": False, "error": "Press Start in your bot first."}
        try:
            TgApi(p["token"]).call("getUpdates", offset=p["offset"], timeout=0)
        except (TgError, requests.RequestException):
            pass
        self.store.set_telegram(p["token"], p["owner"]["id"], p["owner"]["name"], p["bot"])
        if not autostart.is_enabled():
            try:
                autostart.set_enabled(True)
            except OSError:
                pass
        self.pending = {}
        threading.Thread(target=self.start_services, daemon=True).start()
        return {"ok": True}

    # ---------------------------------------------------------------- local API
    def handle(self, method: str, path: str, body: dict):
        if path == "/api/state":
            return self.state()
        if path == "/api/activity":
            after = float(body.get("after") or 0)
            return {"rows": [r for r in list(self.activity) if r["ts"] > after][-200:]}
        if path == "/api/setup/token":
            return self.setup_token(str(body.get("token", "")).strip())
        if path == "/api/setup/wait":
            return self.setup_wait()
        if path == "/api/setup/confirm":
            return self.setup_confirm()
        if path == "/api/pause":
            self.store.cfg["paused"] = bool(body.get("paused"))
            self.store.save()
            rec.ev("user_pause" if self.store.cfg["paused"] else "user_resume", source="app")
            return {"ok": True}
        if path == "/api/trip":
            ok = self.store.set_trip_active(str(body.get("id")), bool(body.get("active")))
            return {"ok": ok}
        if path == "/api/transfer":
            if not self.engine:
                return {"ok": False, "error": "The bot isn't running yet."}
            ok, msg = self.engine.transfer_hold(str(body.get("order")), str(body.get("from")), str(body.get("to")))
            rec.ev("user_transfer", order=str(body.get("order")), ok=ok, source="app")
            return {"ok": ok, "error": msg}
        if path == "/api/passengers":
            if self.engine:
                self.engine.refresh_friends()
            return {"ok": True, "count": len(self.engine.friends) if self.engine else 0}
        if path == "/api/console":
            from .live import open_window
            ok = open_window()
            return {"ok": ok, "error": "" if ok else "No terminal app found. Run: uzrailbot live"}
        if path == "/api/trip/delete":
            ok = self.store.delete_trip(str(body.get("id")))
            rec.ev("trip_deleted", trip=str(body.get("id")), source="app")
            return {"ok": ok}
        if path == "/api/cancel":
            ok = self.engine.cancel_hold(str(body.get("order"))) if self.engine else False
            return {"ok": ok}
        if path == "/api/autostart":
            autostart.set_enabled(bool(body.get("on")))
            return {"ok": True, "on": autostart.is_enabled()}
        if path == "/api/open":
            target = body.get("target")
            if target == "telegram":
                webbrowser.open(f"https://t.me/{self.store.cfg['telegram'].get('bot_username', '')}")
            elif target == "folder":
                folder = str(paths.app_dir())
                if os.name == "nt":
                    os.startfile(folder)
                else:
                    subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", folder])
            elif target == "url" and str(body.get("url", "")).startswith("https://"):
                webbrowser.open(body["url"])
            return {"ok": True}
        if path == "/api/update":
            self.updater.check_now(install=bool(body.get("install")))
            return {"ok": True}
        if path == "/api/autoupdate":
            self.store.cfg["auto_update"] = bool(body.get("on"))
            self.store.save()
            return {"ok": True, "on": self.store.cfg["auto_update"]}
        if path == "/api/show":
            self.show()
            return {"ok": True}
        if path == "/api/quit":
            threading.Thread(target=self.quit, daemon=True).start()
            return {"ok": True}
        return None

    def serve(self) -> bool:
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, data: bytes, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _api(self, method):
                if self.headers.get("X-App-Key") != app.key:
                    return self._send(403, b'{"error":"forbidden"}')
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                except ValueError:
                    body = {}
                try:
                    res = app.handle(method, self.path.split("?")[0], body)
                except Exception as e:
                    log.exception("api")
                    return self._send(500, json.dumps({"error": str(e)}).encode())
                if res is None:
                    return self._send(404, b'{"error":"not found"}')
                self._send(200, json.dumps(res, ensure_ascii=False, default=str).encode("utf-8"))

            def do_GET(self):
                if self.path.startswith("/api/"):
                    return self._api("GET")
                name = self.path.split("?")[0].lstrip("/") or "index.html"
                if name not in app.ui_files:
                    return self._send(404, b"not found", "text/plain")
                self._send(200, app.ui_files[name], TYPES.get(Path(name).suffix, "application/octet-stream"))

            def do_POST(self):
                self._api("POST")

        try:
            server = _Server(("127.0.0.1", PORT), Handler)
        except OSError:
            return False
        self.ui_files = {f.name: f.read_bytes() for f in UI_DIR.iterdir() if f.is_file()}
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True, name="ui-server").start()
        (paths.data_dir() / "app.key").write_text(self.key, encoding="utf-8")
        self.url = f"http://127.0.0.1:{PORT}/#k={self.key}"
        return True

    # ---------------------------------------------------------------- window + tray
    def show(self):
        if self.window is not None:
            try:
                self.window.show()
                self.window.restore()
            except Exception:
                pass
        else:
            webbrowser.open(self.url)

    def quit(self):
        rec.ev("stop")
        self.stop_services()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass
        time.sleep(0.3)
        os._exit(0)

    @staticmethod
    def _tray_icon(color):
        from .icon import draw_icon
        return draw_icon(color, 64)

    def _tray_color(self):
        st = self.state()
        if st["holds"]:
            return "#F5A524"
        if st["paused"] or st["state"] in ("idle", "setup"):
            return "#8B96A8"
        if st["state"] in ("error", "maintenance"):
            return "#E5484D"
        return "#08BFA8"

    def start_tray(self):
        try:
            import pystray
        except ImportError:
            return

        def toggle_pause(_icon, _item):
            self.store.cfg["paused"] = not self.store.cfg.get("paused")
            self.store.save()

        menu = pystray.Menu(
            pystray.MenuItem("Open UzRail Bot", lambda *_: self.show(), default=True),
            pystray.MenuItem(lambda _i: "Resume watching" if self.store.cfg.get("paused") else "Pause watching",
                             toggle_pause),
            pystray.MenuItem("Open the Telegram bot", lambda *_: self.handle("POST", "/api/open", {"target": "telegram"})),
            pystray.MenuItem("Open live console", lambda *_: self.handle("POST", "/api/console", {})),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda *_: self.quit()),
        )
        color = self._tray_color()
        self.tray = pystray.Icon("uzrailbot", self._tray_icon(color), "UzRail Bot", menu)

        if sys.platform == "darwin":
            # macOS: the menu-bar icon must live on the main thread; run_detached() hooks it into the event loop
            # the window runs (pystray's documented way to share it). Its colour can't be changed from another
            # thread there, so it stays as it is.
            try:
                self.tray.run_detached()
            except Exception:
                log.exception("menu-bar icon unavailable")
                self.tray = None
            return

        def run_tray():
            try:
                self.tray.run()
            except Exception:          # e.g. no tray support on this Linux desktop
                log.exception("tray icon unavailable")
                self.tray = None       # then closing the window quits instead of hiding
        threading.Thread(target=run_tray, daemon=True, name="tray").start()

        def refresh():
            last = color
            while True:
                time.sleep(2)
                if self.tray is None:
                    return
                try:
                    c = self._tray_color()
                    st = self.state()
                    self.tray.title = f"UzRail Bot · {st['state']}" + (f" · {len(st['holds'])} held" if st["holds"] else "")
                    if c != last:
                        self.tray.icon = self._tray_icon(c)
                        last = c
                except Exception:
                    pass
        threading.Thread(target=refresh, daemon=True, name="tray-refresh").start()

    def run(self):
        if not self.serve():
            # another copy is running: ask it to show its window and stop here
            try:
                key = (paths.data_dir() / "app.key").read_text(encoding="utf-8").strip()
                requests.post(f"http://127.0.0.1:{PORT}/api/show", headers={"X-App-Key": key}, timeout=3)
            except (OSError, requests.RequestException):
                print("UzRail Bot is already running.")
            return
        folder = rec.open()
        rec.ev("start", version=__version__, mode="gui", updated_from=os.environ.get("UZRAILBOT_JUST_UPDATED"))
        self.updater.start()
        log.info("UzRail Bot %s, recording to %s, UI at http://127.0.0.1:%d", __version__, folder, PORT)
        if self.store.is_configured():
            threading.Thread(target=self.start_services, daemon=True).start()
        self.start_tray()
        try:
            import webview
        except ImportError:
            webview = None
        if webview is None:
            self._browser_fallback()
            return
        self.window = webview.create_window("UzRail Bot", self.url, width=1000, height=680, min_size=(820, 560),
                                            background_color="#F3F6FA", hidden=self.background)

        def on_closing():
            # closing the window keeps the bot running in the tray (on a Mac also without one: opening the app
            # again brings the window back)
            if self.tray is not None or sys.platform == "darwin":
                self.window.hide()
                return False
            return True
        self.window.events.closing += on_closing
        try:
            webview.start(private_mode=False, storage_path=str(paths.data_dir() / "webview"))
        except Exception:
            log.exception("window failed; using the browser instead")
            self.window = None
            self._browser_fallback()
            return
        self.quit()

    def _browser_fallback(self):
        """No embedded window available (e.g. missing WebKit on Linux): the tray + the default browser."""
        if not self.background:
            webbrowser.open(self.url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            self.quit()
