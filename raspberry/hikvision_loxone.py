#!/usr/bin/env python3
"""Ponte telecamere Hikvision -> Miniserver Loxone, per Raspberry Pi.

Ascolta il flusso di eventi di ogni telecamera (ISAPI /ISAPI/Event/notification/alertStream) e, per ogni
evento configurato (attraversamento linea, intrusione, movimento, ingresso/uscita da un'area...), scrive
un comando negli ingressi virtuali di Loxone: GET http://<miniserver>/dev/sps/io/<ingresso>/<comando>.
Il comando predefinito e' "SET(Sw;On;Pulse)": il programma Loxone lo passa, attraverso un ingresso virtuale
di testo, all'ingresso API di un pulsante (es. "movimento_ingresso" -> pulsante "Movimento ingresso").

Eventi letti come documenti XML completi, un solo comando per evento (le telecamere ripetono "active"
ogni secondo), filtri per regione e tipo di bersaglio (persona/veicolo), ricollegamento automatico, log
nel journal di sistema.

Configurazione TOML (sul Raspberry: /etc/hikvision-loxone/config.toml, leggibile solo dal servizio):

    [miniserver]
    host = "192.168.1.10"
    user = "..."                    # utente Loxone dedicato, con accesso agli ingressi usati
    password = "..."
    command = "SET(Sw;On;Pulse)"    # facoltativo

    [[cameras]]
    name = "Esterna"
    host = "192.168.1.20"
    user = "..."
    password = "..."

    [[cameras.events]]
    type = "linedetection"          # linedetection, fielddetection, VMD, regionEntrance, regionExiting...
    region = 1                      # facoltativo: solo questa linea/zona della telecamera
    target = "human"                # facoltativo: human o vehicle
    inputs = ["movimento_ingresso"] # ingressi virtuali di Loxone da attivare
    cooldown = 5                    # facoltativo: secondi minimi tra due comandi uguali

Uso:
  hikvision_loxone.py run                      avvia il ponte (servizio systemd)
  sudo hikvision_loxone.py configura --miniserver IP --telecamere IP1,IP2
                                               chiede e verifica utenti e password (Loxone e telecamere),
                                               poi scrive la configurazione mantenendo le regole esistenti
  hikvision_loxone.py eventi <telecamera> [s]  mostra gli eventi che arrivano (per configurare le regole)
  hikvision_loxone.py prova <ingresso>         manda il comando a Loxone per provare un ingresso
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

CONFIG_PATH = "/etc/hikvision-loxone/config.toml"
SERVICE_GROUP = "pi"    # il servizio gira come utente pi: deve poter leggere la configurazione
DEFAULT_COMMAND = "SET(Sw;On;Pulse)"
STREAM_PATH = "/ISAPI/Event/notification/alertStream"
SILENCE_S = 90          # senza dati (nemmeno il "videoloss" periodico) il collegamento si considera perso
EVENT_NAMES = {"linedetection": "attraversamento linea", "fielddetection": "intrusione", "vmd": "movimento",
               "regionentrance": "ingresso nell'area", "regionexiting": "uscita dall'area",
               "videoloss": "segnale di vita"}

log = logging.getLogger("hikvision-loxone")


@dataclass
class Rule:
    type: str
    inputs: list[str]
    region: int | None = None
    target: str | None = None
    cooldown: float = 5.0


@dataclass
class Camera:
    name: str
    host: str
    user: str
    password: str
    rules: list[Rule] = field(default_factory=list)


@dataclass
class Event:
    type: str
    state: str
    regions: set[int]
    target: str | None
    channel: str | None


def load_config(text: str) -> tuple[dict, list[Camera]]:
    data = tomllib.loads(text)
    ms = data.get("miniserver") or {}
    for key in ("host", "user", "password"):
        if not ms.get(key):
            raise ValueError(f"[miniserver] manca '{key}'")
    cams = []
    for c in data.get("cameras") or []:
        rules = [Rule(type=e["type"].lower(), inputs=list(e["inputs"]), region=e.get("region"),
                      target=(e.get("target") or None) and e["target"].lower(), cooldown=float(e.get("cooldown", 5)))
                 for e in c.get("events") or []]
        cams.append(Camera(name=c["name"], host=c["host"], user=c["user"], password=c["password"], rules=rules))
    if not cams:
        raise ValueError("nessuna telecamera in [[cameras]]")
    return ms, cams


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def parse_event(xml_bytes: bytes) -> Event | None:
    """Documento EventNotificationAlert -> Event (None se non e' un evento)."""
    try:
        root = _strip_ns(ET.fromstring(xml_bytes))
    except ET.ParseError:
        return None
    if root.tag != "EventNotificationAlert":
        return None
    regions = {int(r.text) for r in root.iter("regionID") if (r.text or "").strip().isdigit()}
    target = next((t.text.strip().lower() for t in root.iter() if t.tag in ("detectionTarget", "targetType")
                   and (t.text or "").strip()), None)
    return Event(type=(root.findtext("eventType") or "").strip().lower(),
                 state=(root.findtext("eventState") or "").strip().lower(),
                 regions=regions, target=target, channel=root.findtext("channelID"))


def matches(rule: Rule, ev: Event) -> bool:
    if ev.type != rule.type or ev.state != "active":
        return False
    if rule.region is not None and rule.region not in ev.regions:
        return False
    return rule.target is None or ev.target == rule.target


def iter_parts(stream, boundary: bytes):
    """Parti di un flusso multipart: (content-type, corpo). Usa Content-Length quando c'e' (immagini JPEG)."""
    marker = b"--" + boundary
    while True:
        line = stream.readline()
        if not line:
            return
        if not line.strip().startswith(marker):
            continue
        headers = {}
        while True:
            h = stream.readline()
            if not h:
                return
            h = h.strip()
            if not h:
                break
            if b":" in h:
                k, v = h.split(b":", 1)
                headers[k.strip().lower()] = v.strip()
        ctype = headers.get(b"content-type", b"").decode("latin-1").lower()
        length = headers.get(b"content-length")
        if length and length.isdigit():
            body = stream.read(int(length))
        else:
            chunks = []
            while True:
                pos = stream.peek(len(marker) + 2)[:len(marker)] if hasattr(stream, "peek") else b""
                if pos.startswith(marker):
                    break
                l = stream.readline()
                if not l:
                    break
                chunks.append(l)
            body = b"".join(chunks)
        yield ctype, body


class Loxone:
    def __init__(self, ms: dict) -> None:
        self.host = ms["host"]
        self.auth = (ms["user"], ms["password"])
        self.command = ms.get("command") or DEFAULT_COMMAND

    def trigger(self, input_name: str) -> bool:
        import requests
        url = (f"http://{self.host}/dev/sps/io/{urllib.parse.quote(input_name, safe='')}/"
               f"{urllib.parse.quote(self.command, safe='();,')}")
        try:
            r = requests.get(url, auth=self.auth, timeout=5)
        except requests.RequestException as e:
            log.error("Loxone non raggiungibile per %s: %s", input_name, e)
            return False
        code = re.search(r'Code"\s*:\s*"?(\d+)', r.text)
        ok = r.status_code == 200 and (code is None or code.group(1) == "200")
        if ok:
            log.info("Loxone: %s <- %s", input_name, self.command)
        else:
            log.error("Loxone ha rifiutato %s (HTTP %s %s)", input_name, r.status_code, r.text[:120])
        return ok


class CameraWorker(threading.Thread):
    def __init__(self, cam: Camera, loxone: Loxone) -> None:
        super().__init__(name=f"cam-{cam.name}", daemon=True)
        self.cam = cam
        self.loxone = loxone
        self.last_fired: dict[tuple[int, str], float] = {}
        self.last_seen: tuple | None = None

    def handle(self, ev: Event) -> None:
        seen = (ev.type, ev.state, tuple(sorted(ev.regions)), ev.target)
        if ev.type != "videoloss" and seen != self.last_seen:  # la telecamera ripete "active" ogni secondo
            log.info("%s: %s (%s)%s%s", self.cam.name, EVENT_NAMES.get(ev.type, ev.type), ev.state,
                     f" regioni {sorted(ev.regions)}" if ev.regions else "", f" bersaglio {ev.target}" if ev.target else "")
        self.last_seen = seen
        now = time.monotonic()
        for i, rule in enumerate(self.cam.rules):
            if not matches(rule, ev):
                continue
            for name in rule.inputs:
                key = (i, name)
                if now - self.last_fired.get(key, -1e9) < rule.cooldown:
                    continue
                self.last_fired[key] = now
                self.loxone.trigger(name)

    def run(self) -> None:
        import requests
        from requests.auth import HTTPDigestAuth
        backoff = 5
        while True:
            try:
                url = f"http://{self.cam.host}{STREAM_PATH}"
                with requests.get(url, auth=HTTPDigestAuth(self.cam.user, self.cam.password), stream=True,
                                  timeout=(10, SILENCE_S)) as r:
                    if r.status_code == 401:
                        log.error("%s: utente o password della telecamera non validi", self.cam.name)
                        time.sleep(300)
                        continue
                    r.raise_for_status()
                    m = re.search(r'boundary="?([^";]+)"?', r.headers.get("Content-Type", ""))
                    boundary = (m.group(1) if m else "boundary").encode()
                    log.info("%s: collegata, in ascolto degli eventi", self.cam.name)
                    backoff = 5
                    r.raw.decode_content = True
                    stream = io.BufferedReader(r.raw, buffer_size=65536)
                    for ctype, body in iter_parts(stream, boundary):
                        if "xml" in ctype or body.lstrip().startswith(b"<"):
                            ev = parse_event(body)
                            if ev:
                                self.handle(ev)
                log.warning("%s: flusso eventi chiuso dalla telecamera", self.cam.name)
            except Exception as e:  # rete, telecamera riavviata, timeout di silenzio: si riprova
                log.warning("%s: collegamento perso (%s: %s)", self.cam.name, type(e).__name__, e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)


def toml_str(value: str) -> str:
    """Stringa TOML tra virgolette: le password possono contenere qualsiasi carattere."""
    out = []
    for ch in value:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def dump_config(ms: dict, cams: list[Camera]) -> str:
    lines = ["# Scritto da 'hikvision_loxone.py configura'. Contiene password: leggibile solo da root e dal servizio.",
             "", "[miniserver]"]
    lines += [f"{k} = {toml_str(str(ms[k]))}" for k in ("host", "user", "password", "command") if ms.get(k)]
    for cam in cams:
        lines += ["", "[[cameras]]"] + [f"{k} = {toml_str(getattr(cam, k))}" for k in ("name", "host", "user", "password")]
        for r in cam.rules:
            lines += ["", "[[cameras.events]]", f"type = {toml_str(r.type)}"]
            if r.region is not None:
                lines.append(f"region = {int(r.region)}")
            if r.target:
                lines.append(f"target = {toml_str(r.target)}")
            lines.append("inputs = [" + ", ".join(toml_str(i) for i in r.inputs) + "]")
            if r.cooldown != 5.0:
                lines.append(f"cooldown = {r.cooldown:g}")
    return "\n".join(lines) + "\n"


def check_loxone(host: str, user: str, password: str) -> str | None:
    """None se il Miniserver accetta utente e password (lettura innocua), altrimenti il motivo."""
    import requests
    try:
        r = requests.get(f"http://{host}/jdev/sps/LoxAPPversion3", auth=(user, password), timeout=8)
    except requests.RequestException as e:
        return f"Miniserver non raggiungibile ({type(e).__name__})"
    if r.status_code == 401:
        return "utente o password non accettati (il nome distingue maiuscole e minuscole)"
    code = re.search(r'Code"\s*:\s*"?(\d+)', r.text)
    if r.status_code != 200 or (code and code.group(1) != "200"):
        return f"risposta inattesa del Miniserver (HTTP {r.status_code})"
    return None


def camera_info(host: str, user: str, password: str) -> tuple[str | None, str]:
    """(errore, descrizione): con le credenziali giuste legge nome, modello e firmware della telecamera."""
    import requests
    from requests.auth import HTTPDigestAuth
    try:
        r = requests.get(f"http://{host}/ISAPI/System/deviceInfo", auth=HTTPDigestAuth(user, password), timeout=8)
    except requests.RequestException as e:
        return f"telecamera non raggiungibile ({type(e).__name__})", ""
    if r.status_code == 401:
        return "utente o password non accettati (dopo vari tentativi la telecamera si blocca per un po')", ""
    if r.status_code != 200:
        return f"risposta inattesa (HTTP {r.status_code})", ""
    try:
        root = _strip_ns(ET.fromstring(r.content))
    except ET.ParseError:
        return None, "telecamera Hikvision"
    parts = [(root.findtext(k) or "").strip() for k in ("deviceName", "model", "firmwareVersion")]
    return None, " / ".join(p for p in parts if p) or "telecamera Hikvision"


def _ask(prompt: str, default: str = "") -> str:
    value = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return value or default


def write_private(path: str, text: str) -> None:
    """Scrive il file con permessi 0640, proprietario root e gruppo del servizio."""
    import grp
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chown(tmp, 0, grp.getgrnam(SERVICE_GROUP).gr_gid)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


def configure(path: str, miniserver: str, camera_hosts: list[str]) -> None:
    """Procedura guidata: le password le scrive l'utente e vengono verificate prima di salvarle."""
    import getpass
    if os.geteuid() != 0:
        raise SystemExit("esegui con sudo: la configurazione sta in /etc/hikvision-loxone")
    old_ms: dict = {}
    old_cams: list[Camera] = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old_ms, old_cams = load_config(f.read())
        except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError):
            print("(configurazione esistente illeggibile: si riparte da zero)")
    print("Configurazione del ponte telecamere -> Loxone.")
    print("Le password non si vedono mentre le scrivi: scrivile e premi Invio.\n")
    print("1) Miniserver Loxone")
    host = _ask("   Indirizzo del Miniserver", old_ms.get("host", miniserver))
    for _ in range(3):
        user = _ask("   Utente Loxone", old_ms.get("user", ""))
        password = getpass.getpass("   Password Loxone: ")
        err = check_loxone(host, user, password)
        if err is None:
            print("   OK: il Miniserver accetta l'utente")
            break
        print(f"   {err}")
    else:
        raise SystemExit("credenziali Loxone non verificate: niente e' stato salvato")
    ms = {"host": host, "user": user, "password": password, "command": old_ms.get("command", "")}

    print("\n2) Telecamere Hikvision (password vuota = salta la telecamera)")
    known = {c.host: c for c in old_cams}
    cams: list[Camera] = []
    for cam_host in dict.fromkeys(camera_hosts + [c.host for c in old_cams]):
        old = known.get(cam_host)
        print(f"\n   Telecamera {cam_host}")
        name = _ask("   Nome da usare nei log (es. Esterna)", old.name if old else f"Telecamera_{cam_host.rsplit('.', 1)[-1]}")
        for _ in range(3):
            cam_user = _ask("   Utente", old.user if old else "admin")
            cam_password = getpass.getpass("   Password: ")
            if not cam_password:
                print("   saltata")
                break
            err, info = camera_info(cam_host, cam_user, cam_password)
            if err is None:
                print(f"   OK: {info}")
                cams.append(Camera(name, cam_host, cam_user, cam_password, old.rules if old else []))
                break
            print(f"   {err}")
            if "non raggiungibile" in err:
                break
    if not cams:
        raise SystemExit("nessuna telecamera verificata: niente e' stato salvato")
    write_private(path, dump_config(ms, cams))
    rules = sum(len(c.rules) for c in cams)
    print(f"\nSalvato {path}: {len(cams)} telecamere, {rules} regole.")
    subprocess.run(["systemctl", "restart", "hikvision-loxone.service"], check=False)
    print("Servizio hikvision-loxone riavviato" + ("" if rules else ": per ora registra solo gli eventi, "
                                                    "senza comandare Loxone"))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Ponte telecamere Hikvision -> Loxone")
    ap.add_argument("--config", default=CONFIG_PATH)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run")
    a = sub.add_parser("configura", help="chiede e verifica utenti e password, poi salva la configurazione")
    a.add_argument("--miniserver", default="", help="IP del Miniserver proposto")
    a.add_argument("--telecamere", default="", help="IP delle telecamere separati da virgola")
    a = sub.add_parser("eventi", help="mostra gli eventi di una telecamera")
    a.add_argument("telecamera")
    a.add_argument("secondi", nargs="?", type=float, default=60)
    a = sub.add_parser("prova", help="manda il comando a un ingresso di Loxone")
    a.add_argument("ingresso")
    args = ap.parse_args(argv)
    if args.cmd == "configura":
        try:
            configure(args.config, args.miniserver, [h.strip() for h in args.telecamere.split(",") if h.strip()])
        except (KeyboardInterrupt, EOFError):
            raise SystemExit("\nannullato: niente e' stato salvato")
        return
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    with open(args.config, encoding="utf-8") as f:
        ms, cams = load_config(f.read())
    loxone = Loxone(ms)
    if args.cmd == "prova":
        sys.exit(0 if loxone.trigger(args.ingresso) else 1)
    if args.cmd == "eventi":
        cam = next((c for c in cams if c.name.lower() == args.telecamera.lower()), None)
        if cam is None:
            raise SystemExit(f"telecamera '{args.telecamera}' non configurata: {', '.join(c.name for c in cams)}")
        cam.rules = []  # solo ascolto: nessun comando a Loxone
        CameraWorker(cam, loxone).start()
        time.sleep(args.secondi)
        return
    for cam in cams:
        CameraWorker(cam, loxone).start()
        log.info("telecamera %s (%s): %d regole", cam.name, cam.host, len(cam.rules))
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
