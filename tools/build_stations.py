"""Builds docs/stations.json: every station served by passenger trains, crawled from the railway's own
schedule and train-route data (needs a railway account login):

    UZRAIL_EMAIL=... UZRAIL_PASSWORD=... python tools/build_stations.py

Breadth-first from Tashkent: station schedule -> its trains -> each train's stops -> those stations...
Names are fetched in English, Uzbek and Russian so the Mini App can search any spelling.
Throttled to ~3 requests per second."""
from __future__ import annotations

import collections
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from uzrailbot import eticket  # noqa: E402

START = "2900000"           # Tashkent
LANGS = ("en", "uz", "ru")
GAP = 0.34                  # seconds between requests

# The railway API only returns English station names, so Uzbek and Russian names come from this table
# (English name as the API spells it -> Uzbek, Russian).
LOCAL_NAMES = {
    "TASHKENT": ("Toshkent", "Ташкент"), "TASHKENT CENTRAL": ("Toshkent Markaziy", "Ташкент Центральный"),
    "TASHKENT SOUTH": ("Toshkent Janubiy", "Ташкент Южный"), "TASHKENT NORTH": ("Toshkent Shimoliy", "Ташкент Северный"),
    "SAMARKAND": ("Samarqand", "Самарканд"), "BUKHARA": ("Buxoro", "Бухара"), "URGENCH": ("Urganch", "Ургенч"),
    "KHIVA": ("Xiva", "Хива"), "NUKUS": ("Nukus", "Нукус"), "KARSHI": ("Qarshi", "Карши"), "TERMEZ": ("Termiz", "Термез"),
    "NAVOI": ("Navoiy", "Навои"), "DJIZAKH": ("Jizzax", "Джизак"), "GULISTAN": ("Guliston", "Гулистан"),
    "ANDIJAN": ("Andijon", "Андижан"), "NAMANGAN": ("Namangan", "Наманган"), "KOKAND": ("Qo'qon", "Коканд"),
    "MARGILAN": ("Marg'ilon", "Маргилан"), "KUNGRAD": ("Qo'ng'irot", "Кунград"), "DENAU": ("Denov", "Денау"),
    "SHAKHRISABZ": ("Shahrisabz", "Шахрисабз"), "PAP": ("Pop", "Пап"), "ANGREN": ("Angren", "Ангрен"),
    "UCHKUDUK 2": ("Uchquduq", "Учкудук"), "KATTAKURG": ("Kattaqo'rg'on", "Каттакурган"),
    "KATTAKURGAN": ("Kattaqo'rg'on", "Каттакурган"), "YANGIYER": ("Yangiyer", "Янгиер"), "SHAVAT": ("Shovot", "Шават"),
    "KHODJEYLI": ("Xo'jayli", "Ходжейли"), "BERUNI": ("Beruniy", "Беруни"), "TURTKUL": ("To'rtko'l", "Турткуль"),
    "MISKIN": ("Miskin", "Мискин"), "KAGAN": ("Kogon", "Каган"), "GIJDUVAN": ("G'ijduvon", "Гиждуван"),
    "TAHIATASH": ("Taxiatosh", "Тахиаташ"), "TAKHIATASH": ("Taxiatosh", "Тахиаташ"), "BAYSUN": ("Boysun", "Байсун"),
    "SARIASIYA": ("Sariosiyo", "Сариасия"), "KUMKURGAN": ("Qumqo'rg'on", "Кумкурган"),
    "DJARKURGAN": ("Jarqo'rg'on", "Джаркурган"), "KHAVAST": ("Xovos", "Хаваст"), "BEKABAD": ("Bekobod", "Бекабад"),
    "CHIRCHIK": ("Chirchiq", "Чирчик"), "DJUMA": ("Juma", "Джума"), "KASAN": ("Koson", "Касан"),
    "KITAB": ("Kitob", "Китаб"), "GUZAR": ("G'uzor", "Гузар"), "ZARAFSHAN": ("Zarafshon", "Зарафшан"),
    "TASHGUZAR": ("Toshg'uzar", "Ташгузар"), "KHANABAD": ("Xonobod", "Ханабад"), "ASAKA": ("Asaka", "Асака"),
    "UCHKURGAN": ("Uchqo'rg'on", "Учкурган"), "KUVASAY": ("Quvasoy", "Кувасай"), "RISHTAN": ("Rishton", "Риштан"),
    "SYRDARYA": ("Sirdaryo", "Сырдарья"), "KHAZARASP": ("Hazorasp", "Хазарасп"), "KHANKA": ("Xonqa", "Ханка"),
    "BALAKCHI": ("Baliqchi", "Балыкчи"), "MANGIT": ("Mang'it", "Мангит"), "TINCHLIK": ("Tinchlik", "Тинчлик"),
    "ZIYOVUDDIN": ("Ziyovuddin", "Зиявуддин"), "NURABAD": ("Nurobod", "Нурабад"), "UZUN": ("Uzun", "Узун"),
}
LOCAL_NAMES.update({
    "HODJEILI": ("Xo'jayli", "Ходжейли"), "BOISUN": ("Boysun", "Байсун"), "HAZARASP": ("Hazorasp", "Хазарасп"),
    "SARYASIIA": ("Sariosiyo", "Сариасия"), "ZIEVUDDIN": ("Ziyovuddin", "Зиявуддин"), "ASSAKE": ("Asaka", "Асака"),
    "KYZYLTEPA": ("Qiziltepa", "Кызылтепа"), "DASHTOBOD": ("Dashtobod", "Даштабад"), "DARBAND": ("Darband", "Дарбанд"),
    "DEHKONOBOD": ("Dehqonobod", "Дехканабад"), "ELLIKALA": ("Ellikqal'a", "Элликкала"),
    "ALTYNKUL": ("Oltinko'l", "Алтынкуль"), "JASLYK": ("Jasliq", "Жаслык"),
    "KARAKALPAKST": ("Qoraqalpog'iston", "Каракалпакстан"), "AIRYTAM": ("Ayritom", "Айритам"),
    "OKRABOT": ("Oqrabot", "Акрабат"), "SHURCHI": ("Sho'rchi", "Шурчи"), "ZIRABULAK": ("Ziyrabuloq", "Зирабулак"),
    "ALTYARYK": ("Oltiariq", "Алтыарык"), "SURKHAN": ("Surxon", "Сурхан"), "TANGIMUSH": ("Tangimush", "Тангимуш"),
    "CHARTAK": ("Chortoq", "Чартак"), "CHUST": ("Chust", "Чуст"), "TUKIMACHI": ("To'qimachi", "Тукимачи"),
    "GUMBAZ": ("Gumbaz", "Гумбаз"),
})
# Some route stops come back in Cyrillic: give them a Latin (English-style) name.
CYRILLIC_EN = {"СУРХОН": "SURKHAN", "ТАНГИМУШ": "TANGIMUSH", "ЧОРТОК": "CHARTAK", "ЧУСТ": "CHUST",
               "ТУКИМАЧИ": "TUKIMACHI", "ГУМБАЗ": "GUMBAZ"}
