"""Live console: follows the running bot's recording and prints every check and booking step.

    python -m uzrailbot live

Read-only: it never talks to the railway site, so it costs nothing and can't slow the bot down."""
from __future__ import annotations

import json
import os
import sys
import time

from . import paths

RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = "\033[91m", "\033[92m", "\033[93m", "\033[94m", "\033[95m", "\033[96m"

BIG = {
    "availability": (YELLOW, "SEATS"), "detect": (MAGENTA, "DETECT"), "fanout": (MAGENTA, "FAN-OUT"),
    "plan": (CYAN, "PLAN"), "cars_retry": (CYAN, "CARS-RETRY"), "reserve_sent": (MAGENTA, "RESERVE"),
    "reserve_refused": (RED, "REFUSED"), "settled": (CYAN, "SETTLED"), "fanout_extra_cancelled": (DIM, "EXTRA-CANCEL"), "extra_hold_cancelled": (YELLOW, "EXTRA-CANCEL"),
    "held": (GREEN, "HELD"), "pay_link": (GREEN, "PAY-LINK"), "pay_link_fail": (RED, "PAYLINK-FAIL"),
    "seats_gone": (RED, "GONE"), "missed": (RED, "MISSED"), "hold_end": (YELLOW, "HOLD-END"),
    "renew_attempt": (YELLOW, "RENEW"), "paid": (GREEN, "PAID"), "book_error": (RED, "ERROR"),
    "check_fail": (RED, "CHECK-FAIL"), "http_fail": (RED, "NET-FAIL"), "state": (BLUE, "STATE"),
    "learned_cars": (BLUE, "LEARNED"), "heartbeat": (DIM, "HEARTBEAT"), "watch_start": (BLUE, "START"),
    "notify": (GREEN, "NOTIFY"), "recover_cancel": (YELLOW, "RECOVER"),
}


def latest_events_file():
    root = paths.app_dir() / "logs" / "sessions"
    sessions = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
    return sessions[-1] / "events.jsonl" if sessions else None


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


def main():
    os.system("")                    # turn on colours in the Windows console
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    path = None
    while path is None:
        path = latest_events_file()
        if path is None:
            print("Waiting for the bot to start recording...")
            time.sleep(2)
    print(f"{BOLD}UzRail live view{RESET}  following {path}\n"
          f"{DIM}Every check of the railway site is one line. Coloured lines are booking steps. Ctrl+C to close.{RESET}\n")
    f = open(path, encoding="utf-8")
    window, last_summary = [], time.time()
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
            time.sleep(0.1)
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        kind, t = e.get("kind"), e.get("t", "")
        if kind == "check":
            window.append(e.get("ms", 0))
            print(f"{DIM}{t}{RESET}  check #{e.get('n', '?'):<6} {e.get('ms', '?'):>4} ms  {e.get('date', '')}  "
                  f"{fmt_trains(e.get('trains') or {})}")
        elif kind == "http":
            if not e.get("path", "").endswith("trains/list"):
                color = RED if e.get("status", 200) >= 400 else DIM
                print(f"{DIM}{t}{RESET}  {color}request {e.get('m')} {e.get('path')} -> {e.get('status')} "
                      f"in {e.get('ms')} ms{RESET}")
        elif kind == "ws":
            color = GREEN if e.get("state") == "PAYMENT_STARTED" else RED if "FAIL" in str(e.get("state")) else CYAN
            print(f"{DIM}{t}{RESET}  {color}live event {e.get('state')} order {e.get('order')} "
                  f"seats {e.get('seats')}{RESET}")
        elif kind in BIG:
            color, tag = BIG[kind]
            extra = {k: v for k, v in e.items() if k not in ("t", "ts", "kind")}
            print(f"{DIM}{t}{RESET}  {color}{BOLD}{tag:<12}{RESET}{color} {json.dumps(extra, ensure_ascii=False)[:220]}{RESET}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
