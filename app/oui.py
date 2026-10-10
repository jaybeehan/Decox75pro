"""Guess a device's manufacturer from its MAC address.

Uses Wireshark's "manuf" list (built from the IEEE registry, including the
smaller /28 and /36 blocks). A copy is downloaded when the Docker image is built,
and the dashboard refreshes it into /data about once a month.

Phones, tablets and laptops often use a random "private" MAC per Wi-Fi network,
which has no manufacturer. Those are detected and labelled as such.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import threading
import time
import urllib.request

log = logging.getLogger("oui")

SOURCES = [
    ("manuf", "https://www.wireshark.org/download/automated/data/manuf"),
    ("ieee", "https://standards-oui.ieee.org/oui/oui.csv"),
]
BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manuf.txt")
REFRESH_DAYS = 30


def _hex(s: str) -> str:
    return "".join(ch for ch in s.upper() if ch in "0123456789ABCDEF")


class OuiDB:
    def __init__(self):
        self.tables: dict[int, dict[str, str]] = {}  # prefix bits -> {hexprefix: vendor}
        self.source = ""
        self.lock = threading.Lock()

    # -- loading ---------------------------------------------------------
    def load_text(self, text: str, source: str = "") -> int:
        tables: dict[int, dict[str, str]] = {}
        if text.lstrip().startswith("Registry,"):
            for row in csv.DictReader(io.StringIO(text)):
                a = _hex(row.get("Assignment", ""))
                name = (row.get("Organization Name") or "").strip()
                if a and name:
                    tables.setdefault(len(a) * 4, {})[a] = name
        else:
            for line in text.splitlines():
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                prefix = parts[0].strip()
                name = (parts[2] if len(parts) > 2 and parts[2].strip() else parts[1]).strip()
                bits = 24
                if "/" in prefix:
                    prefix, b = prefix.split("/", 1)
                    try:
                        bits = int(b)
                    except ValueError:
                        continue
                h = _hex(prefix)
                if bits % 4 or len(h) * 4 < bits:
                    continue
                tables.setdefault(bits, {})[h[: bits // 4]] = name
        n = sum(len(t) for t in tables.values())
        if n:
            with self.lock:
                self.tables = tables
                self.source = source
        return n

    def load_file(self, path: str) -> int:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return self.load_text(f.read(), path)
        except FileNotFoundError:
            return 0

    # -- lookup ----------------------------------------------------------
    @staticmethod
    def is_random(mac: str) -> bool:
        h = _hex(mac)
        return len(h) >= 2 and bool(int(h[:2], 16) & 0x02)

    def lookup(self, mac: str) -> str:
        h = _hex(mac)
        if len(h) != 12:
            return ""
        if self.is_random(mac):
            return "Private (random) address"
        with self.lock:
            for bits in sorted(self.tables, reverse=True):
                v = self.tables[bits].get(h[: bits // 4])
                if v:
                    return v
        return ""


def download(dest: str, timeout: int = 60) -> bool:
    for kind, url in SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "deco-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read().decode("utf-8", errors="replace")
            probe = OuiDB()
            if probe.load_text(data) < 10_000:
                raise ValueError("list looked too small")
            tmp = dest + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, dest)
            log.info("Downloaded manufacturer list from %s", url)
            return True
        except Exception as e:
            log.warning("Could not download %s: %s", url, e)
    return False


def start(db: OuiDB, data_dir: str, on_loaded=None):
    """Load the freshest list available, then refresh in the background when stale."""
    local = os.path.join(data_dir, "manuf.txt")
    n = db.load_file(local) or db.load_file(BUNDLED)
    log.info("Manufacturer list: %d prefixes (%s)", n, db.source or "none")

    def refresher():
        while True:
            try:
                age = time.time() - os.path.getmtime(local) if os.path.exists(local) else 1e12
                if age > REFRESH_DAYS * 86400 and download(local):
                    db.load_file(local)
                    if on_loaded:
                        on_loaded()
            except Exception:
                log.exception("manufacturer refresh failed")
            time.sleep(86400)

    threading.Thread(target=refresher, daemon=True).start()


if __name__ == "__main__":  # used at image build time
    import sys
    logging.basicConfig(level=logging.INFO)
    ok = download(sys.argv[1] if len(sys.argv) > 1 else BUNDLED)
    sys.exit(0)  # never fail the build over this; the app retries later
