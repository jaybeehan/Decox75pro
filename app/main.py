"""Deco dashboard: background poller + SQLite history + small web UI."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from functools import wraps

from flask import Flask, Response, jsonify, request, send_from_directory

import oui
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
vendors = oui.OuiDB()
PRESENCE_BUCKET = 300  # seconds per presence slot (5 min)
USER_EVENT_KINDS = ("new", "online", "offline")  # shown on the Activity tab; the rest go to Beta


def local_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))

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
        CREATE TABLE IF NOT EXISTS usage (
            day TEXT, mac TEXT, down INTEGER DEFAULT 0, up INTEGER DEFAULT 0, PRIMARY KEY (day, mac)
        );
        CREATE TABLE IF NOT EXISTS presence (bucket INTEGER, mac TEXT, PRIMARY KEY (bucket, mac));
        """)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(devices)")}
        if "watch" not in cols:
            con.execute("ALTER TABLE devices ADD COLUMN watch INTEGER DEFAULT 0")


# ---- poller --------------------------------------------------------------
state = {"snapshot": None, "last_ok": None, "missing": {}}
_poll_now = threading.Event()


def _send(text, title, tags, priority):
    try:
        import requests
        server, _, topic = NTFY_URL.rstrip("/").rpartition("/")
        r = requests.post(server, timeout=10, json={
            "topic": topic, "title": title, "message": text, "tags": [tags], "priority": priority})
        r.raise_for_status()
        return None
    except Exception as e:
        log.warning("ntfy failed: %s", e)
        return str(e)


