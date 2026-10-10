"""Deco dashboard: background poller + SQLite history + small web UI."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from functools import wraps

from flask import Flask, Response, jsonify, request, send_from_directory

from deco import DecoSource, DemoSource

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dashboard")

# ---- configuration (all from environment variables) ----------------------
DEMO = os.environ.get("DEMO", "0") == "1"
DECO_HOST = os.environ.get("DECO_HOST", "http://192.168.68.1")
DECO_PASSWORD = os.environ.get("DECO_PASSWORD", "")
POLL_SECONDS = max(20, int(os.environ.get("POLL_SECONDS", "60")))
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")
NTFY_URL = os.environ.get("NTFY_URL", "")  # e.g. https://ntfy.sh/my-secret-topic
DATA_DIR = os.environ.get("DATA_DIR", "/data")
OFFLINE_AFTER = int(os.environ.get("OFFLINE_AFTER_POLLS", "3"))  # missed polls before "went offline"

os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "deco.db")

source = DemoSource() if DEMO else DecoSource(DECO_HOST, DECO_PASSWORD)

# ---- database ------------------------------------------------------------
_db_lock = threading.Lock()


def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _db_lock, db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            mac TEXT PRIMARY KEY, name TEXT, nickname TEXT DEFAULT '', notes TEXT DEFAULT '',
            known INTEGER DEFAULT 0, first_seen REAL, last_seen REAL,
            last_ip TEXT, last_band TEXT, last_network TEXT, last_node TEXT,
            online_polls INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mac TEXT, kind TEXT, detail TEXT
        );
        CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
        CREATE TABLE IF NOT EXISTS samples (ts REAL, clients INTEGER, down INTEGER, up INTEGER);
        CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        """)


# ---- poller --------------------------------------------------------------
state = {"snapshot": None, "last_ok": None, "missing": {}}
_poll_now = threading.Event()


def notify(text: str):
    if not NTFY_URL:
        return
    try:
        import requests
        requests.post(NTFY_URL, data=text.encode("utf-8"),
                      headers={"Title": "Deco: new device", "Tags": "warning"}, timeout=10)
    except Exception as e:
        log.warning("ntfy failed: %s", e)


def record(snap):
    now = snap.taken_at
    present = {c.mac: c for c in snap.clients}
    with _db_lock, db() as con:
        baseline_done = con.execute("SELECT v FROM meta WHERE k='baseline'").fetchone()
        for mac, c in present.items():
            row = con.execute("SELECT * FROM devices WHERE mac=?", (mac,)).fetchone()
            if row is None:
                known = 0 if baseline_done else 1  # first ever poll = your existing devices
                con.execute("""INSERT INTO devices (mac,name,known,first_seen,last_seen,last_ip,last_band,
                               last_network,last_node,online_polls) VALUES (?,?,?,?,?,?,?,?,?,1)""",
                            (mac, c.name, known, now, now, c.ip, c.band, c.network, c.node_mac))
                if baseline_done:
                    con.execute("INSERT INTO events (ts,mac,kind,detail) VALUES (?,?,?,?)",
                                (now, mac, "new", f"{c.name} ({c.ip}, {c.band}, {c.network} network)"))
                    notify(f"New device joined: {c.name}\n{mac} · {c.ip} · {c.band} · {c.network} network")
            else:
                was_offline = mac in state["missing"] and state["missing"][mac] >= OFFLINE_AFTER
                con.execute("""UPDATE devices SET name=?, last_seen=?, last_ip=?, last_band=?, last_network=?,
                               last_node=?, online_polls=online_polls+1 WHERE mac=?""",
                            (c.name, now, c.ip, c.band, c.network, c.node_mac, mac))
                if was_offline:
                    con.execute("INSERT INTO events (ts,mac,kind,detail) VALUES (?,?,?,?)",
                                (now, mac, "online", f"{c.ip}, {c.band}"))
            state["missing"].pop(mac, None)

        # devices that dropped out of the list
        prev = state["snapshot"]
        if prev and prev.ok:
            for c in prev.clients:
                if c.mac not in present and c.mac not in state["missing"]:
                    state["missing"][c.mac] = 0
        for mac in list(state["missing"]):
            if mac in present:
                continue
            state["missing"][mac] += 1
            if state["missing"][mac] == OFFLINE_AFTER:
                con.execute("INSERT INTO events (ts,mac,kind,detail) VALUES (?,?,?,?)",
                            (now, mac, "offline", ""))
            if state["missing"][mac] > 10_000:
                state["missing"].pop(mac)

        down = sum(c.down_bps or 0 for c in snap.clients)
        up = sum(c.up_bps or 0 for c in snap.clients)
        con.execute("INSERT INTO samples VALUES (?,?,?,?)", (now, len(present), down, up))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('baseline','1')")
        con.execute("DELETE FROM samples WHERE ts < ?", (now - 30 * 86400,))
        con.execute("DELETE FROM events WHERE ts < ?", (now - 180 * 86400,))


