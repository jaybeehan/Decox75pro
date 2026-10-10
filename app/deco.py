"""Talks to the Deco mesh over its local API.

Uses the open-source tplinkrouterc6u library for login/encryption, then calls the
Deco's own endpoints directly so we can see per-station (node) details and which
station each client is attached to.

Only one local login is allowed on the Deco at a time, so every operation logs in,
does its work, and logs out again, all under a lock.
"""
from __future__ import annotations

import base64
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field, asdict

log = logging.getLogger("deco")


def _b64(value):
    """Deco returns names base64-encoded; fall back to the raw value if not."""
    if not value:
        return ""
    try:
        return base64.b64decode(value).decode("utf-8")
    except Exception:
        return str(value)


BAND_NAMES = {"band2_4": "2.4 GHz", "band5": "5 GHz", "band5_1": "5 GHz", "band6": "6 GHz"}


@dataclass
class Node:
    mac: str
    name: str
    model: str
    role: str  # "master" (main) or "slave" (satellite)
    ip: str = ""
    firmware: str = ""
    hardware: str = ""
    online: bool = True
    internet: str = ""
    backhaul: str = ""  # e.g. "wired", "5 GHz"
    signal: dict = field(default_factory=dict)


@dataclass
class Client:
    mac: str
    name: str
    ip: str
    band: str  # "wired", "2.4 GHz", ...
    network: str  # "main", "guest", "iot"
    node_mac: str = ""
    down_bps: int | None = None  # bytes per second
    up_bps: int | None = None


@dataclass
class Snapshot:
    ok: bool
    error: str = ""
    taken_at: float = 0.0
    nodes: list = field(default_factory=list)
    clients: list = field(default_factory=list)
    wan: dict = field(default_factory=dict)
    wifi: dict = field(default_factory=dict)  # {"main_2.4 GHz": True, ...}
    radios: list = field(default_factory=list)  # per band: channel, width, mode, backhaul channel
    perf: dict = field(default_factory=dict)  # cpu/mem of main unit

    def to_dict(self):
        d = asdict(self)
        return d


