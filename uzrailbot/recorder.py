"""Flight recorder: every check, request timing, detection, booking step, websocket event and error
goes to logs/sessions/<start time>/events.jsonl, so any run can be replayed and explained afterwards
(`python -m uzrailbot report`). Passport numbers and full names are never written."""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time

from . import paths

KEEP_SESSIONS = 20


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.f = None
        self.folder = None

    def open(self):
        root = paths.app_dir() / "logs" / "sessions"
        root.mkdir(parents=True, exist_ok=True)
        old = sorted(p for p in root.iterdir() if p.is_dir())
        for p in old[:-KEEP_SESSIONS]:
            for child in p.iterdir():
                child.unlink(missing_ok=True)
            p.rmdir()
        self.folder = root / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.folder.mkdir(parents=True, exist_ok=True)
        self.f = open(self.folder / "events.jsonl", "a", encoding="utf-8", buffering=1)
        self.ev("session_start", pid=os.getpid())
        return self.folder

    def ev(self, kind: str, **data):
        if self.f is None:
            return
        now = time.time()
        row = {"t": dt.datetime.fromtimestamp(now).strftime("%H:%M:%S.%f")[:-3], "ts": round(now, 3), "kind": kind}
        row.update(data)
        line = json.dumps(row, ensure_ascii=False, default=str)
        with self.lock:
            try:
                self.f.write(line + "\n")
            except (OSError, ValueError):
                pass


rec = Recorder()
