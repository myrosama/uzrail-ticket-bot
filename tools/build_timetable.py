"""Build docs/timetable.json: every train, its stops with times, the weekdays it runs and the ticket classes
(car types) it carries. The Telegram Mini App uses it to list the trains and classes of ANY route the moment
From and To are picked: a train's stop list covers every station pair along it, so no route has to be
searched one by one. (The Mini App can't ask the railway site itself: the site sends no CORS headers.)
The PC app still checks live seats after a trip is saved, so an old timetable only affects suggestions.

    UZRAIL_EMAIL=... UZRAIL_PASSWORD=... python tools/build_timetable.py            full build (~15 min)
    ... python tools/build_timetable.py --resume     continue from the saved file (skips finished steps)
    ... python tools/build_timetable.py --fill-classes   only retry trains whose classes are unknown

Steps (progress is saved after each one, so a dropped connection loses nothing):
 1. every station's schedule: trains that start or end there today, with their printed running days;
 2. the live train list between every pair of the biggest stations on each of the next 7 days: finds every
    train between them and which weekdays it really runs;
 3. each train's route: stops with arrival/departure times and day offsets;
 4. classes: the train list between each train's first and last Uzbek stop 2-6 weeks ahead, when sales have
    just opened and every class still shows (sold-out classes are invisible in the API), then a second pass
    on shorter stretches for trains whose end stations return nothing (e.g. Khiva).
The pace stays near 3 requests a second, well under the site's limits."""
from __future__ import annotations

import collections
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from uzrailbot import eticket  # noqa: E402

GAP = 0.34                              # seconds between requests (schedules, routes, classes)
LIST_GAP = 0.2                          # between train-list searches in step 2 (~4 a second with the reply time)
HUBS = 16                               # biggest stations searched pairwise in step 2
CLASS_DAYS = (16, 23, 30, 9, 37, 2)     # days ahead to sample classes (far first: more seats on sale)
DEST = ROOT / "docs" / "timetable.json"


def safe(fn, *args, default=None):
    """One railway call; network hiccups (DNS, resets) are retried after a pause instead of ending the run."""
    for attempt in range(4):
        try:
            return fn(*args)
        except eticket.EticketError:
            return default
        except Exception as e:                       # requests' ConnectionError, timeouts...
            wait = 5 * (attempt + 1)
            print(f"    network problem ({type(e).__name__}), retrying in {wait} s", flush=True)
            time.sleep(wait)
    return default


