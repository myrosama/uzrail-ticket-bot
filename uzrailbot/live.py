"""Live console: follows the running bot's recording and prints everything it does, as it happens:
every check of the railway site (one line each), every booking step, live order events from the railway,
network problems, settings changes from Telegram, updates.

    python -m uzrailbot live          the last ~60 lines, then live
    python -m uzrailbot live --all    the whole session from its start

Read-only: it never talks to the railway site, so it costs nothing and can't slow the bot down.
The desktop app opens it in its own window (Activity → Open live console, or the tray menu)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import paths

RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = "\033[91m", "\033[92m", "\033[93m", "\033[94m", "\033[95m", "\033[96m"
TITLE = "UzRail Bot - live console"

BIG = {
    "availability": (YELLOW, "SEATS"), "detect": (MAGENTA, "DETECT"), "fanout": (MAGENTA, "FAN-OUT"),
    "plan": (CYAN, "PLAN"), "cars_retry": (CYAN, "CARS-RETRY"), "reserve_sent": (MAGENTA, "RESERVE"),
    "reserve_refused": (RED, "REFUSED"), "settled": (CYAN, "SETTLED"), "fanout_extra_cancelled": (DIM, "EXTRA-CANCEL"),
    "extra_hold_cancelled": (YELLOW, "EXTRA-CANCEL"),
    "held": (GREEN, "HELD"), "pay_link": (GREEN, "PAY-LINK"), "pay_link_fail": (RED, "PAYLINK-FAIL"),
    "seats_gone": (RED, "GONE"), "missed": (RED, "MISSED"), "hold_end": (YELLOW, "HOLD-END"),
    "renew_attempt": (YELLOW, "RENEW"), "paid": (GREEN, "PAID"), "book_error": (RED, "ERROR"),
    "check_fail": (RED, "CHECK-FAIL"), "http_fail": (RED, "NET-FAIL"), "state": (BLUE, "STATE"),
    "learned_cars": (BLUE, "LEARNED"), "heartbeat": (DIM, "HEARTBEAT"), "watch_start": (BLUE, "START"),
    "notify": (GREEN, "NOTIFY"), "recover_cancel": (YELLOW, "RECOVER"), "recover_hold": (YELLOW, "RECOVER"),
    "start": (BLUE, "BOT START"), "stop": (BLUE, "BOT STOP"), "session_start": (DIM, "SESSION"),
    "user_cancel_hold": (YELLOW, "YOU CANCELLED"), "user_pause": (BLUE, "PAUSED"), "user_resume": (BLUE, "RESUMED"),
    "trip_saved": (BLUE, "TRIP SAVED"), "trip_trains": (BLUE, "TRAINS"), "trip_deleted": (BLUE, "TRIP DELETED"),
    "account_saved": (GREEN, "ACCOUNT"), "miniapp": (DIM, "MINI APP"), "returned_from_bank": (CYAN, "BANK"),
    "update_found": (CYAN, "UPDATE"), "update_installed": (GREEN, "UPDATE"), "update_restart": (CYAN, "UPDATE"),
    "update_failed": (RED, "UPDATE"), "pdf_fail": (RED, "PDF-FAIL"),
}
CLASS_LEGEND = "3П plaskart · 2К kupe · 1Л SV · 1А lux · 2Е/1С/2В/1В sitting"


def latest_events_file():
    root = paths.app_dir() / "logs" / "sessions"
    sessions = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
    return sessions[-1] / "events.jsonl" if sessions else None


def station_names() -> dict:
    """Station code -> name, from the saved trips (read-only)."""
    try:
        cfg = json.loads((paths.data_dir() / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    names = {}
    for t in cfg.get("trips") or []:
        for end in ("from", "to"):
            s = t.get(end) or {}
            if s.get("code"):
                names[s["code"]] = str(s.get("name", "")).title()
    return names


def fmt_trains(trains: dict) -> str:
    if not trains:
        return DIM + "no trains listed" + RESET
    parts = []
    for number, classes in trains.items():
        if not classes:
            parts.append(f"{number} {DIM}sold out{RESET}")
        else:
            seats = " ".join(f"{c}:{(GREEN + BOLD) if n else ''}{n}{RESET}" for c, n in classes.items())
            parts.append(f"{number} {seats}")
    return "  ·  ".join(parts)


def tail_offset(path: Path, lines: int) -> int:
    """Byte offset where the last `lines` lines of the file start."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = pos = f.tell()
        found = 0
        while pos > 0 and found <= lines:
            step = min(65536, pos)
            pos -= step
            f.seek(pos)
            found += f.read(step).count(b"\n")
        if pos == 0 and found <= lines:
            return 0
        f.seek(pos)
        chunk = f.read(end - pos)
        cut = len(chunk)
        for _ in range(lines + 1):
            cut = chunk.rfind(b"\n", 0, cut)
            if cut < 0:
                return pos
        return pos + cut + 1


