"""Build docs/timetable.json: every train, its stops with times, the days it runs and the ticket classes it
carries. The Telegram Mini App uses it to show the trains and classes of a route the moment From and To are
picked (it cannot ask the railway site itself: the site sends no CORS headers). The PC app still checks live
seats after a trip is saved, so a slightly old timetable only affects the suggestions.

    UZRAIL_EMAIL=... UZRAIL_PASSWORD=... python tools/build_timetable.py      (about 5-10 minutes)

1. Every station's schedule (only trains that START or END there are listed, with their running days),
   breadth-first over the stations the trains visit -> every train number.
2. Each train's route: stops with arrival/departure times.
3. Classes: the train list between each train's first and last Uzbek stop on dates 2-6 weeks ahead, when
   sales have just opened and every class still shows (sold-out classes are invisible in the API)."""
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

GAP = 0.34                       # seconds between requests: polite, well under the site's limits
CLASS_DAYS = (16, 23, 30, 9, 37, 2)   # days ahead to sample classes (far first: more seats on sale)


def safe(fn, *args, default=None):
    """One railway call; network hiccups (DNS, resets) are retried after a pause instead of ending the run."""
    for attempt in range(3):
        try:
            return fn(*args)
        except eticket.EticketError:
            return default
        except Exception as e:                       # requests' ConnectionError, timeouts...
            print(f"    network problem ({type(e).__name__}), retrying in {5 * (attempt + 1)} s", flush=True)
            time.sleep(5 * (attempt + 1))
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


