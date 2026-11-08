"""Usage logs (offline: the upload is faked). Run: python tests/test_telemetry.py"""
import gzip
import os
import sys
import tempfile

os.environ["UZRAILBOT_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from uzrailbot import paths, telemetry  # noqa: E402
from uzrailbot.recorder import rec  # noqa: E402
from uzrailbot.store import Store  # noqa: E402

# ---- personal data is blanked out
s = telemetry.scrub('{"error": "login failed for ali.v@mail.uz, call +998 90 123 45 67, passport AB1234567, '
                    'pay https://pay.hamkorbank.uz/x?order=UO1"}')
assert "ali.v" not in s and "<email>" in s, s
assert "123 45" not in s and "<phone>" in s, s
assert "AB1234567" not in s and "<document>" in s, s
assert "hamkorbank" not in s and "<link>" in s, s
assert "UO781FQB3X91WZ" in telemetry.scrub("order UO781FQB3X91WZ seat 12 train 056Ж"), "order ids and trains stay"

# ---- a huge session drops the per-check lines first
big = "\n".join(['{"kind": "check", "n": %d, "pad": "%s"}' % (i, os.urandom(40).hex()) for i in range(120000)]
                + ['{"kind": "held", "order": "UO1"}'])
gz = telemetry.pack(big)
assert gz and len(gz) <= telemetry.MAX_GZ and b'"held"' in gzip.decompress(gz) and b'"check"' not in gzip.decompress(gz)

# ---- at start: earlier unsent sessions only; when closing: only the current one; sent ones are marked
store = Store()
root = paths.app_dir() / "logs" / "sessions"
for name in ("20261101-100000", "20261102-100000"):
    (root / name).mkdir(parents=True)
    (root / name / "events.jsonl").write_text('{"kind": "start"}\n', encoding="utf-8")
rec.open()                                       # the current session
posted = []


class R:
    status_code = 204


telemetry.requests.post = lambda url, data, timeout, headers: posted.append(headers["X-Session"]) or R()
assert telemetry.upload(store) == 2 and posted == ["20261101-100000", "20261102-100000"], posted
assert telemetry.upload(store) == 0, "already sent sessions are not sent again"
posted.clear()
assert telemetry.upload(store, closing=True) == 1 and posted == [rec.folder.name], posted
store.cfg["share_logs"] = False
(root / "20261101-100000" / ".sent").unlink()
assert telemetry.upload(store) == 0, "switched off in Settings: nothing is sent"
assert len(store.cfg["install_id"]) == 24
print("telemetry: all checks passed")