def notify(text: str, title: str = "Deco: new device", tags: str = "warning", priority: int = 3):
    """Push a phone notification via ntfy (in the background, so polling never waits on it)."""
    if NTFY_URL:
        threading.Thread(target=_send, args=(text, title, tags, priority), daemon=True).start()


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
                    if row["watch"]:
                        who = row["nickname"] or c.name
                        notify(f"{who} is back on Wi-Fi ({c.band}).", f"{who} connected", "green_circle")
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
                row = con.execute("SELECT name, nickname, watch FROM devices WHERE mac=?", (mac,)).fetchone()
                if row and row["watch"]:
                    who = row["nickname"] or row["name"] or mac
                    notify(f"{who} has left the Wi-Fi.", f"{who} disconnected", "red_circle")
            if state["missing"][mac] > 10_000:
                state["missing"].pop(mac)

        down = sum(c.down_bps or 0 for c in snap.clients)
        up = sum(c.up_bps or 0 for c in snap.clients)
        con.execute("INSERT INTO samples VALUES (?,?,?,?)", (now, len(present), down, up))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('baseline','1')")

        record_beta(con, snap, prev, present, now)

        con.execute("DELETE FROM samples WHERE ts < ?", (now - 30 * 86400,))
        con.execute("DELETE FROM events WHERE ts < ?", (now - 180 * 86400,))
        con.execute("DELETE FROM presence WHERE bucket < ?", (int((now - 8 * 86400) // PRESENCE_BUCKET),))
        con.execute("DELETE FROM usage WHERE day < ?", (local_day(now - 90 * 86400),))


def _node_names(snap):
    return {n.mac: n.name for n in snap.nodes}


def record_beta(con, snap, prev, present, now):
    """Extra history for the Beta tab: data usage, presence, roaming, network changes."""
    # Estimated data per device: current speed x time since the last reading.
    last = state.get("last_ok")
    dt = min(now - last, 3 * POLL_SECONDS) if last else 0
    if dt > 0:
        day = local_day(now)
        for mac, c in present.items():
            d, u = int((c.down_bps or 0) * dt), int((c.up_bps or 0) * dt)
            if d or u:
                con.execute("""INSERT INTO usage (day,mac,down,up) VALUES (?,?,?,?)
                               ON CONFLICT(day,mac) DO UPDATE SET down=down+excluded.down, up=up+excluded.up""",
                            (day, mac, d, u))

    bucket = int(now // PRESENCE_BUCKET)
    con.executemany("INSERT OR IGNORE INTO presence VALUES (?,?)", [(bucket, m) for m in present])

    if not (prev and prev.ok):
        return
    names = _node_names(snap) | _node_names(prev)
    before = {c.mac: c for c in prev.clients}
    for mac, c in present.items():
        p = before.get(mac)
        if not p:
            continue
        moved = p.node_mac and c.node_mac and p.node_mac != c.node_mac
        if moved or (p.band != c.band):
            frm = f"{names.get(p.node_mac, '?')} · {p.band}"
            to = f"{names.get(c.node_mac, '?')} · {c.band}"
            con.execute("INSERT INTO events (ts,mac,kind,detail) VALUES (?,?,?,?)",
                        (now, mac, "roam" if moved else "band", f"{frm} → {to}"))

    old_ip, new_ip = (prev.wan or {}).get("ip"), (snap.wan or {}).get("ip")
    if old_ip and new_ip and old_ip != new_ip:
        con.execute("INSERT INTO events (ts,mac,kind,detail) VALUES (?,?,?,?)",
                    (now, "", "wan_ip", f"Public IP changed: {old_ip} → {new_ip}"))
        notify(f"{old_ip} → {new_ip}", "Public IP changed", "globe_with_meridians", 2)
    main_now = next((n.internet for n in snap.nodes if n.role == "master"), "")
    main_before = next((n.internet for n in prev.nodes if n.role == "master"), "")
    if main_now and main_before and main_now != main_before:
        con.execute("INSERT INTO events (ts,mac,kind,detail) VALUES (?,?,?,?)",
                    (now, "", "internet", f"Internet status: {main_before} → {main_now}"))
        notify(f"Main Deco reports internet: {main_now}", "Internet status changed", "satellite", 4)
    for n in snap.nodes:
        p = next((x for x in prev.nodes if x.mac == n.mac), None)
        if p and p.online != n.online:
            con.execute("INSERT INTO events (ts,mac,kind,detail) VALUES (?,?,?,?)",
                        (now, n.mac, "node", f"{n.name} {'back online' if n.online else 'dropped off the mesh'}"))
            notify(f"{n.name} {'is back on the mesh' if n.online else 'dropped off the mesh'}.",
                   f"Deco {n.name} {'back' if n.online else 'offline'}", "house", 3 if n.online else 4)
        if p and p.backhaul and n.backhaul and p.backhaul != n.backhaul:
            con.execute("INSERT INTO events (ts,mac,kind,detail) VALUES (?,?,?,?)",
                        (now, n.mac, "node", f"{n.name} mesh link: {p.backhaul} → {n.backhaul}"))


def log_reachability(ok: bool, error: str = ""):
    """Note in the network log when the dashboard loses / regains contact with the Deco."""
    if ok:
        if state.get("alerted_down"):
            notify("The dashboard can read the Deco again.", "Deco reachable again", "white_check_mark", 2)
        state["fail_count"], state["alerted_down"] = 0, False
    else:
        state["fail_count"] = state.get("fail_count", 0) + 1
        if state["fail_count"] == 3:
            state["alerted_down"] = True
            notify(f"Couldn't reach the Deco for 3 readings in a row. The mesh may be down or rebooting.\n{error[:150]}",
                   "Deco unreachable", "rotating_light", 4)
    was_down = state.get("deco_down", False)
    if ok == (not was_down):
        return
    state["deco_down"] = not ok
    try:
        with _db_lock, db() as con:
            con.execute("INSERT INTO events (ts,mac,kind,detail) VALUES (?,?,?,?)",
                        (time.time(), "", "deco",
                         "Dashboard reached the Deco again" if ok else f"Couldn't reach the Deco: {error[:120]}"))
    except Exception:
        log.exception("could not log reachability")


def poll_once():
    snap = source.snapshot()
    log_reachability(snap.ok, snap.error)
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
    resp = send_from_directory(app.static_folder, "index.html")
    resp.headers["Cache-Control"] = "no-cache"  # always pick up a new version after an update
    return resp


@app.get("/api/state")
@auth
def api_state():
    snap = state["snapshot"]
    with _db_lock, db() as con:
        devs = {r["mac"]: dict(r) for r in con.execute("SELECT * FROM devices")}
        events = [dict(r) for r in con.execute(
            "SELECT e.*, d.name, d.nickname FROM events e LEFT JOIN devices d ON d.mac=e.mac "
            f"WHERE e.kind IN ({','.join('?' * len(USER_EVENT_KINDS))}) ORDER BY ts DESC LIMIT 100",
            USER_EVENT_KINDS)]
        since = time.time() - 24 * 3600
        samples = [dict(r) for r in con.execute(
            "SELECT * FROM samples WHERE ts > ? ORDER BY ts", (since,))]
    online = []
    if snap and snap.ok:
        for c in snap.clients:
            d = devs.get(c.mac, {})
            online.append({**c.__dict__, "nickname": d.get("nickname", ""), "known": d.get("known", 1),
                           "first_seen": d.get("first_seen"), "notes": d.get("notes", ""),
                           "watch": d.get("watch", 0),
                           "vendor": vendors.lookup(c.mac)})
    online_macs = {c["mac"] for c in online}
    offline = sorted(({**d, "vendor": vendors.lookup(m)} for m, d in devs.items() if m not in online_macs),
                     key=lambda d: -(d["last_seen"] or 0))
    return jsonify({
        "demo": DEMO, "poll_seconds": POLL_SECONDS,
        "ok": bool(snap and snap.ok), "error": state.get("error", ""),
        "error_at": state.get("error_at"), "last_ok": state.get("last_ok"),
        "nodes": [n.__dict__ for n in snap.nodes] if snap and snap.ok else [],
        "wan": snap.wan if snap and snap.ok else {}, "wifi": snap.wifi if snap and snap.ok else {},
        "radios": getattr(snap, "radios", []) if snap and snap.ok else [],
        "perf": snap.perf if snap and snap.ok else {},
        "online": online, "offline": offline, "events": events, "samples": samples,
        "unknown_count": sum(1 for c in online if not c["known"]),
        "ntfy": bool(NTFY_URL),
    })


@app.get("/api/beta")
@auth
def api_beta():
    now = time.time()
    today, week_ago = local_day(now), local_day(now - 6 * 86400)
    first_bucket = int((now - 24 * 3600) // PRESENCE_BUCKET) + 1
    with _db_lock, db() as con:
        devs = {r["mac"]: dict(r) for r in con.execute("SELECT mac, name, nickname, known FROM devices")}
        top = [dict(r) for r in con.execute(
            "SELECT mac, down, up FROM usage WHERE day=? ORDER BY down+up DESC LIMIT 15", (today,))]
        days = [dict(r) for r in con.execute(
            "SELECT day, SUM(down) AS down, SUM(up) AS up FROM usage WHERE day>=? GROUP BY day ORDER BY day",
            (week_ago,))]
        pres = {}
        for r in con.execute("SELECT bucket, mac FROM presence WHERE bucket>=?", (first_bucket,)):
            pres.setdefault(r["mac"], []).append(r["bucket"] - first_bucket)
        log_rows = [dict(r) for r in con.execute(
            "SELECT e.ts, e.mac, e.kind, e.detail, d.name, d.nickname FROM events e "
            "LEFT JOIN devices d ON d.mac=e.mac "
            f"WHERE e.kind NOT IN ({','.join('?' * len(USER_EVENT_KINDS))}) ORDER BY ts DESC LIMIT 150",
            USER_EVENT_KINDS)]

    def label(mac):
        d = devs.get(mac, {})
        return d.get("nickname") or d.get("name") or mac

    snap = state["snapshot"]
    makers = {}
    if snap and snap.ok:
        for c in snap.clients:
            v = vendors.lookup(c.mac) or "Unknown"
            makers[v] = makers.get(v, 0) + 1
    presence = sorted(({"mac": m, "label": label(m), "known": devs.get(m, {}).get("known", 1),
                        "slots": sorted(s)} for m, s in pres.items()),
                      key=lambda x: x["label"].lower())
    return jsonify({
        "today": today,
        "top": [{**t, "label": label(t["mac"]), "vendor": vendors.lookup(t["mac"])} for t in top],
        "days": days,
        "presence": presence, "slots": 24 * 3600 // PRESENCE_BUCKET, "slot_seconds": PRESENCE_BUCKET,
        "first_slot_ts": first_bucket * PRESENCE_BUCKET,
        "roams": [{**r, "label": label(r["mac"])} for r in log_rows if r["kind"] in ("roam", "band")][:60],
        "network": [r for r in log_rows if r["kind"] in ("wan_ip", "internet", "node", "deco")][:60],
        "makers": sorted(makers.items(), key=lambda kv: -kv[1]),
        "vendor_db": bool(vendors.tables),
    })


@app.post("/api/device/<mac>")
@auth
def api_device(mac):
    body = request.get_json(force=True) or {}
    fields = {k: body[k] for k in ("nickname", "notes", "known", "watch") if k in body}
    for k in ("known", "watch"):
        if k in fields:
            fields[k] = 1 if fields[k] else 0
    if not fields:
        return jsonify({"ok": False, "error": "nothing to change"}), 400
    with _db_lock, db() as con:
        con.execute(f"UPDATE devices SET {', '.join(k + '=?' for k in fields)} WHERE mac=?",
                    (*fields.values(), mac.upper()))
    return jsonify({"ok": True})


@app.post("/api/notify-test")
@auth
def api_notify_test():
    if not NTFY_URL:
        return jsonify({"ok": False, "error": "NTFY_URL isn't set in the compose file yet"}), 400
    err = _send("If you can read this, alerts from your Deco dashboard will reach this phone.",
                "Deco dashboard test", "tada", 3)
    if err:
        return jsonify({"ok": False, "error": err}), 502
    return jsonify({"ok": True})


@app.get("/api/export.csv")
@auth
def api_export():
    import csv
    import io
    with _db_lock, db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM devices ORDER BY last_seen DESC")]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "Nickname", "MAC", "Maker (guess)", "Last IP", "Band", "Network", "Recognised",
                "Notify", "First seen", "Last seen", "Notes"])
    fmt = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""
    for r in rows:
        w.writerow([r["name"], r["nickname"], r["mac"], vendors.lookup(r["mac"]), r["last_ip"], r["last_band"],
                    r["last_network"], "yes" if r["known"] else "no", "yes" if r.get("watch") else "no",
                    fmt(r["first_seen"]), fmt(r["last_seen"]), r["notes"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=deco-devices-{local_day(time.time())}.csv"})


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


@app.get("/api/discover")
@auth
def api_discover():
    try:
        return jsonify(source.discover())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    oui.start(vendors, DATA_DIR)
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
