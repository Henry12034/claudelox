"""Telefono Android collegato al PC con il cavo USB (adb): Wi-Fi per configurare i dispositivi.

Il telefono fa da ponte verso la rete di configurazione senza password di un dispositivo appena resettato
(es. uno Shelly): 'cmd wifi' cerca le reti e si collega (Android 11+, senza root) e 'curl', legato
all'interfaccia wlan0, parla con il dispositivo anche se quella rete non ha internet (intanto la rete
predefinita del telefono resta quella dei dati mobili). Indirizzi e dati arrivano a curl dallo standard
input, mai dalla riga di comando: possono contenere una password.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

SCAN_RE = re.compile(r"^\s*([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\s+(\d+)\s+(-?\d+)\S*\s+[\d.,]+\s+(.*?)\s*(\[.*\])?\s*$")
CONNECTED_RE = re.compile(r'Wifi is connected to "(.*)"')
IP_RE = re.compile(r"\bIP: /(\d+\.\d+\.\d+\.\d+)")
NETWORK_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s+(\S+?)\^?\s*$")


class PhoneError(Exception):
    pass


def find_adb() -> str | None:
    """adb nel PATH oppure nelle cartelle abituali degli Android platform-tools."""
    found = shutil.which("adb")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    candidates = [Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe"] if local else []
    candidates += [Path.home() / "Android" / "Sdk" / "platform-tools" / "adb", Path("C:/platform-tools/adb.exe")]
    return next((str(p) for p in candidates if p.is_file()), None)


def parse_scan(text: str) -> list[dict]:
    """Righe di 'cmd wifi list-scan-results' -> reti (bssid, freq in MHz, rssi in dBm, ssid, flags)."""
    nets = []
    for line in text.splitlines():
        m = SCAN_RE.match(line)
        if m:
            nets.append({"bssid": m[1].lower(), "freq": int(m[2]), "rssi": int(m[3]), "ssid": m[4],
                         "flags": m[5] or ""})
    return nets


def is_open(net: dict) -> bool:
    """Rete senza password (le reti protette hanno WPA/RSN/WEP tra i flag)."""
    return not re.search(r"WPA|RSN|WEP|SAE|PSK|EAP", net.get("flags", ""))


def parse_status(text: str) -> dict:
    """'cmd wifi status' -> rete Wi-Fi attuale (ssid) e indirizzo del telefono (ip); None se scollegato."""
    m = CONNECTED_RE.search(text)
    ip = IP_RE.search(text) if m else None
    return {"ssid": m[1] if m else None, "ip": ip[1] if ip else None}


def parse_networks(text: str) -> list[tuple[int, str]]:
    """'cmd wifi list-networks' -> reti salvate (id, ssid), una volta sola per id."""
    out: dict[int, str] = {}
    for line in text.splitlines()[1:]:
        m = NETWORK_RE.match(line)
        if m:
            out.setdefault(int(m[1]), m[2])
    return list(out.items())


def _cfg(value: str) -> str:
    """Valore tra virgolette in un file di configurazione di curl."""
    return (value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
            .replace("\t", "\\t"))


def curl_config(url: str, data: str | None = None, content_type: str | None = None) -> bytes:
    """Configurazione di curl (letta dallo standard input con -K -): POST se ci sono dati."""
    lines = [f'url = "{_cfg(url)}"']
    if data is not None:
        lines.append(f'data-raw = "{_cfg(data)}"')
        if content_type:
            lines.append(f'header = "Content-Type: {_cfg(content_type)}"')
    return ("\n".join(lines) + "\n").encode("utf-8")


class Phone:
    def __init__(self, serial: str | None = None, adb: str | None = None):
        self.adb = adb or find_adb()
        if not self.adb:
            raise PhoneError("adb non trovato: installa gli Android platform-tools (oppure mettili nel PATH)")
        self.serial = serial or self._only_device()

    def _only_device(self) -> str:
        out = subprocess.run([self.adb, "devices"], capture_output=True, text=True, timeout=30).stdout
        rows = [line.split() for line in out.splitlines()[1:] if line.strip()]
        ready = [r[0] for r in rows if len(r) > 1 and r[1] == "device"]
        if len(ready) == 1:
            return ready[0]
        if ready:
            raise PhoneError(f"piu' telefoni collegati ({', '.join(ready)}): scegli con --telefono")
        if any(len(r) > 1 and r[1] == "unauthorized" for r in rows):
            raise PhoneError("sul telefono conferma 'Consenti debug USB' e riprova")
        raise PhoneError("nessun telefono collegato via USB con il debug USB attivo")

    def shell(self, command: str, data: bytes | None = None, timeout: float = 30) -> tuple[int, str]:
        r = subprocess.run([self.adb, "-s", self.serial, "shell", command], input=data or b"",
                           capture_output=True, timeout=timeout)
        return r.returncode, r.stdout.decode("utf-8", "replace")

    def model(self) -> str:
        return self.shell("getprop ro.product.model")[1].strip()

    def status(self) -> dict:
        return parse_status(self.shell("cmd wifi status")[1])

    def scan(self, wait: float = 5) -> list[dict]:
        self.shell("cmd wifi start-scan")
        time.sleep(wait)
        return parse_scan(self.shell("cmd wifi list-scan-results")[1])

    def wait_for_network(self, ssid: str, timeout: float = 60) -> str | None:
        """Attende che il telefono sia collegato a 'ssid' con un indirizzo; restituisce l'indirizzo."""
        end = time.monotonic() + timeout
        while True:
            st = self.status()
            if st["ssid"] == ssid and st["ip"]:
                return st["ip"]
            if time.monotonic() >= end:
                return None
            time.sleep(1.5)

    def connect_open(self, ssid: str, timeout: float = 40) -> str:
        """Collega il telefono a una rete senza password; restituisce l'indirizzo del telefono su quella rete."""
        code, out = self.shell(f"cmd wifi connect-network {shlex.quote(ssid)} open")
        if code != 0:
            raise PhoneError(f"il telefono non riesce a collegarsi a {ssid}: {out.strip()}")
        ip = self.wait_for_network(ssid, timeout)
        if not ip:
            raise PhoneError(f"il telefono non si e' collegato a {ssid} entro {timeout:.0f} s")
        return ip

    def forget(self, ssid: str) -> None:
        """Toglie una rete da quelle salvate: il telefono torna da solo alla rete di casa."""
        for net_id, name in parse_networks(self.shell("cmd wifi list-networks")[1]):
            if name == ssid:
                self.shell(f"cmd wifi forget-network {net_id}")

    def http(self, url: str, data: str | None = None, content_type: str | None = None,
             timeout: float = 8) -> tuple[int, str]:
        """Richiesta HTTP dal telefono attraverso il suo Wi-Fi (POST se ci sono dati) -> (codice o 0, testo)."""
        code, out = self.shell(f"curl --interface wlan0 -s -m {int(timeout)} -w '\\n%{{http_code}}' -K -",
                               curl_config(url, data, content_type), timeout + 20)
        body, _, status = out.rpartition("\n")
        try:
            return int(status.strip()), body
        except ValueError:
            return 0, out