def poll_once():
    snap = source.snapshot()
    if snap.ok:
        try:
            record(snap)
        except Exception:
            log.exception("could not record snapshot")
        state["last_ok"] = snap.taken_at
        state["snapshot"] = snap
    else:
        # keep the last good data on screen, but surface the error
        state["error"] = snap.error
        state["error_at"] = snap.taken_at
        if state["snapshot"] is None:
            state["snapshot"] = snap
        return
    state["error"] = ""


def poller():
    while True:
        try:
            poll_once()
        except Exception:
            log.exception("poll crashed")
        _poll_now.wait(POLL_SECONDS)
        _poll_now.clear()


# ---- web -----------------------------------------------------------------
app = Flask(__name__, static_folder="static")


def auth(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if DASH_PASSWORD:
            a_ = request.authorization
            if not a_ or a_.password != DASH_PASSWORD:
                return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Deco dashboard"'})
        return fn(*a, **kw)
    return wrapper


@app.get("/")
@auth
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/state")
@auth
def api_state():
    snap = state["snapshot"]
    with _db_lock, db() as con:
        devs = {r["mac"]: dict(r) for r in con.execute("SELECT * FROM devices")}
        events = [dict(r) for r in con.execute(
            "SELECT e.*, d.name, d.nickname FROM events e LEFT JOIN devices d ON d.mac=e.mac "
            "ORDER BY ts DESC LIMIT 100")]
        since = time.time() - 24 * 3600
        samples = [dict(r) for r in con.execute(
            "SELECT * FROM samples WHERE ts > ? ORDER BY ts", (since,))]
    online = []
    if snap and snap.ok:
        for c in snap.clients:
            d = devs.get(c.mac, {})
            online.append({**c.__dict__, "nickname": d.get("nickname", ""), "known": d.get("known", 1),
                           "first_seen": d.get("first_seen"), "notes": d.get("notes", "")})
    online_macs = {c["mac"] for c in online}
    offline = sorted((d for m, d in devs.items() if m not in online_macs),
                     key=lambda d: -(d["last_seen"] or 0))
    return jsonify({
        "demo": DEMO, "poll_seconds": POLL_SECONDS,
        "ok": bool(snap and snap.ok), "error": state.get("error", ""),
        "error_at": state.get("error_at"), "last_ok": state.get("last_ok"),
        "nodes": [n.__dict__ for n in snap.nodes] if snap and snap.ok else [],
        "wan": snap.wan if snap and snap.ok else {}, "wifi": snap.wifi if snap and snap.ok else {},
        "perf": snap.perf if snap and snap.ok else {},
        "online": online, "offline": offline, "events": events, "samples": samples,
        "unknown_count": sum(1 for c in online if not c["known"]),
    })


@app.post("/api/device/<mac>")
@auth
def api_device(mac):
    body = request.get_json(force=True) or {}
    fields = {k: body[k] for k in ("nickname", "notes", "known") if k in body}
    if "known" in fields:
        fields["known"] = 1 if fields["known"] else 0
    if not fields:
        return jsonify({"ok": False, "error": "nothing to change"}), 400
    with _db_lock, db() as con:
        con.execute(f"UPDATE devices SET {', '.join(k + '=?' for k in fields)} WHERE mac=?",
                    (*fields.values(), mac.upper()))
    return jsonify({"ok": True})


@app.post("/api/known-all")
@auth
def api_known_all():
    with _db_lock, db() as con:
        con.execute("UPDATE devices SET known=1")
    return jsonify({"ok": True})


@app.post("/api/wifi")
@auth
def api_wifi():
    body = request.get_json(force=True) or {}
    try:
        source.set_wifi(body["network"], body["band"], bool(body["enable"]))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    time.sleep(2)
    _poll_now.set()
    return jsonify({"ok": True})


@app.post("/api/reboot")
@auth
def api_reboot():
    try:
        source.reboot()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.post("/api/refresh")
@auth
def api_refresh():
    _poll_now.set()
    return jsonify({"ok": True})


@app.get("/api/raw")
@auth
def api_raw():
    try:
        return jsonify(source.raw())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/healthz")
def healthz():
    return "ok"


def main():
    if not DEMO and not DECO_PASSWORD:
        log.error("DECO_PASSWORD is not set. Set it in the compose file.")
    init_db()
    threading.Thread(target=poller, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    log.info("Deco dashboard on port %s (demo=%s, host=%s, every %ss)", port, DEMO, DECO_HOST, POLL_SECONDS)
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=4)
    except ImportError:
        app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
