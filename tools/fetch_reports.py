"""Download the usage logs apps sent (tools/logs-worker). For the maintainer only: needs the admin key.

    python tools/fetch_reports.py [--since 20261108] [--out reports] [--key-file ../cloudflare.txt]

The key comes from UZRAIL_LOGS_KEY or a key file with a line "logs_admin_key=...". Files land in
<out>/<day>/<install>/<session>.jsonl (unzipped); ones already downloaded are skipped.
Read one with: python -m uzrailbot report <file>   (or open it: one JSON event per line)."""
import argparse
import gzip
import json
import os
import pathlib
import urllib.request

BASE = "https://uzrail-logs.sadrikov49.workers.dev"


def key_from(path):
    if os.environ.get("UZRAIL_LOGS_KEY"):
        return os.environ["UZRAIL_LOGS_KEY"]
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("logs_admin_key="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"no logs_admin_key in {path} and UZRAIL_LOGS_KEY is not set")


def get(path, key):
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + key, "User-Agent": "UzRailBot-reports"})  # Cloudflare blocks "Python-urllib" (error 1010)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="", help="only sessions on or after this day (YYYYMMDD)")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--key-file", default=str(pathlib.Path(__file__).resolve().parents[2] / "cloudflare.txt"))
    a = ap.parse_args()
    key = key_from(a.key_file)
    objects, cursor = [], ""
    while True:
        page = json.loads(get("/admin/list" + (f"?cursor={cursor}" if cursor else ""), key))
        objects += page["objects"]
        cursor = page.get("cursor")
        if not cursor:
            break
    new = 0
    for o in objects:
        day = o["key"].split("/")[1]
        if a.since and day < a.since:
            continue
        dest = pathlib.Path(a.out) / o["key"].removeprefix("logs/").removesuffix(".gz")
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(gzip.decompress(get("/admin/get?key=" + urllib.request.quote(o["key"]), key)))
        new += 1
        print(f"{o['key']}  {o['size'] // 1024} KB  v{o['meta'].get('version', '?')}  {o['meta'].get('os', '')}")
    print(f"{len(objects)} sessions on the server, {new} downloaded to {a.out}")


if __name__ == "__main__":
    main()