def fill_missing(c, trains, names, tash):
    """Second pass for trains still without classes: the end stations (e.g. Khiva) often return nothing that
    far ahead, so try shorter stretches of the route and 'all Tashkent' instead of one Tashkent station."""
    today = dt.date.today()
    todo = [n for n, i in trains.items() if not i.get("c") and len(i.get("s") or []) >= 2]
    print(f"second pass: {len(todo)} trains without classes", flush=True)
    for n in todo:
        uz = [r[0] for r in trains[n]["s"] if r[0].startswith("2900")]
        alias = lambda code: "2900000" if code in tash else code
        pairs = []
        for a, b in ((0, -1), (1, -1), (0, -2), (1, -2), (0, len(uz) // 2), (len(uz) // 2, -1)):
            if len(uz) > max(a, abs(b)):
                p = (alias(uz[a]), alias(uz[b]))
                if p[0] != p[1] and p not in pairs:
                    pairs.append(p)
        for a, b in pairs[:4]:
            for k in (16, 30, 9):
                found = safe(c.trains, (today + dt.timedelta(days=k)).isoformat(), a, b, default=[])
                time.sleep(GAP)
                for t in found:
                    if t.get("number") in trains:
                        add_classes(trains[t["number"]], t)
                if trains[n].get("c"):
                    break
            if trains[n].get("c"):
                break
        print(f"  {n} {names.get(uz[0], uz[0])} -> {names.get(uz[-1], uz[-1])}: "
              f"{'/'.join(trains[n].get('c') or ['still unknown'])}", flush=True)


def discover(c, trains, stations, top=10, days=(1, 3, 5), save=None):
    """Station schedules only list trains that run TODAY, so also search the live train list between the
    biggest stations on several weekdays; any train not known yet gets its route fetched."""
    big = [s["c"] for s in sorted((s for s in stations if s.get("uzb") and s["c"] not in ("2900001", "2900002")),
                                  key=lambda s: -(s.get("t") or 0))][:top]
    today = dt.date.today()
    before = len(trains)
    for ai, a in enumerate(big):
        if save:
            save()                                   # keep what we have if the run is interrupted
        for b in big:
            if a == b:
                continue
            for k in days:
                found = safe(c.trains, (today + dt.timedelta(days=k)).isoformat(), a, b, default=[])
                time.sleep(GAP)
                for t in found:
                    n = t.get("number")
                    if not n:
                        continue
                    if n not in trains:
                        stops = safe(c.train_route, n, default=[])
                        time.sleep(GAP)
                        trains[n] = {"s": stop_rows(stops)}
                        print(f"  found {n} ({t.get('brand', '')}) between {a} and {b}", flush=True)
                    add_classes(trains[n], t)
    print(f"discovery: {len(trains) - before} more trains", flush=True)


def main():
    email, password = os.environ.get("UZRAIL_EMAIL"), os.environ.get("UZRAIL_PASSWORD")
    if not email or not password:
        raise SystemExit("set UZRAIL_EMAIL and UZRAIL_PASSWORD")
    c = eticket.Client(email, password)
    c.login()
    if "--fill-classes" in sys.argv or "--discover" in sys.argv:   # extra passes on an existing timetable.json
        dest = ROOT / "docs" / "timetable.json"
        out = json.loads(dest.read_text(encoding="utf-8"))
        stations = json.loads((ROOT / "docs" / "stations.json").read_text(encoding="utf-8"))
        names = {s["c"]: s["en"] for s in stations}
        def save():
            dest.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        if "--discover" in sys.argv:
            discover(c, out["trains"], stations, save=save)
        fill_missing(c, out["trains"], names, set(out["groups"].get("2900000", [])))
        out["trains"] = {n: {k: v for k, v in i.items() if v} for n, i in sorted(out["trains"].items()) if i.get("s")}
        dest.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"updated {dest}: {sum(1 for t in out['trains'].values() if t.get('c'))}/{len(out['trains'])} with classes")
        return
    stations = json.loads((ROOT / "docs" / "stations.json").read_text(encoding="utf-8"))
    names = {s["c"]: s["en"] for s in stations}
    queue = collections.deque(s["c"] for s in stations if s.get("uzb"))
    done_st, trains, routed = set(), {}, set()
    t_start = time.time()

    # 1 + 2: schedules and routes, breadth-first
    while queue:
        code = queue.popleft()
        if code in done_st:
            continue
        done_st.add(code)
        try:
            sched = c.station_schedule(code)
        except eticket.EticketError as e:
            print("schedule", code, e)
            continue
        time.sleep(GAP)
        for part in ("departure", "arrival"):
            for t in (sched.get("trains") or {}).get(part) or []:
                num = t.get("number")
                if not num:
                    continue
                info = trains.setdefault(num, {})
                if part == "departure" and t.get("schedule") and "r" not in info:
                    p = t["schedule"]
                    info["r"] = {k: p.get(k) for k in ("from", "to", "daysOfWeek", "dates")}
        for num in sorted(set(trains) - routed):
            routed.add(num)
            try:
                stops = c.train_route(num)
            except eticket.EticketError:
                stops = []
            time.sleep(GAP)
            trains[num]["s"] = stop_rows(stops)
            for s in stops:
                sc = str(s.get("code") or "")
                names.setdefault(sc, str(s.get("name") or ""))
                if sc.startswith("2900") and sc not in done_st:
                    queue.append(sc)
        print(f"{code} {sched.get('stationName', '')}: trains so far {len(trains)}, stations left {len(queue)} "
              f"({time.time() - t_start:.0f} s)", flush=True)

    # 3: classes and brand, sampled between each train's first and last Uzbek stop
    groups = collections.defaultdict(set)
    for num, info in trains.items():
        uz = [r[0] for r in info.get("s") or [] if r[0].startswith("2900")]
        if len(uz) >= 2:
            groups[(uz[0], uz[-1])].add(num)
    today = dt.date.today()
    for i, ((a, b), nums) in enumerate(sorted(groups.items()), 1):
        quiet = 0
        for k in CLASS_DAYS:
            d = (today + dt.timedelta(days=k)).isoformat()
            try:
                found = c.trains(d, a, b)
            except eticket.EticketError:
                found = []
            time.sleep(GAP)
            added = sum(add_classes(trains[t["number"]], t) for t in found if t.get("number") in trains)
            quiet = quiet + 1 if not added else 0
            if quiet >= 2 and all(trains[n].get("c") for n in nums):
                break
        print(f"classes {i}/{len(groups)} {names.get(a, a)} -> {names.get(b, b)}: "
              + ", ".join(f"{n}:{'/'.join(trains[n].get('c') or ['?'])}" for n in sorted(nums)), flush=True)

    # Tashkent's "all stations" code covers every Tashkent station
    tash = sorted({code for code, n in names.items() if str(n).upper().startswith(("TASHKENT", "ТАШКЕНТ"))} | {"2900000"})
    discover(c, trains, stations)
    fill_missing(c, trains, names, set(tash))
    out = {"v": 1, "built": today.isoformat(), "groups": {"2900000": tash},
           "trains": {n: {k: v for k, v in info.items() if v} for n, info in sorted(trains.items()) if info.get("s")}}
    dest = ROOT / "docs" / "timetable.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    with_classes = sum(1 for t in out["trains"].values() if t.get("c"))
    print(f"wrote {len(out['trains'])} trains ({with_classes} with classes) to {dest} "
          f"in {time.time() - t_start:.0f} s; Tashkent group {tash}")


if __name__ == "__main__":
    main()