def hm(s):
    if not s:
        return None
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def stop_rows(stops):
    """[[code, arr, dep, arrDay, depDay]]: days counted from the origin's departure day."""
    if not stops:
        return []
    d0 = hm(stops[0]["time"].get("departure")) or 0
    rows = []
    for s in stops:
        t = s.get("time") or {}
        ow = t.get("onWay") or {}
        on = int(ow.get("days") or 0) * 1440 + int(ow.get("hours") or 0) * 60 + int(ow.get("minutes") or 0)
        arr_abs = d0 + on
        dep_abs = arr_abs + int(t.get("waiting") or 0)
        rows.append([str(s.get("code")), t.get("arrival") or "", t.get("departure") or "",
                     arr_abs // 1440 if t.get("arrival") else 0, dep_abs // 1440])
    return rows


def add_classes(info, t) -> int:
    """Merge one train-list entry's classes (and brand) into the timetable entry. Returns how many were new."""
    if t.get("brand"):
        info["b"] = t["brand"]
    cl, added = set(info.get("c") or []), 0
    for car in t.get("cars") or []:
        for tf in car.get("tariffs") or []:
            if tf.get("classServiceType") and tf["classServiceType"] not in cl:
                cl.add(tf["classServiceType"])
                added += 1
    info["c"] = sorted(cl)
    return added


class Build:
    def __init__(self, client, resume: bool):
        self.c = client
        self.stations = json.loads((ROOT / "docs" / "stations.json").read_text(encoding="utf-8"))
        self.names = {s["c"]: s["en"] for s in self.stations}
        self.out = {"v": 2, "built": dt.date.today().isoformat(), "steps": [], "groups": {}, "trains": {}}
        if resume and DEST.exists():
            old = json.loads(DEST.read_text(encoding="utf-8"))
            self.out.update({k: old[k] for k in ("steps", "groups", "trains") if k in old})
            print(f"resuming: {len(self.out['trains'])} trains, steps done {self.out['steps']}", flush=True)
        self.trains = self.out["trains"]
        self.t0 = time.time()

    def save(self, step=None):
        if step and step not in self.out["steps"]:
            self.out["steps"].append(step)
        self.out["built"] = dt.date.today().isoformat()
        DEST.write_text(json.dumps(self.out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    def log(self, msg):
        print(f"[{time.time() - self.t0:5.0f} s] {msg}", flush=True)

    def tash(self) -> set:
        return {code for code, n in self.names.items() if str(n).upper().startswith(("TASHKENT", "ТАШКЕНТ"))} | {"2900000"}

    def route(self, num):
        stops = safe(self.c.train_route, num, default=[])
        time.sleep(GAP)
        for s in stops:
            self.names.setdefault(str(s.get("code")), str(s.get("name") or ""))
        return stop_rows(stops)

    # 1 ----------------------------------------------------------------------------------------------
    def schedules(self):
        queue = collections.deque(s["c"] for s in self.stations if s.get("uzb"))
        done = set()
        while queue:
            code = queue.popleft()
            if code in done:
                continue
            done.add(code)
            sched = safe(self.c.station_schedule, code, default={}) or {}
            time.sleep(GAP)
            new = []
            for part in ("departure", "arrival"):
                for t in (sched.get("trains") or {}).get(part) or []:
                    num = t.get("number")
                    if not num:
                        continue
                    info = self.trains.setdefault(num, {})
                    if part == "departure" and t.get("schedule") and "r" not in info:
                        p = t["schedule"]
                        info["r"] = {k: p.get(k) for k in ("from", "to", "daysOfWeek", "dates")}
                    if not info.get("s"):
                        new.append(num)
            for num in new:
                self.trains[num]["s"] = self.route(num)
                for row in self.trains[num]["s"]:
                    if row[0].startswith("2900") and row[0] not in done:
                        queue.append(row[0])
            if new:
                self.log(f"schedule {code} {sched.get('stationName', '')}: +{len(new)} trains, total {len(self.trains)}")
        self.save("schedules")

    # 2 ----------------------------------------------------------------------------------------------
    def discover(self):
        tash = self.tash()
        hubs = [s["c"] for s in sorted((s for s in self.stations if s.get("uzb") and s["c"] not in tash - {"2900000"}),
                                       key=lambda s: -(s.get("t") or 0))][:HUBS]
        self.log("hubs: " + ", ".join(self.names.get(h, h) for h in hubs))
        today = dt.date.today()
        prog = self.out.setdefault("progress", {"hubs_done": [], "w": {}})   # resumable hub by hub
        seen_days = collections.defaultdict(set, {n: set(v) for n, v in prog["w"].items()})
        found_new = 0
        for i, a in enumerate(hubs, 1):
            if a in prog["hubs_done"]:
                continue
            a_codes = tash if a == "2900000" else {a}
            for b in hubs:
                if a == b:
                    continue
                for k in range(1, 8):                        # the next 7 days: every weekday once
                    day = today + dt.timedelta(days=k)
                    found = safe(self.c.trains, day.isoformat(), a, b, default=[]) or []
                    time.sleep(LIST_GAP)
                    for t in found:
                        n = t.get("number")
                        if not n:
                            continue
                        info = self.trains.setdefault(n, {})
                        if not info.get("s"):
                            info["s"] = self.route(n)
                            found_new += 1
                            self.log(f"  new train {n} ({t.get('brand', '')}) {self.names.get(a, a)} -> {self.names.get(b, b)}")
                        add_classes(info, t)
                        # weekday it LEFT ITS FIRST STATION (JS numbering: Sunday = 0)
                        dep_day = next((r[4] for r in info["s"] if r[0] in a_codes and r[2]), None)
                        if dep_day is not None:
                            origin = day - dt.timedelta(days=dep_day)
                            seen_days[n].add((origin.weekday() + 1) % 7)
            prog["hubs_done"].append(a)
            prog["w"] = {n: sorted(d) for n, d in seen_days.items()}
            self.log(f"hub {i}/{len(hubs)} {self.names.get(a, a)} done; {len(self.trains)} trains ({found_new} new)")
            self.save()
            self.c.ensure_login()                        # the token lasts 60 minutes
        for n, days in seen_days.items():
            self.trains[n]["w"] = sorted(days)
        self.out["weekdays"] = "next 7 days from " + today.isoformat()
        self.out.pop("progress", None)
        self.save("discover")

    # 3 ----------------------------------------------------------------------------------------------
    def routes(self):
        missing = [n for n, i in self.trains.items() if not i.get("s")]
        for n in missing:
            self.trains[n]["s"] = self.route(n)
        self.log(f"routes: fetched {len(missing)} missing")
        self.save("routes")

    # 4 ----------------------------------------------------------------------------------------------
    def classes(self):
        groups = collections.defaultdict(set)
        for num, info in self.trains.items():
            uz = [r[0] for r in info.get("s") or [] if r[0].startswith("2900")]
            if len(uz) >= 2:
                groups[(uz[0], uz[-1])].add(num)
        today = dt.date.today()
        for i, ((a, b), nums) in enumerate(sorted(groups.items()), 1):
            quiet = 0
            for k in CLASS_DAYS:
                found = safe(self.c.trains, (today + dt.timedelta(days=k)).isoformat(), a, b, default=[]) or []
                time.sleep(GAP)
                added = sum(add_classes(self.trains[t["number"]], t) for t in found if t.get("number") in self.trains)
                quiet = quiet + 1 if not added else 0
                if quiet >= 2 and all(self.trains[n].get("c") for n in nums):
                    break
            self.log(f"classes {i}/{len(groups)} {self.names.get(a, a)} -> {self.names.get(b, b)}: "
                     + ", ".join(f"{n}:{'/'.join(self.trains[n].get('c') or ['?'])}" for n in sorted(nums)))
            if i % 10 == 0:
                self.save()
        self.fill_missing()
        self.save("classes")

    def fill_missing(self):
        """Trains still without classes: their end stations (e.g. Khiva) often return nothing that far ahead,
        so try shorter stretches of the route and 'all Tashkent' instead of one Tashkent station."""
        tash = self.tash()
        today = dt.date.today()
        todo = [n for n, i in self.trains.items() if not i.get("c") and len(i.get("s") or []) >= 2]
        self.log(f"second pass: {len(todo)} trains without classes")
        for n in todo:
            uz = [r[0] for r in self.trains[n]["s"] if r[0].startswith("2900")]
            alias = lambda code: "2900000" if code in tash else code
            pairs = []
            for a, b in ((0, -1), (1, -1), (0, -2), (1, -2), (0, len(uz) // 2), (len(uz) // 2, -1)):
                if len(uz) > max(a, abs(b)):
                    p = (alias(uz[a]), alias(uz[b]))
                    if p[0] != p[1] and p not in pairs:
                        pairs.append(p)
            for a, b in pairs[:4]:
                for k in (16, 30, 9):
                    found = safe(self.c.trains, (today + dt.timedelta(days=k)).isoformat(), a, b, default=[]) or []
                    time.sleep(GAP)
                    for t in found:
                        if t.get("number") in self.trains:
                            add_classes(self.trains[t["number"]], t)
                    if self.trains[n].get("c"):
                        break
                if self.trains[n].get("c"):
                    break
            self.log(f"  {n} {self.names.get(uz[0], uz[0]) if uz else '?'} -> {self.names.get(uz[-1], uz[-1]) if uz else '?'}: "
                     f"{'/'.join(self.trains[n].get('c') or ['still unknown'])}")

    def finish(self):
        self.out["groups"] = {"2900000": sorted(self.tash())}
        self.out["trains"] = {n: {k: v for k, v in i.items() if v} for n, i in sorted(self.trains.items()) if i.get("s")}
        self.trains = self.out["trains"]
        self.save("done")
        tr = self.out["trains"]
        self.log(f"wrote {len(tr)} trains: {sum(1 for t in tr.values() if t.get('c'))} with classes, "
                 f"{sum(1 for t in tr.values() if t.get('w') or t.get('r'))} with running days -> {DEST}")


def main():
    email, password = os.environ.get("UZRAIL_EMAIL"), os.environ.get("UZRAIL_PASSWORD")
    if not email or not password:
        raise SystemExit("set UZRAIL_EMAIL and UZRAIL_PASSWORD")
    c = eticket.Client(email, password)
    c.login()
    b = Build(c, resume="--resume" in sys.argv or "--fill-classes" in sys.argv)
    if "--fill-classes" in sys.argv:
        b.fill_missing()
        b.finish()
        return
    for step in ("schedules", "discover", "routes", "classes"):
        if step in b.out["steps"]:
            b.log(f"{step}: already done")
            continue
        b.log(f"--- {step}")
        getattr(b, step)()
        c.ensure_login()                                  # the token lasts 60 min; refresh between steps
    b.finish()


if __name__ == "__main__":
    main()
