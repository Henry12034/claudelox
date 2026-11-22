"""Dispositivi Shelly in rete locale: ricerca, indirizzo fisso, identificazione, collegamento al Wi-Fi.

Usa solo l'API locale HTTP degli Shelly (senza password: questi dispositivi non hanno
l'autenticazione attiva). La password del Wi-Fi la scrive l'utente: serve solo a
'configure_wifi', che la manda allo Shelly nella sua rete di configurazione e non la salva.
"""
from __future__ import annotations

import json
import re
import socket
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

NETMASK = "255.255.255.0"
AP_HOST = "192.168.33.1"  # uno Shelly nella sua rete di configurazione (modalita' AP)
SETUP_SSID_RE = re.compile(r"(?i)^shelly")
LIGHT_MODELS = ("SHCB-", "SHBLB-", "SHBDUO-", "SHVIN-", "SHBVIN-", "SHRGBW", "SHDM-", "SHCL-")
SENSOR_MODELS = ("SHMOS-", "SHHT-", "SHWT-", "SHDW-", "SHSM-", "SHGS-", "SHTRV-")


def _get_json(url: str, timeout: float = 3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _loads(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def device_info(ip: str, d) -> dict | None:
    """Dati pubblici di uno Shelly dalla risposta di /shelly, oppure None se non e' uno Shelly."""
    if not isinstance(d, dict) or not ({"type", "model", "gen"} & d.keys()):
        return None
    gen = int(d.get("gen", 1))
    return {"ip": ip, "gen": gen, "model": d.get("model") or d.get("type"), "app": d.get("app"),
            "mac": (d.get("mac") or "").upper(), "fw": d.get("ver") or d.get("fw"),
            "auth": bool(d.get("auth_en", d.get("auth")))}


def identify(ip: str, timeout: float = 2.5) -> dict | None:
    """Dati pubblici di uno Shelly (/shelly) oppure None se all'indirizzo non c'e' uno Shelly."""
    try:
        return device_info(ip, _get_json(f"http://{ip}/shelly", timeout))
    except (OSError, ValueError):
        return None


def device_kind(dev: dict) -> str:
    """Tipo di Shelly: 'luce', 'sensore' o 'presa' (prese, rele' e tutto il resto)."""
    model, app = (dev.get("model") or "").upper(), (dev.get("app") or "").upper()
    if model.startswith(LIGHT_MODELS) or any(k in app for k in ("RGBW", "DIMMER", "BULB", "LIGHT")):
        return "luce"
    if model.startswith(SENSOR_MODELS) or app.endswith("HT") or any(
            k in app for k in ("MOTION", "SMOKE", "FLOOD", "PRESENCE")):
        return "sensore"
    return "presa"


def program_kind(commands: list[str]) -> str:
    """Tipo di Shelly che il programma si aspetta su un indirizzo, dai comandi che manda e dai valori che legge."""
    text = " ".join(commands).lower()
    if any(k in text for k in ("/light/", "/color/", "/white/", "mode=color", "mode=white", "light.set", "rgbw.set")):
        return "luce"
    if "/relay/" not in text and "switch." not in text and any(
            k in text for k in ('"lux"', '"tmp"', '"bat"', '"motion"')):
        return "sensore"
    return "presa"


SHELLY_PATHS = ("/relay/", "/light/", "/color/", "/white/", "/roller/", "/rpc/", "/rpc?", "switch.", "light.set",
                "rgbw.set", "cover.")
SHELLY_FIELDS = ('"lux"', '"tmp"', '"bat"', '"motion"', '"apower"', '"ison')


def looks_like_shelly(commands: list[str]) -> bool:
    """I comandi del programma verso un indirizzo sono quelli di uno Shelly? Un inverter o una telecamera sulla porta
    80 non deve finire tra gli indirizzi da assegnare."""
    text = " ".join(c for c in commands if c).lower()
    return any(k in text for k in SHELLY_PATHS) or ("/status" in text and any(k in text for k in SHELLY_FIELDS))


def ssid_matches(ssid: str, mac: str) -> bool:
    """La rete di configurazione di uno Shelly finisce con il suo MAC, intero o nelle ultime cifre."""
    m = re.search(r"-([0-9a-fA-F]{6,12})$", ssid or "")
    return bool(m) and (mac or "").replace(":", "").upper().endswith(m.group(1).upper())


def sta_form(ssid: str, password: str, ip: str, gateway: str, dns: str) -> str:
    """Parametri di /settings/sta (gen1): modalita' client con IP fisso."""
    return urllib.parse.urlencode({"enabled": 1, "ssid": ssid, "key": password, "ipv4_method": "static", "ip": ip,
                                   "netmask": NETMASK, "gateway": gateway, "dns": dns})


def sta_rpc(ssid: str, password: str, ip: str, gateway: str, dns: str) -> str:
    """Richiesta WiFi.SetConfig (gen2+): rete di casa con IP fisso."""
    sta = {"ssid": ssid, "pass": password, "enable": True, "ipv4mode": "static", "ip": ip, "netmask": NETMASK,
           "gw": gateway, "nameserver": dns}
    return json.dumps({"id": 1, "method": "WiFi.SetConfig", "params": {"config": {"sta": sta}}})


def _sta_applied(d, ssid: str) -> bool:
    return isinstance(d, dict) and d.get("ssid") == ssid and d.get("enabled") in (True, 1)


def configure_wifi(http, dev: dict, ssid: str, password: str, ip: str, gateway: str, dns: str) -> str:
    """Collega al Wi-Fi di casa, con IP fisso, uno Shelly che e' nella sua rete di configurazione.

    'http(url, data=None, content_type=None)' -> (codice HTTP o 0, testo) parla con lo Shelly (es. dal
    telefono). Restituisce 'ok' (lo Shelly ha confermato) o 'inviato' (risposta persa: lo Shelly cambia
    rete subito). Se lo Shelly rifiuta solleva ValueError, senza la password nel messaggio.
    """
    def short(text: str) -> str:
        return " ".join((text or "").split())[:200].replace(password, "***") or "nessuna"

    base = f"http://{AP_HOST}"
    if dev["gen"] >= 2:
        code, text = http(f"{base}/rpc", sta_rpc(ssid, password, ip, gateway, dns), "application/json")
        d = _loads(text)
        if code == 0:
            return "inviato"
        if code == 200 and isinstance(d, dict) and "result" in d:
            return "ok"
        raise ValueError(f"lo Shelly ha rifiutato la rete (risposta {code}: {short(text)})")
    form = sta_form(ssid, password, ip, gateway, dns)
    code, text = http(f"{base}/settings/sta", form, "application/x-www-form-urlencoded")
    if _sta_applied(_loads(text), ssid):
        return "ok"
    if code == 0:  # risposta persa: se lo Shelly c'e' ancora, guarda cosa ha salvato
        code, text = http(f"{base}/settings/sta")
        if code == 0:
            return "inviato"
        if _sta_applied(_loads(text), ssid):
            return "ok"
    code, text = http(f"{base}/settings/sta?{form}")  # i parametri nell'indirizzo li leggono tutte le versioni
    if code == 0:
        return "inviato"
    if _sta_applied(_loads(text), ssid):
        return "ok"
    raise ValueError(f"lo Shelly non ha accettato la rete (risposta {code}: {short(text)})")


def disable_ap(dev: dict) -> bool:
    """Spegne la rete di configurazione (aperta) di uno Shelly gen2+ gia' collegato al Wi-Fi di casa.

    I gen1 la spengono da soli quando passano in modalita' client."""
    if dev["gen"] < 2:
        return True
    cfg = urllib.parse.quote(json.dumps({"ap": {"enable": False}}))
    try:
        _get_json(f"http://{dev['ip']}/rpc/WiFi.SetConfig?config={cfg}", timeout=5)
        return True
    except (OSError, ValueError):
        return False


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
    """Accende e spegne la luce (o il rele') per riconoscere fisicamente il dispositivo, poi la lascia com'era."""
    if dev["gen"] >= 2:
        app = (dev.get("app") or "").upper()
        part = "RGBW" if "RGBW" in app else "Light" if device_kind(dev) == "luce" else "Switch"
        state, key = f"http://{dev['ip']}/rpc/{part}.GetStatus?id=0", "output"
        on = f"http://{dev['ip']}/rpc/{part}.Set?id=0&on=true"
        off = f"http://{dev['ip']}/rpc/{part}.Set?id=0&on=false"
    else:
        kind = "relay" if "PLG" in (dev["model"] or "") else "light"
        state, key = f"http://{dev['ip']}/{kind}/0", "ison"
        on, off = f"http://{dev['ip']}/{kind}/0?turn=on", f"http://{dev['ip']}/{kind}/0?turn=off"
    try:
        was_on = bool(_get_json(state, timeout=3).get(key))
    except (OSError, ValueError, AttributeError):
        was_on = False
    for url in [on, off] * times + ([on] if was_on else []):
        try:
            _get_json(url, timeout=3)
        except (OSError, ValueError):
            pass
        time.sleep(1.2)
