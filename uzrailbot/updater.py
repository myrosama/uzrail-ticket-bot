"""Self-update from GitHub.

On start and every 6 hours: read __version__ from the repo's main branch. If it is newer, download the code,
install it into this app's own Python environment while the old version keeps watching, then restart, but
never while a booking is in flight or seats are held (a restart then waits). Only installs made by
install.ps1 / install.sh update themselves; a source checkout is left alone."""
from __future__ import annotations

import importlib
import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import requests

from . import __version__, paths
from .recorder import rec

log = logging.getLogger("uzrailbot.update")
REPO = "myrosama/uzrail-ticket-bot"
VERSION_URL = os.environ.get("UZRAILBOT_UPDATE_VERSION_URL") or \
    f"https://raw.githubusercontent.com/{REPO}/main/uzrailbot/__init__.py"
ZIP_URL = os.environ.get("UZRAILBOT_UPDATE_ZIP_URL") or f"https://github.com/{REPO}/archive/refs/heads/main.zip"
CHECK_EVERY = 6 * 3600
APP_FILES = ("uzrailbot", "pyproject.toml", "requirements.txt", "README.md", "LICENSE")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW: no console flash under pythonw


def parse(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


def remote_version(timeout: float = 8) -> str | None:
    r = requests.get(VERSION_URL, timeout=timeout, headers={"Cache-Control": "no-cache"})
    r.raise_for_status()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', r.text)
    return m.group(1) if m else None


def managed() -> bool:
    """True for the copy the installer made (its venv lives in the app folder)."""
    try:
        return Path(sys.prefix).resolve() == (paths.app_dir() / "venv").resolve()
    except OSError:
        return False


def _console_python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def restart(from_version: str):
    """Start the new version with the same arguments, then leave."""
    env = dict(os.environ, UZRAILBOT_JUST_UPDATED=from_version)
    args = [sys.executable, "-m", "uzrailbot", *sys.argv[1:]]
    log.info("restarting into the new version: %s", args)
    if os.name != "nt":
        os.execve(sys.executable, args, env)                 # same PID: fine for systemd and terminals
    if sys.stdout is not None and sys.stdout.isatty():       # console mode: keep using this console
        os._exit(subprocess.call(args, env=env))
    subprocess.Popen(args, env=env, close_fds=True,
                     creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    os._exit(0)


class Updater:
    def __init__(self, store, can_restart, before_restart):
        self.store = store
        self.can_restart = can_restart          # -> bool: no booking in flight, no seats held
        self.before_restart = before_restart    # stop the bot and free the port
        self.wake = threading.Event()
        self.force = False
        self.lock = threading.Lock()
        self.status = {"state": "dev" if not managed() else "idle", "current": __version__, "latest": None,
                       "checked": 0, "error": ""}

    def start(self):
        threading.Thread(target=self._loop, name="updater", daemon=True).start()

    def check_now(self, install: bool = False):
        self.force = self.force or install
        self.wake.set()

    def _set(self, state, **kw):
        self.status.update(state=state, **kw)

    def _loop(self):
        if os.environ.get("UZRAILBOT_JUST_UPDATED"):
            self.wake.wait(CHECK_EVERY)                      # we just updated: no need to look again now
            self.wake.clear()
        while True:
            try:
                self._cycle()
            except Exception as e:                           # never let the updater take the bot down
                log.exception("update check")
                self._set("failed", error=str(e)[:200])
            self.wake.wait(CHECK_EVERY)
            self.wake.clear()

    def _cycle(self):
        if not managed():
            self._set("dev")
            return
        force, self.force = self.force, False
        self._set("checking")
        try:
            latest = remote_version()
        except requests.RequestException as e:
            self._set("offline", error=type(e).__name__)
            return
        self.status.update(latest=latest, checked=time.time(), error="")
        if not latest or parse(latest) <= parse(__version__):
            self._set("current")
            return
        rec.ev("update_found", current=__version__, latest=latest)
        if not self.store.cfg.get("auto_update", True) and not force:
            self._set("available")
            return
        with self.lock:
            self._set("downloading")
            if not self._install(latest):
                return
        self._set("ready")
        rec.ev("update_installed", version=latest)
        while not self.can_restart():                        # never interrupt a booking or a held seat
            time.sleep(15)
        self._set("restarting")
        rec.ev("update_restart", old=__version__, new=latest)
        self.before_restart()
        restart(__version__)

    def _install(self, latest: str) -> bool:
        tmp = Path(tempfile.mkdtemp(prefix="uzrailbot-update-"))
        try:
            r = requests.get(ZIP_URL, timeout=120)
            r.raise_for_status()
            zipfile.ZipFile(io.BytesIO(r.content)).extractall(tmp)
            src = next(p for p in tmp.iterdir() if p.is_dir())
            got = re.search(r'__version__\s*=\s*"([^"]+)"', (src / "uzrailbot" / "__init__.py").read_text("utf-8"))
            if not got or parse(got.group(1)) < parse(latest):
                self._set("failed", error="the download is not the new version yet; will retry")
                return False
            # the running version must not pick up new code halfway: load all of its modules first
            for name in ("engine", "telegram", "eticket", "icon", "power", "autostart", "classes", "crypto",
                         "store", "paths", "recorder", "live", "gui"):
                try:
                    importlib.import_module(f"uzrailbot.{name}")
                except Exception:
                    pass
            self._set("installing")
            py = _console_python()
            p = subprocess.run([py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--upgrade",
                                str(src)], capture_output=True, text=True, timeout=900, creationflags=NO_WINDOW)
            if p.returncode != 0:
                self._set("failed", error=(p.stderr or p.stdout).strip()[-300:])
                rec.ev("update_failed", version=latest, error=(p.stderr or p.stdout).strip()[-300:])
                return False
            check = subprocess.run([py, "-I", "-c", "import uzrailbot; print(uzrailbot.__version__)"],
                                   capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
            if parse(check.stdout.strip()) < parse(latest):
                self._set("failed", error=f"installed {check.stdout.strip() or '?'}, expected {latest}")
                return False
            app_copy = paths.app_dir() / "app"                # the installer's copy (shortcut icon lives here)
            shutil.rmtree(app_copy, ignore_errors=True)
            app_copy.mkdir(parents=True, exist_ok=True)
            for item in APP_FILES:
                s = src / item
                if s.is_dir():
                    shutil.copytree(s, app_copy / item, ignore=shutil.ignore_patterns("__pycache__"))
                elif s.exists():
                    shutil.copy2(s, app_copy / item)
            return True
        except Exception as e:
            log.exception("update install")
            self._set("failed", error=str(e)[:200])
            rec.ev("update_failed", version=latest, error=str(e)[:200])
            return False
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