class DecoSource:
    def __init__(self, host: str, password: str, verify_ssl: bool = False, timeout: int = 20):
        self.host = host
        self.password = password
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.lock = threading.Lock()

    # -- session helpers -------------------------------------------------
    def _client(self):
        from tplinkrouterc6u import TPLinkDecoClient  # imported lazily so demo mode needs no deps
        return TPLinkDecoClient(self.host, self.password, verify_ssl=self.verify_ssl,
                                timeout=self.timeout)

    def _session(self, fn):
        with self.lock:
            c = self._client()
            c.authorize()
            try:
                return fn(c)
            finally:
                try:
                    c.logout()
                except Exception:
                    pass

    @staticmethod
    def _req(c, path, params=None, ignore_errors=False):
        body = {"operation": "read"} if params is None else params
        return c.request(path, json.dumps(body), ignore_errors=ignore_errors) or {}

    # -- reading ---------------------------------------------------------
    def snapshot(self) -> Snapshot:
        try:
            return self._session(self._read_all)
        except Exception as e:  # network down, wrong password, etc.
            log.warning("Deco read failed: %s", e)
            return Snapshot(ok=False, error=str(e), taken_at=time.time())

    def raw(self) -> dict:
        """Raw endpoint responses, for troubleshooting field names."""
        def go(c):
            out = {}
            for path in ("admin/device?form=device_list", "admin/network?form=wan_ipv4",
                         "admin/network?form=performance", "admin/wireless?form=wlan"):
                try:
                    out[path] = self._req(c, path)
                except Exception as e:
                    out[path] = {"error": str(e)}
            try:
                out["client_list"] = self._req(c, "admin/client?form=client_list",
                                               {"operation": "read", "params": {"device_mac": "default"}})
            except Exception as e:
                out["client_list"] = {"error": str(e)}
            # never expose Wi-Fi passwords
            return json.loads(json.dumps(out).replace('"password"', '"password_hidden"'))
        return self._session(go)

    # Words that mark a command we must never send, even as a "read".
    UNSAFE = ("reboot", "reset", "factory", "upgrade", "firmware", "restore", "backup", "logout", "login",
              "remove", "delete", "del_", "add_", "write", "set_", "bind", "unbind", "format", "update", "auth")

    @staticmethod
    def _scrub(obj):
        """Hide anything that looks like a password or key before showing it."""
        if isinstance(obj, dict):
            return {k: ("***" if re.search(r"pass|psk|key|token|secret|pwd", k, re.I) else DecoSource._scrub(v))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [DecoSource._scrub(x) for x in obj[:3]]
        return obj

    def _fetch_web_ui(self):
        """Download the Deco's own web-page scripts and pull out every local command they use."""
        import requests
        base = self.host.rstrip("/")
        seen_files, queue, found, notes = set(), ["/", "/webpages/index.html"], set(), []
        total = 0
        while queue and len(seen_files) < 80 and total < 25_000_000:
            path = queue.pop(0)
            if path in seen_files:
                continue
            seen_files.add(path)
            try:
                r = requests.get(base + path, timeout=15, verify=self.verify_ssl)
            except Exception as e:
                notes.append(f"{path}: {str(e)[:80]}")
                continue
            if r.status_code != 200:
                continue
            text = r.text
            total += len(text)
            for m in re.finditer(r"""(?:src|href)\s*=\s*["']([^"'#?]+\.(?:js|html))""", text):
                ref = m.group(1)
                if ref.startswith("http"):
                    continue
                if not ref.startswith("/"):
                    ref = path.rsplit("/", 1)[0] + "/" + ref
                queue.append(re.sub(r"/\./", "/", ref))
            for m in re.finditer(r"""["'/](?:admin|domain_login|login)/([a-z0-9_]+)\?form=([a-z0-9_]+)""", text):
                found.add(f"admin/{m.group(1)}?form={m.group(2)}")
            # scripts loaded by name, e.g. "js/app.js" inside other scripts
            for m in re.finditer(r"""["']((?:\.{0,2}/)?[a-zA-Z0-9_\-/]+\.js)["']""", text):
                ref = m.group(1)
                if not ref.startswith("/"):
                    ref = "/webpages/" + ref.lstrip("./")
                queue.append(ref)
        return sorted(found), sorted(seen_files), notes

    def discover(self) -> dict:
        """Find the commands the Deco's web page uses, then try each one as a read. Changes nothing."""
        try:
            endpoints, files, notes = self._fetch_web_ui()
        except Exception as e:
            return {"error": f"could not read the Deco web page: {e}"}
        safe = [e for e in endpoints if not any(w in e.lower() for w in self.UNSAFE)]
        skipped = [e for e in endpoints if e not in safe]

        def go(c):
            answered, refused = {}, []
            for path in safe:
                try:
                    r = c.request(path, json.dumps({"operation": "read"}), ignore_errors=True)
                    bad = isinstance(r, dict) and ((r.get("error_code") not in (None, 0)) or r.get("success") is False)
                    if bad or r is None:
                        refused.append(path)
                    else:
                        answered[path] = json.dumps(self._scrub(r))[:300]
                except Exception:
                    refused.append(path)
                    try:
                        c.authorize()
                    except Exception:
                        pass
            return answered, refused

        answered, refused = self._session(go) if safe else ({}, [])
        return {"files_read": len(files), "found": len(endpoints), "answered": answered,
                "refused": refused, "skipped_unsafe": skipped, "notes": notes[:10]}

    def _read_all(self, c) -> Snapshot:
        snap = Snapshot(ok=True, taken_at=time.time())

        devices = self._req(c, "admin/device?form=device_list").get("device_list", [])
        for d in devices:
            sig = d.get("signal_level") or {}
            conn = d.get("connection_type") or []
            if d.get("role") == "master":
                backhaul = "main unit"
            elif isinstance(conn, list) and conn:
                backhaul = ", ".join("wired" if x == "wired" else BAND_NAMES.get(x, x) for x in conn)
            else:
                backhaul = str(conn or "")
            snap.nodes.append(Node(
                mac=(d.get("mac") or "").upper().replace("-", ":"),
                name=_b64(d.get("custom_nickname")) or _b64(d.get("nickname")) or d.get("device_model", "Deco"),
                model=d.get("device_model", ""),
                role=d.get("role", ""),
                ip=d.get("device_ip", ""),
                firmware=d.get("software_ver", ""),
                hardware=d.get("hardware_ver", ""),
                online=d.get("group_status", "connected") != "disconnected",
                internet=d.get("inet_status", ""),
                backhaul=backhaul,
                signal={BAND_NAMES.get(k, k): v for k, v in sig.items()} if isinstance(sig, dict) else {},
            ))

        # Clients: ask each station for its own list so we know who is on which Deco.
        seen = {}
        for node in snap.nodes:
            try:
                lst = self._req(c, "admin/client?form=client_list",
                                {"operation": "read", "params": {"device_mac": node.mac.replace(":", "-")}}
                                ).get("client_list", [])
            except Exception:
                lst = []
            for item in lst:
                cl = self._client_from(item, node.mac)
                if cl and cl.mac not in seen:
                    seen[cl.mac] = cl
        # Then the full list, to catch anything the per-node query missed.
        try:
            lst = self._req(c, "admin/client?form=client_list",
                            {"operation": "read", "params": {"device_mac": "default"}}).get("client_list", [])
        except Exception:
            lst = []
        for item in lst:
            cl = self._client_from(item, "")
            if cl and cl.mac not in seen:
                seen[cl.mac] = cl
        snap.clients = list(seen.values())

        try:
            wan = self._req(c, "admin/network?form=wan_ipv4")
            ip_info = (wan.get("wan") or {}).get("ip_info") or {}
            snap.wan = {
                "ip": ip_info.get("ip", ""),
                "gateway": ip_info.get("gateway", ""),
                "dns": ", ".join(x for x in (ip_info.get("dns1"), ip_info.get("dns2")) if x and x != "0.0.0.0"),
                "type": (wan.get("wan") or {}).get("dial_type", ""),
                "lan_ip": ((wan.get("lan") or {}).get("ip_info") or {}).get("ip", ""),
            }
        except Exception as e:
            snap.wan = {"error": str(e)}

        try:
            perf = self._req(c, "admin/network?form=performance")
            snap.perf = {"cpu": perf.get("cpu_usage"), "mem": perf.get("mem_usage")}
        except Exception:
            pass

        try:
            wl = self._req(c, "admin/wireless?form=wlan")
            for key, label in (("band2_4", "2.4 GHz"), ("band5_1", "5 GHz"), ("band6", "6 GHz")):
                band = wl.get(key)
                if not isinstance(band, dict):
                    continue
                for net in ("host", "guest"):
                    if isinstance(band.get(net), dict) and "enable" in band[net]:
                        snap.wifi[f"{'main' if net == 'host' else 'guest'}|{label}"] = bool(band[net]["enable"])
            labels = {"band2_4": "2.4 GHz", "band5_1": "5 GHz", "band5_2": "5 GHz (2)", "band5": "5 GHz", "band6": "6 GHz"}
            for key in sorted(k for k in wl if k.startswith("band")):
                band = wl.get(key)
                if not isinstance(band, dict):
                    continue
                host = band.get("host") or {}
                bh = band.get("backhaul") or {}
                width = str(host.get("channel_width", "") or "")
                m = re.search(r"(\d+)", width)
                snap.radios.append({
                    "band": labels.get(key, key),
                    "channel": host.get("channel"),
                    "width": f"{m.group(1)} MHz" if m else (width or ""),
                    "mode": host.get("mode", ""),
                    "backhaul_channel": bh.get("channel"),
                })
        except Exception:
            pass
        return snap

    @staticmethod
    def _client_from(item, node_mac) -> Client | None:
        if not item.get("online", True):
            return None
        mac = (item.get("mac") or "").upper().replace("-", ":")
        if not mac:
            return None
        if item.get("wire_type") == "wired":
            band = "wired"
        else:
            band = BAND_NAMES.get(item.get("connection_type"), item.get("connection_type") or "wifi")
        net = item.get("interface") or "main"
        return Client(
            mac=mac,
            name=_b64(item.get("name")) or mac,
            ip=item.get("ip", ""),
            band=band,
            network=net,
            node_mac=node_mac or (item.get("access_host") or "").upper().replace("-", ":"),
            down_bps=item.get("down_speed"),
            up_bps=item.get("up_speed"),
        )

    # -- actions ---------------------------------------------------------
    def set_wifi(self, network: str, band: str, enable: bool):
        from tplinkrouterc6u import Connection
        table = {
            ("main", "2.4 GHz"): Connection.HOST_2G, ("main", "5 GHz"): Connection.HOST_5G,
            ("main", "6 GHz"): Connection.HOST_6G, ("guest", "2.4 GHz"): Connection.GUEST_2G,
            ("guest", "5 GHz"): Connection.GUEST_5G, ("guest", "6 GHz"): Connection.GUEST_6G,
        }
        conn = table[(network, band)]
        self._session(lambda c: c.set_wifi(conn, enable))

    def reboot(self):
        self._session(lambda c: c.reboot())


class DemoSource:
    """Fake mesh for trying the UI without a Deco (DEMO=1)."""

    def __init__(self):
        self.wifi = {"main|2.4 GHz": True, "main|5 GHz": True, "main|6 GHz": True,
                     "guest|2.4 GHz": False, "guest|5 GHz": False, "guest|6 GHz": False}
        self.tick = 0

    def snapshot(self) -> Snapshot:
        self.tick += 1
        nodes = [
            Node("A8-42-A1-00-00-01".replace("-", ":"), "Living Room", "XE75 Pro", "master",
                 "192.168.68.1", "1.3.1 Build 20251023", "3.0", True, "online", "main unit"),
            Node("A8:42:A1:00:00:02", "Basement", "XE75 Pro", "slave", "192.168.68.50",
                 "1.3.1 Build 20251023", "3.0", True, "online", "6 GHz",
                 {"2.4 GHz": 3, "5 GHz": 3, "6 GHz": 2}),
        ]
        base = [
            ("3C:22:FB:11:22:33", "Jason's Find X9 Pro", "192.168.68.101",
             "5 GHz" if self.tick % 3 == 0 else "6 GHz", "main", 1 if self.tick % 3 == 0 else 0),
            ("F0:18:98:44:55:66", "iPad", "192.168.68.102", "5 GHz", "main", 0),
            ("00:11:32:AA:BB:CC", "Synology NAS", "192.168.68.10", "wired", "main", 0),
            ("B8:27:EB:12:34:56", "Living Room TV", "192.168.68.103", "5 GHz", "main", 0),
            ("D8:F1:5B:01:02:03", "Smart Plug Kitchen", "192.168.68.120", "2.4 GHz", "main", 1),
            ("D8:F1:5B:04:05:06", "Smart Plug Garage", "192.168.68.121", "2.4 GHz", "iot", 1),
            ("44:65:0D:77:88:99", "Echo Dot", "192.168.68.122", "2.4 GHz", "main", 1),
            ("A4:83:E7:10:20:30", "MacBook", "192.168.68.104", "5 GHz", "main", 1),
        ]
        if self.tick % 4 == 0:
            base.append(("7A:1C:00:DE:AD:01", "Unknown device", "192.168.68.150", "2.4 GHz", "guest", 1))
        clients = [Client(m, n, ip, b, net, nodes[ni].mac,
                          random.randint(0, 900_000) if b != "wired" else random.randint(0, 5_000_000),
                          random.randint(0, 200_000))
                   for m, n, ip, b, net, ni in base]
        return Snapshot(ok=True, taken_at=time.time(), nodes=nodes, clients=clients,
                        wan={"ip": "203.0.113.45", "gateway": "203.0.113.1", "dns": "1.1.1.1, 8.8.8.8",
                             "type": "dynamic_ip", "lan_ip": "192.168.68.1"},
                        wifi=dict(self.wifi),
                        radios=[{"band": "2.4 GHz", "channel": 6, "width": "20 MHz", "mode": "11ax", "backhaul_channel": None},
                                {"band": "5 GHz", "channel": 44, "width": "80 MHz", "mode": "11ax", "backhaul_channel": None},
                                {"band": "6 GHz", "channel": 37, "width": "160 MHz", "mode": "11ax", "backhaul_channel": 37}],
                        perf={"cpu": round(random.uniform(0.05, 0.3), 2), "mem": 0.41})

    def raw(self):
        return {"demo": True}

    def discover(self):
        return {"files_read": 12, "found": 4,
                "answered": {"admin/network?form=performance": '{"cpu_usage": 0.1, "mem_usage": 0.4}',
                             "admin/device?form=device_list": '{"device_list": [{"mac": "..."}]}'},
                "refused": ["admin/foo?form=bar"], "skipped_unsafe": ["admin/system?form=reboot"], "notes": []}

    def set_wifi(self, network, band, enable):
        self.wifi[f"{network}|{band}"] = enable

    def reboot(self):
        pass