# Stations people book that the schedule crawl can miss: seeded through the station search.
SEED_QUERIES = ["Tashkent", "Shakhrisabz", "Karshi", "Termez", "Nukus", "Kungrad", "Denau", "Uchkuduk", "Navoi",
                "Kitab", "Guzar", "Khiva", "Urgench", "Andijan", "Namangan", "Kokand", "Margilan", "Pap", "Angren",
                "Bukhara", "Samarkand", "Djizakh", "Gulistan", "Khavast", "Yangiyer", "Sariasiya", "Baysun",
                "Kumkurgan", "Djarkurgan", "Beruni", "Turtkul", "Khodjeyli", "Takhiatash", "Miskin", "Kagan",
                "Gijduvan", "Kattakurgan", "Djuma", "Shavat", "Khazarasp", "Khanka", "Asaka", "Kasan", "Uchkurgan"]


def enrich(out: list, client) -> list:
    """Add stations found by search (e.g. the all-Tashkent code 2900000) and Uzbek/Russian names."""
    have = {s["c"] for s in out}
    for q in SEED_QUERIES:
        try:
            found = client.stations(q)
        except eticket.EticketError:
            found = []
        time.sleep(GAP)
        for st in found:
            code = str(st["code"])
            if code.startswith("2900") and code not in have:
                have.add(code)
                out.append({"c": code, "en": st["name"], "uz": "", "ru": "", "t": 0, "uzb": True})
    for s in out:
        s["en"] = CYRILLIC_EN.get(s["en"].upper(), s["en"])
        uz, ru = LOCAL_NAMES.get(s["en"].upper(), ("", ""))
        s["uz"] = uz if uz and uz.upper() != s["en"].upper() else ""
        s["ru"] = ru
    for s in out:                       # the all-stations Tashkent code goes first
        if s["c"] == "2900000":
            s["t"] = 999
    out.sort(key=lambda s: (-s["t"], s["en"]))
    return out


