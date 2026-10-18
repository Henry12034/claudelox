"""Dispositivi Shelly in rete locale: ricerca, indirizzo fisso, identificazione.

Usa solo l'API locale HTTP degli Shelly (senza password: questi dispositivi non hanno
l'autenticazione attiva). La password del Wi-Fi non passa mai da qui: la inserisce
l'utente quando collega lo Shelly alla rete.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

NETMASK = "255.255.255.0"


def _get_json(url: str, timeout: float = 3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def identify(ip: str, timeout: float = 2.5) -> dict | None:
    """Dati pubblici di uno Shelly (/shelly) oppure None se all'indirizzo non c'e' uno Shelly."""
    try:
        d = _get_json(f"http://{ip}/shelly", timeout)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not ({"type", "model", "gen"} & d.keys()):
        return None
    gen = int(d.get("gen", 1))
    return {"ip": ip, "gen": gen, "model": d.get("model") or d.get("type"), "app": d.get("app"),
            "mac": (d.get("mac") or "").upper(), "fw": d.get("ver") or d.get("fw"),
            "auth": bool(d.get("auth_en", d.get("auth")))}


def ip_mode(dev: dict) -> str:
    """'static' o 'dhcp' (o '?' se non leggibile)."""
    try:
        if dev["gen"] >= 2:
            cfg = _get_json(f"http://{dev['ip']}/rpc/WiFi.GetConfig")
            return cfg.get("sta", {}).get("ipv4mode", "?")
        return _get_json(f"http://{dev['ip']}/settings/sta").get("ipv4_method", "?")
    except (OSError, ValueError):
        return "?"


def scan(prefix: str = "192.168.1.", workers: int = 64) -> list[dict]:
    def probe(i: int):
        ip = f"{prefix}{i}"
        try:
            with socket.create_connection((ip, 80), timeout=1.2):
                pass
        except OSError:
            return None
        return identify(ip)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        found = [d for d in ex.map(probe, range(1, 255)) if d]
    for d in found:
        d["ipv4"] = ip_mode(d)
    return found


def is_free(ip: str) -> bool:
    try:
        with socket.create_connection((ip, 80), timeout=1.5):
            return False
    except OSError:
        return True


def set_static_ip(dev: dict, new_ip: str, gateway: str, dns: str) -> None:
    """Imposta l'IP fisso senza toccare nome rete e password (gia' configurati dall'utente)."""
    if dev["gen"] >= 2:
        cfg = {"sta": {"ipv4mode": "static", "ip": new_ip, "netmask": NETMASK, "gw": gateway, "nameserver": dns}}
        url = f"http://{dev['ip']}/rpc/WiFi.SetConfig?config={urllib.parse.quote(json.dumps(cfg))}"
    else:
        q = urllib.parse.urlencode({"ipv4_method": "static", "ip": new_ip, "netmask": NETMASK,
                                    "gateway": gateway, "dns": dns})
        url = f"http://{dev['ip']}/settings/sta?{q}"
    try:
        _get_json(url, timeout=5)
    except (OSError, ValueError):
        pass  # lo Shelly si ricollega subito con il nuovo indirizzo: la risposta puo' andare persa


def wait_for(ip: str, mac: str, seconds: float = 60) -> dict | None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        d = identify(ip, timeout=2)
        if d and (not mac or d["mac"] == mac):
            return d
        time.sleep(2)
    return None


def blink(dev: dict, times: int = 3) -> None:
    """Accende e spegne la luce (o il rele') per riconoscere fisicamente il dispositivo."""
    if dev["gen"] >= 2:
        on = f"http://{dev['ip']}/rpc/Switch.Set?id=0&on=true"
        off = f"http://{dev['ip']}/rpc/Switch.Set?id=0&on=false"
    else:
        kind = "relay" if "PLG" in (dev["model"] or "") else "light"
        on, off = f"http://{dev['ip']}/{kind}/0?turn=on", f"http://{dev['ip']}/{kind}/0?turn=off"
    for _ in range(times):
        for url in (on, off):
            try:
                _get_json(url, timeout=3)
            except (OSError, ValueError):
                pass
            time.sleep(1.2)