def print_event(e: dict, names: dict, window: list):
    kind, t = e.get("kind"), e.get("t", "")
    if kind == "check":
        window.append(e.get("ms", 0))
        route = e.get("route") or []
        where = f"{names.get(route[0], route[0])}→{names.get(route[1], route[1])} " if len(route) == 2 else ""
        print(f"{DIM}{t}{RESET}  check #{e.get('n', '?'):<6} {e.get('ms', '?'):>4} ms  {where}{e.get('date', '')}  "
              f"{fmt_trains(e.get('trains') or {})}")
    elif kind == "http":
        if not e.get("path", "").endswith("trains/list"):
            color = RED if (e.get("status") or 200) >= 400 else DIM
            print(f"{DIM}{t}{RESET}  {color}request {e.get('m')} {e.get('path')} -> {e.get('status')} "
                  f"in {e.get('ms')} ms{RESET}")
    elif kind == "ws":
        color = GREEN if e.get("state") == "PAYMENT_STARTED" else RED if "FAIL" in str(e.get("state")) else CYAN
        print(f"{DIM}{t}{RESET}  {color}live event {e.get('state')} order {e.get('order')} "
              f"seats {e.get('seats')}{RESET}")
    else:
        color, tag = BIG.get(kind, (RESET, str(kind).upper().replace("_", " ")))
        extra = {k: v for k, v in e.items() if k not in ("t", "ts", "kind")}
        print(f"{DIM}{t}{RESET}  {color}{BOLD}{tag:<14}{RESET}{color} {json.dumps(extra, ensure_ascii=False)[:220]}{RESET}")


def _set_title():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(TITLE)
        except Exception:
            pass
    else:
        sys.stdout.write(f"\033]0;{TITLE}\007")


def main(args=None):
    args = list(sys.argv[2:] if args is None else args)
    os.system("")                    # turn on colours in the Windows console
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    _set_title()
    path = None
    while path is None:
        path = latest_events_file()
        if path is None:
            print("Waiting for the bot to start recording...")
            time.sleep(2)
    names = station_names()
    print(f"{BOLD}UzRail live console{RESET}  following {path}\n"
          f"{DIM}Every check of the railway site is one line; coloured lines are booking steps and everything else "
          f"the bot does.\nClasses: {CLASS_LEGEND}. Close this window any time: the bot keeps running.{RESET}\n")
    f = open(path, encoding="utf-8")
    if "--all" not in args:
        f.seek(tail_offset(path, 60))
    window, last_summary, names_at = [], time.time(), time.time()
    while True:
        line = f.readline()
        if not line:
            newer = latest_events_file()
            if newer and newer != path:              # the bot was restarted: follow the new session
                path, f = newer, open(newer, encoding="utf-8")
                print(f"\n{BOLD}--- new session {newer.parent.name} ---{RESET}\n")
                continue
            if time.time() - last_summary > 10 and window:
                ms = sorted(window)
                print(f"{DIM}  last 10 s: {len(ms)} checks, median {ms[len(ms) // 2]} ms, slowest {ms[-1]} ms{RESET}")
                window, last_summary = [], time.time()
            if time.time() - names_at > 30:          # pick up trips added meanwhile
                names, names_at = station_names(), time.time()
            time.sleep(0.1)
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        print_event(e, names, window)


def open_window() -> bool:
    """Open the live console in its own terminal window. Returns False if no terminal could be started."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        exe = exe.with_name("python.exe")
    cmd = [str(exe), "-m", "uzrailbot", "live"]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("UZRAILBOT_JUST_UPDATED", None)
    try:
        if os.name == "nt":
            subprocess.Popen(cmd, env=env, cwd=str(paths.app_dir()), creationflags=subprocess.CREATE_NEW_CONSOLE)
            return True
        if sys.platform == "darwin":
            line = " ".join(f"'{c}'" for c in cmd)
            subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{line}"',
                              "-e", 'tell application "Terminal" to activate'], env=env)
            return True
        for term, flag in (("x-terminal-emulator", "-e"), ("gnome-terminal", "--"), ("konsole", "-e"),
                           ("xfce4-terminal", "-x"), ("mate-terminal", "-x"), ("kitty", ""), ("alacritty", "-e"),
                           ("xterm", "-e")):
            if shutil.which(term):
                subprocess.Popen([term] + ([flag] if flag else []) + cmd, env=env, start_new_session=True)
                return True
    except OSError:
        return False
    return False


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