def main():
    dest = ROOT / "docs" / "stations.json"
    if "--enrich-only" in sys.argv:          # re-run only the search seeds + local names on an existing file
        out = json.loads(dest.read_text(encoding="utf-8"))
        out = enrich(out, eticket.Client())
        dest.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"enriched: {len(out)} stations")
        return
    email, password = os.environ.get("UZRAIL_EMAIL"), os.environ.get("UZRAIL_PASSWORD")
    if not email or not password:
        raise SystemExit("set UZRAIL_EMAIL and UZRAIL_PASSWORD")
    clients = {lang: eticket.Client(email, password, lang=lang) for lang in LANGS}
    for c in clients.values():
        c.login()
        time.sleep(GAP)
    en = clients["en"]
    names = collections.defaultdict(dict)       # code -> {lang: name}
    trains_at = collections.Counter()           # code -> number of trains stopping there
    seen_trains, done_stations = set(), set()
    queue = collections.deque([START])
    while queue:
        code = queue.popleft()
        if code in done_stations:
            continue
        done_stations.add(code)
        try:
            sched = en.station_schedule(code)
        except eticket.EticketError as e:
            print("schedule", code, e)
            continue
        time.sleep(GAP)
        numbers = set()
        for part in ("departure", "arrival"):
            for t in (sched.get("trains") or {}).get(part) or []:
                if t.get("number"):
                    numbers.add(t["number"])
        print(f"{code} {sched.get('stationName', '')}: {len(numbers)} trains, queue {len(queue)}", flush=True)
        for number in sorted(numbers - seen_trains):
            seen_trains.add(number)
            for lang, c in clients.items():
                try:
                    stops = c.train_route(number)
                except eticket.EticketError:
                    stops = []
                time.sleep(GAP)
                for s in stops:
                    sc = str(s.get("code") or "")
                    if not sc:
                        continue
                    names[sc][lang] = str(s.get("name") or "").strip()
                    if lang == "en":
                        trains_at[sc] += 1
                        if sc.startswith("2900") and sc not in done_stations:
                            queue.append(sc)
    out = []
    for code, n in names.items():
        out.append({"c": code, "en": n.get("en") or n.get("uz") or n.get("ru"), "uz": n.get("uz", ""),
                    "ru": n.get("ru", ""), "t": trains_at[code], "uzb": code.startswith("2900")})
    out = enrich(out, en)
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {len(out)} stations ({sum(1 for s in out if s['uzb'])} in Uzbekistan) from {len(seen_trains)} trains "
          f"to {dest}")


if __name__ == "__main__":
    main()
