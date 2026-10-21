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
    channel = 2                     # facoltativo, per i registratori (NVR): solo gli eventi di questo canale
    inputs = ["movimento_ingresso"] # ingressi virtuali di Loxone da attivare
    cooldown = 5                    # facoltativo: secondi minimi tra due comandi uguali

Uso:
  hikvision_loxone.py run                      avvia il ponte (servizio systemd)
  sudo hikvision_loxone.py configura --miniserver IP --telecamere IP1,IP2
                                               chiede e verifica utenti e password (Loxone e telecamere),
                                               poi scrive la configurazione mantenendo le regole esistenti
  hikvision_loxone.py info                     modello, telecamere collegate, rilevazioni attive, eventi inviati
  hikvision_loxone.py ora <telecamera>         ora automatica (NTP) e fuso dell'Europa centrale con ora legale
  hikvision_loxone.py volti elenco <registratore>
  hikvision_loxone.py volti aggiungi <registratore> <archivio> <persona> <foto.jpg...>
  hikvision_loxone.py volti togli <registratore> <archivio> <PID>
  hikvision_loxone.py volti motore <registratore> [face_recognition]
  hikvision_loxone.py volti rilevamento <registratore> <canale> [--sensibilita 1-5] [--spento]
  hikvision_loxone.py volti risultati <registratore> [--ore 2] [--xml]
  hikvision_loxone.py volti confronto <registratore> <canale> <archivio> [--soglia 80] [--spento]
                                               archivio dei volti e riconoscimento facciale di un registratore
  hikvision_loxone.py volti-locali impara <persona> <foto.jpg...> | impara-da <telecamera> <persona>
  hikvision_loxone.py volti-locali elenco | dimentica <persona> | prova <telecamera>
                                               riconoscimento facciale sul Raspberry (regole di tipo "volto")
  hikvision_loxone.py firmware <telecamera> <digicap.dav> --conferma
                                               aggiorna il firmware, riavvia e verifica la nuova versione
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
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

CONFIG_PATH = "/etc/hikvision-loxone/config.toml"
ISAPI_NS = "http://www.isapi.org/ver20/XMLSchema"
CET_TZ = "CST-1:00:00DST01:00:00,M3.5.0/02:00:00,M10.5.0/03:00:00"   # Europa centrale (UTC+1) con ora legale
MAX_FACE_KB = 500       # il registratore accetta foto fino a 512 KB
SERVICE_GROUP = "pi"    # il servizio gira come utente pi: deve poter leggere la configurazione
DEFAULT_COMMAND = "SET(Sw;On;Pulse)"
STREAM_PATH = "/ISAPI/Event/notification/alertStream"
SILENCE_S = 90          # senza dati (nemmeno il "videoloss" periodico) il collegamento si considera perso
EVENT_NAMES = {"linedetection": "attraversamento linea", "fielddetection": "intrusione", "vmd": "movimento",
               "regionentrance": "ingresso nell'area", "regionexiting": "uscita dall'area",
               "videoloss": "perdita del video", "tamperdetection": "manomissione", "shelteralarm": "oscuramento",
               "facedetection": "volto rilevato", "facesnap": "volto fotografato", "io": "ingresso di allarme",
               "pir": "sensore PIR", "diskfull": "disco pieno", "diskerror": "errore del disco",
               "nicbroken": "rete scollegata", "ipconflict": "conflitto di indirizzo IP",
               "illaccess": "accesso non autorizzato", "facecontrast": "volto confrontato con l'archivio",
               "facecontrastsuccess": "volto riconosciuto", "facecontrastfailure": "volto sconosciuto",
               "alarmresult": "risultato del riconoscimento facciale"}
# eventi del riconoscimento facciale (registratore): le regole con "person" guardano solo questi
FACE_TYPES = {"facecontrast", "facecontrastsuccess", "whitelistfacecontrast", "alarmresult"}
LOCAL_FACE = "volto"     # tipo di regola: riconoscimento facciale sul Raspberry con le foto della telecamera
LOCAL_FACE_TRIGGERS = ("fielddetection", "linedetection")
SNAPSHOT_PATH = "/ISAPI/Streaming/channels/101/picture"   # foto a piena risoluzione della telecamera
WATCH_REPORT_S = 600    # analisi continua: un riepilogo nel log ogni 10 minuti
WATCH_LOG_GAP_S = 2.0   # ... e al massimo una riga ogni 2 s mentre riconosce qualcuno
FACE_SAVED = FACE_TYPES | {"facedetection", "facecontrastfailure"}   # eventi di cui si tengono documento e foto
FACE_DIR = os.path.join(os.environ.get("STATE_DIRECTORY", "/var/lib/hikvision-loxone"), "volti")
FACE_KEEP = 60          # file tenuti in FACE_DIR: documento e foto degli ultimi eventi dei volti
MIN_HIT_GAP_S = 1.0     # due riconoscimenti piu' vicini di cosi' sono lo stesso (eventi ripetuti)
# rilevazioni che si possono accendere su ogni canale video: dove leggere se sono attive
SMART_PATHS = {"linedetection": "/ISAPI/Smart/LineDetection/{ch}", "fielddetection": "/ISAPI/Smart/FieldDetection/{ch}",
               "regionentrance": "/ISAPI/Smart/regionEntrance/{ch}", "regionexiting": "/ISAPI/Smart/regionExiting/{ch}",
               "vmd": "/ISAPI/System/Video/inputs/channels/{ch}/motionDetection"}
LOG_REPEAT_S = 60       # un evento che resta "active" si riscrive nel log al massimo una volta al minuto

log = logging.getLogger("hikvision-loxone")


@dataclass
class Rule:
    type: str
    inputs: list[str]
    region: int | None = None
    target: str | None = None
    cooldown: float = 5.0
    channel: int | None = None      # registratori (NVR): solo gli eventi di questo canale
    person: str | None = None       # riconoscimento facciale: solo questa persona dell'archivio
    min_similarity: float = 0.0     # ... con somiglianza almeno questa (percento)
    confirmations: int = 1          # riconoscimenti necessari entro "window" secondi prima di agire
    window: float = 20.0
    dry_run: bool = False           # prova: scrive nel log cosa farebbe, senza comandare Loxone
    # regole "volto" (riconoscimento sul Raspberry): eventi della telecamera che fanno partire la raffica di foto
    triggers: list[str] = field(default_factory=lambda: list(LOCAL_FACE_TRIGGERS))
    continuous: bool = False        # regole "volto": analisi continua delle foto, senza aspettare gli eventi


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
    person: str | None = None       # riconoscimento facciale: persona dell'archivio
    similarity: float | None = None  # ... e somiglianza in percento


def load_config(text: str) -> tuple[dict, list[Camera]]:
    data = tomllib.loads(text)
    ms = data.get("miniserver") or {}
    for key in ("host", "user", "password"):
        if not ms.get(key):
            raise ValueError(f"[miniserver] manca '{key}'")
    cams = []
    for c in data.get("cameras") or []:
        rules = [rule_from_dict(e) for e in c.get("events") or []]
        cams.append(Camera(name=c["name"], host=c["host"], user=c["user"], password=c["password"], rules=rules))
    if not cams:
        raise ValueError("nessuna telecamera in [[cameras]]")
    return ms, cams


def rule_from_dict(e: dict) -> Rule:
    if not e.get("type") or not e.get("inputs"):
        raise ValueError("una regola vuole almeno 'type' e 'inputs'")
    return Rule(type=e["type"].lower(), inputs=list(e["inputs"]), region=e.get("region"),
                target=(e.get("target") or None) and e["target"].lower(), cooldown=float(e.get("cooldown", 5)),
                channel=e.get("channel"), person=e.get("person"), min_similarity=float(e.get("min_similarity", 0)),
                confirmations=int(e.get("confirmations", 1)), window=float(e.get("window", 20)),
                dry_run=bool(e.get("dry_run", False)),
                triggers=[x.lower() for x in e.get("triggers", LOCAL_FACE_TRIGGERS)],
                continuous=bool(e.get("continuous", False)))


def describe_rule(r: Rule) -> str:
    if r.type == LOCAL_FACE and r.continuous:
        parts = ["analisi continua dei volti sul Raspberry"]
    elif r.type == LOCAL_FACE:
        parts = [f"riconoscimento sul Raspberry dopo {'/'.join(EVENT_NAMES.get(x, x) for x in r.triggers)}"]
    else:
        parts = [f"evento {r.type}"]
    if r.channel is not None:
        parts.append(f"canale {r.channel}")
    if r.region is not None:
        parts.append(f"zona {r.region}")
    if r.target:
        parts.append(f"bersaglio {r.target}")
    if r.person:
        parts.append(f"persona {r.person} almeno {r.min_similarity:g}%")
    if r.confirmations > 1 or r.type == LOCAL_FACE:
        parts.append(f"{r.confirmations} conferme in {r.window:g} s")
    return (", ".join(parts) + f" -> {', '.join(r.inputs)} (pausa {r.cooldown:g} s)"
            + (" [PROVA: non comanda Loxone]" if r.dry_run else ""))


def change_rules(cam: Camera, add: str | None, remove: int | None, test: list[str] | None) -> None:
    """Modifica le regole in memoria: add e' una tabella TOML in linea, remove e test usano il numero (da 1)."""
    if add:
        cam.rules.append(rule_from_dict(tomllib.loads(f"r = {add}")["r"]))
    if remove:
        if not 1 <= remove <= len(cam.rules):
            raise IndexError(f"regola {remove} inesistente")
        del cam.rules[remove - 1]
    if test:
        n, flag = int(test[0]), test[1].lower()
        if not 1 <= n <= len(cam.rules):
            raise IndexError(f"regola {n} inesistente")
        if flag not in ("si", "no"):
            raise ValueError("usa si (solo nel log) oppure no (la regola comanda Loxone)")
        cam.rules[n - 1].dry_run = flag == "si"


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
    ev = Event(type=(root.findtext("eventType") or "").strip().lower(),
               state=(root.findtext("eventState") or "").strip().lower(),
               regions=regions, target=target,
               channel=(root.findtext("channelID") or root.findtext("dynChannelID") or "").strip() or None)
    if ev.type in FACE_TYPES:
        ev.person, ev.similarity = face_match(root)
    return ev


def face_match(root: ET.Element) -> tuple[str | None, float | None]:
    """Persona dell'archivio e somiglianza (percento) di un evento del riconoscimento facciale.

    Il formato cambia tra modelli e firmware: la somiglianza e' il valore piu' alto tra i campi "similarity",
    la persona il primo campo "name" che non sta direttamente sotto la radice (li' c'e' il canale).
    """
    sims = []
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.lower() in ("similarity", "maxsimilarity"):
            try:
                sims.append(float((el.text or "").strip()))
            except ValueError:
                pass
    similarity = max(sims) if sims else None
    if similarity is not None and similarity <= 1.0:
        similarity *= 100                                    # alcuni modelli scrivono 0,87 invece di 87
    top_level = set(root)
    person = next(((el.text or "").strip() for el in root.iter() if el.tag in ("name", "personName", "humanName")
                   and el not in top_level and (el.text or "").strip()), None)
    return person, similarity


def matches(rule: Rule, ev: Event) -> bool:
    if ev.state != "active":
        return False
    if rule.person:                                          # regola del riconoscimento facciale
        if ev.type not in FACE_TYPES or (ev.person or "").casefold() != rule.person.casefold():
            return False
        if (ev.similarity or 0) < rule.min_similarity:
            return False
    elif ev.type != rule.type:
        return False
    if rule.region is not None and rule.region not in ev.regions:
        return False
    if rule.channel is not None and str(rule.channel) != ev.channel:
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


def save_face_file(name: str, ext: str, data: bytes, stamp: bool = True, folder: str | None = None) -> str | None:
    """Salva documento o foto di un evento dei volti (per controllo); tiene solo gli ultimi FACE_KEEP file."""
    folder = folder or FACE_DIR
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}_{name}" if stamp else name
    try:
        os.makedirs(folder, mode=0o750, exist_ok=True)
        with open(os.path.join(folder, re.sub(r"[^\w.-]", "_", stem) + ext), "wb") as f:
            f.write(data)
        files = sorted(os.listdir(folder))
        for old in files[:-FACE_KEEP]:
            os.remove(os.path.join(folder, old))
    except OSError as e:
        log.warning("evento dei volti non salvato in %s: %s", folder, e)
        return None
    return re.sub(r"[^\w.-]", "_", stem)


_RECOGNIZER = None
RECOGNIZER_LOCK = threading.Lock()


def local_recognizer():
    """Riconoscitore del Raspberry (OpenCV), caricato alla prima raffica dopo aver controllato i modelli."""
    global _RECOGNIZER
    if _RECOGNIZER is None:
        import volti_locali
        problems = volti_locali.verify_models()
        if problems:
            raise RuntimeError("; ".join(problems))
        _RECOGNIZER = volti_locali.Recognizer()
    return _RECOGNIZER


def snapshot_grabber(cam: Camera, recog):
    """Funzione che chiede alla telecamera una foto a piena risoluzione: (jpeg, immagine decodificata)."""
    import requests
    from requests.auth import HTTPDigestAuth
    session = requests.Session()
    session.auth = HTTPDigestAuth(cam.user, cam.password)

    def grab():
        r = session.get(f"http://{cam.host}{SNAPSHOT_PATH}", timeout=5)
        r.raise_for_status()
        img = recog.decode(r.content)
        if img is None:
            raise ValueError("foto non leggibile")
        return r.content, img
    return grab


def local_faces_command(args, camera) -> None:
    """Comandi "volti-locali": impronte dei volti noti e prove con le foto della telecamera."""
    import volti_locali as vl
    problems = vl.verify_models()
    if problems:
        raise RuntimeError("; ".join(problems))
    recog = vl.Recognizer()
    if args.azione == "elenco":
        for name, arr in recog.known.items():
            print(f"{name}: {len(arr)} impronte")
        if not recog.known:
            print("nessun volto imparato")
        return
    if args.azione == "dimentica":
        print(f"{args.persona}: " + ("dimenticato" if recog.forget(args.persona) else "non c'era"))
        recog.save()
        return
    if args.azione == "impara":
        for path in args.foto:
            img = recog.cv2.imread(path)
            hit = recog.enroll(args.persona, img) if img is not None else None
            print(f"{os.path.basename(path)}: "
                  + (f"volto {hit.width} px, sicurezza {hit.score:.2f}" if hit else "nessun volto trovato"))
        recog.save()
        print(f"{args.persona}: {len(recog.known.get(args.persona, []))} impronte in totale")
        return
    grab = snapshot_grabber(camera(args.telecamera), recog)
    end, added = time.monotonic() + args.secondi, 0
    while time.monotonic() < end:
        try:
            _, img = grab()
        except Exception as e:
            print(f"foto non arrivata: {type(e).__name__}")
            continue
        stamp = time.strftime("%H:%M:%S")
        if args.azione == "prova":
            hits = recog.identify(img)
            print(stamp, ", ".join(f"{h.person or 'sconosciuto'} {h.similarity * 100:.0f}% ({h.width} px)"
                                   for h in hits) or "nessun volto")
            continue
        faces = [f for f in recog.detect(img) if f[2] >= vl.MIN_FACE_PX]          # impara-da
        if not faces:
            print(stamp, "nessun volto")
            continue
        face = max(faces, key=lambda r: r[2] * r[3])
        vec = recog.feature(img, face)
        sim = recog.similarity_to(args.persona, vec)
        if sim is not None and sim < vl.SAME_PERSON and not args.forza:
            print(stamp, f"volto {int(face[2])} px troppo diverso da {args.persona} ({sim * 100:.0f}%): non lo imparo")
            continue
        recog.add(args.persona, vec)
        added += 1
        print(stamp, f"imparato volto {int(face[2])} px"
              + (f" (somiglianza {sim * 100:.0f}%)" if sim is not None else ""))
        if added >= args.massimo:
            break
    if args.azione == "impara-da" and added:
        recog.save()
        print(f"{args.persona}: {added} impronte nuove, {len(recog.known[args.persona])} in totale")


def save_face_crop(recog, name: str, jpeg: bytes, box: tuple[int, int, int, int]) -> None:
    """Salva solo il ritaglio del volto (con un po' di margine), non tutta la scena."""
    img = recog.decode(jpeg)
    if img is None:
        return
    x, y, w, h = box
    pad = max(w, h) // 2
    crop = img[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad]
    ok, data = recog.cv2.imencode(".jpg", crop)
    if ok:
        save_face_file(name, ".jpg", data.tobytes())


class CameraWorker(threading.Thread):
    def __init__(self, cam: Camera, loxone: Loxone) -> None:
        super().__init__(name=f"cam-{cam.name}", daemon=True)
        self.cam = cam
        self.loxone = loxone
        self.last_fired: dict[tuple[int, str], float] = {}
        self.logged: dict[tuple, tuple[str, float]] = {}
        self.hits: dict[int, list[float]] = {}
        self.burst_running = False                 # una sola raffica di foto alla volta
        self.burst_started = -1e9

    def handle(self, ev: Event, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        key = (ev.type, ev.channel, tuple(sorted(ev.regions)), ev.target)
        last = self.logged.get(key)
        heartbeat = ev.type == "videoloss" and ev.state == "inactive"   # segnale di vita periodico
        face = ev.type in FACE_TYPES                                     # rari: si registrano tutti
        # la telecamera ripete "active" ogni secondo: si registra il cambio di stato, poi al massimo una volta al minuto
        if not heartbeat and (face or last is None or last[0] != ev.state or now - last[1] > LOG_REPEAT_S):
            self.logged[key] = (ev.state, now)
            log.info("%s: %s (%s)%s%s%s%s", self.cam.name, EVENT_NAMES.get(ev.type, ev.type), ev.state,
                     f" canale {ev.channel}" if ev.channel else "",
                     f" regioni {sorted(ev.regions)}" if ev.regions else "", f" bersaglio {ev.target}" if ev.target else "",
                     f" persona {ev.person or 'nessuna'}, somiglianza {ev.similarity:.0f}%" if face and ev.similarity
                     is not None else (f" persona {ev.person}" if face and ev.person else ""))
        for i, rule in enumerate(self.cam.rules):
            if rule.type == LOCAL_FACE:
                if not rule.continuous and self.face_trigger(i, rule, ev, now):
                    self.start_face_burst(i, rule)
                continue
            if not matches(rule, ev):
                continue
            if rule.confirmations > 1:                       # es. il volto riconosciuto in 2 foto diverse
                hits = [t for t in self.hits.get(i, []) if now - t <= rule.window]
                if not hits or now - hits[-1] >= MIN_HIT_GAP_S:
                    hits.append(now)
                self.hits[i] = hits
                if len(hits) < rule.confirmations:
                    log.info("%s: regola %d, conferma %d di %d", self.cam.name, i + 1, len(hits), rule.confirmations)
                    continue
                self.hits[i] = []
            for name in rule.inputs:
                key = (i, name)
                if now - self.last_fired.get(key, -1e9) < rule.cooldown:
                    continue
                self.last_fired[key] = now
                if rule.dry_run:
                    log.warning("%s: PROVA, avrei attivato %s", self.cam.name, name)
                else:
                    self.loxone.trigger(name)

    def face_trigger(self, i: int, rule: Rule, ev: Event, now: float) -> bool:
        """Una persona per la regola "volto": parte la raffica (se non ne gira gia' una e la pausa e' finita)."""
        if ev.state != "active" or ev.type not in rule.triggers or self.burst_running:
            return False
        if rule.target is not None and ev.target != rule.target:
            return False
        if rule.region is not None and rule.region not in ev.regions:
            return False
        if any(now - self.last_fired.get((i, name), -1e9) < rule.cooldown for name in rule.inputs):
            return False
        return now - self.burst_started >= MIN_HIT_GAP_S

    def start_face_burst(self, i: int, rule: Rule) -> None:
        self.burst_running = True
        self.burst_started = time.monotonic()
        threading.Thread(target=self.face_burst, args=(i, rule), name=f"volto-{self.cam.name}", daemon=True).start()

    def face_burst(self, i: int, rule: Rule) -> None:
        """Foto a raffica dalla telecamera, riconoscimento sul Raspberry, poi comando (o PROVA) se e' la persona."""
        try:
            from volti_locali import run_burst
            recog = local_recognizer()
            grab = snapshot_grabber(self.cam, recog)
            with RECOGNIZER_LOCK:
                recog.load()                                 # impronte aggiornate (es. dopo "volti-locali impara")
                res = run_burst(grab, recog.identify, rule.person, rule.min_similarity, max(1, rule.confirmations),
                                rule.window)
                if res.best is not None:
                    save_face_crop(recog, f"{self.cam.name}_volto_{res.best.person or 'sconosciuto'}_"
                                          f"{res.best.similarity * 100:.0f}", res.best_photo, res.best.box)
            best = (f"{res.best.person or 'sconosciuto'} {res.best.similarity * 100:.0f}% ({res.best.width} px)"
                    if res.best else "nessun volto")
            log.info("%s: riconoscimento sul Raspberry: %d foto, %d volti, migliore %s, %d/%d conferme%s",
                     self.cam.name, res.photos, res.faces, best, res.matches, rule.confirmations,
                     f", {len(res.errors)} foto mancate" if res.errors else "")
            if res.fired:
                now = time.monotonic()
                for name in rule.inputs:
                    self.last_fired[(i, name)] = now
                    if rule.dry_run:
                        log.warning("%s: PROVA, avrei attivato %s (%s riconosciuto)", self.cam.name, name, rule.person)
                    else:
                        log.warning("%s: %s riconosciuto, attivo %s", self.cam.name, rule.person, name)
                        self.loxone.trigger(name)
        except Exception as e:                                # il resto del ponte continua a funzionare
            log.error("%s: riconoscimento sul Raspberry non riuscito: %s: %s", self.cam.name, type(e).__name__, e)
        finally:
            self.burst_running = False

    def face_watch(self, i: int, rule: Rule) -> None:
        """Analisi continua: foto dalla telecamera una dopo l'altra, comando (o PROVA) quando riconosce la persona."""
        from volti_locali import PHOTO_GAP_S, FaceWatch
        watch = FaceWatch(rule.person, rule.min_similarity, rule.confirmations, rule.window, rule.cooldown)
        recog = grab = known_mtime = None
        failing, since, last_log = False, time.monotonic(), -1e9
        log.info("%s: analisi continua dei volti: %s almeno %g%%, %d conferme in %g s%s", self.cam.name, rule.person,
                 rule.min_similarity, rule.confirmations, rule.window, " [PROVA]" if rule.dry_run else "")
        while True:
            try:
                if recog is None:
                    recog = local_recognizer()
                    grab = snapshot_grabber(self.cam, recog)
                jpeg, img = grab()
                if failing:
                    log.info("%s: analisi continua ripartita", self.cam.name)
                    failing = False
                with RECOGNIZER_LOCK:
                    try:
                        mtime = os.path.getmtime(recog.known_path)
                    except OSError:
                        mtime = None
                    if mtime != known_mtime:                 # impronte cambiate (es. "volti-locali impara")
                        recog.load()
                        known_mtime = mtime
                    hits = recog.identify(img)
            except Exception as e:                            # telecamera irraggiungibile o modelli mancanti
                if not failing:
                    log.warning("%s: analisi continua in pausa: %s: %s", self.cam.name, type(e).__name__, e)
                    failing = True
                time.sleep(10)
                continue
            now = time.monotonic()
            mine, fire = watch.step(hits, now)
            if mine and (fire or now - last_log >= WATCH_LOG_GAP_S):
                best = max(mine, key=lambda h: h.similarity)
                log.info("%s: %s riconosciuto %.0f%% (volto %d px)", self.cam.name, best.person, best.similarity * 100,
                         best.width)
                save_face_crop(recog, f"{self.cam.name}_{best.person}_{best.similarity * 100:.0f}", jpeg, best.box)
                last_log = now
            if fire:
                for name in rule.inputs:
                    self.last_fired[(i, name)] = now
                    if rule.dry_run:
                        log.warning("%s: PROVA, avrei attivato %s (%s riconosciuto)", self.cam.name, name, rule.person)
                    else:
                        log.warning("%s: %s riconosciuto, attivo %s", self.cam.name, rule.person, name)
                        self.loxone.trigger(name)
            if now - since >= WATCH_REPORT_S:
                log.info("%s: analisi continua, ultimi %d min: %d foto, %d volti, migliore sotto soglia %.0f%%",
                         self.cam.name, round((now - since) / 60), watch.photos, watch.faces, watch.best_other * 100)
                watch.reset_stats()
                since = now
            time.sleep(PHOTO_GAP_S)

    def run(self) -> None:
        import requests
        from requests.auth import HTTPDigestAuth
        for i, rule in enumerate(self.cam.rules):
            if rule.type == LOCAL_FACE and rule.continuous:
                threading.Thread(target=self.face_watch, args=(i, rule), name=f"continua-{self.cam.name}",
                                 daemon=True).start()
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
                    face_stem, face_pics = None, 0
                    for ctype, body in iter_parts(stream, boundary):
                        if "xml" in ctype or body.lstrip().startswith(b"<"):
                            ev = parse_event(body)
                            face_stem, face_pics = None, 0
                            if ev:
                                if ev.type in FACE_SAVED:
                                    face_stem = save_face_file(f"{self.cam.name}_{ev.type}", ".xml", body)
                                self.handle(ev)
                        elif face_stem and "image" in ctype and face_pics < 2:   # volto e scena dell'evento
                            face_pics += 1
                            save_face_file(f"{face_stem}_{face_pics}", ".jpg", body, stamp=False)
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
            if r.channel is not None:
                lines.append(f"channel = {int(r.channel)}")
            if r.person:
                lines.append(f"person = {toml_str(r.person)}")
            if r.min_similarity:
                lines.append(f"min_similarity = {r.min_similarity:g}")
            if r.confirmations != 1:
                lines += [f"confirmations = {int(r.confirmations)}", f"window = {r.window:g}"]
            if r.dry_run:
                lines.append("dry_run = true")
            if r.type == LOCAL_FACE:
                lines.append("triggers = [" + ", ".join(toml_str(x) for x in r.triggers) + "]")
            if r.continuous:
                lines.append("continuous = true")
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


def _get_xml(cam: Camera, path: str, timeout: float = 10) -> tuple[ET.Element | None, str]:
    """Lettura ISAPI con le credenziali della telecamera: (XML senza namespace oppure None, motivo)."""
    import requests
    from requests.auth import HTTPDigestAuth
    try:
        r = requests.get(f"http://{cam.host}{path}", auth=HTTPDigestAuth(cam.user, cam.password), timeout=timeout)
    except requests.RequestException as e:
        return None, type(e).__name__
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    try:
        return _strip_ns(ET.fromstring(r.content)), ""
    except ET.ParseError:
        return None, "risposta non XML"


SECRET_TAG_RE = re.compile(r"(?i)pass|secret|key|token")
PRIVATE_PATH_RE = re.compile(r"(?i)security|user|account")


def redact_xml(root: ET.Element) -> ET.Element:
    """Nasconde i valori dei campi che possono contenere password o chiavi."""
    for el in root.iter():
        if isinstance(el.tag, str) and SECRET_TAG_RE.search(el.tag) and (el.text or "").strip():
            el.text = "***"
    return root


def active_detections(cam: Camera, channel: str, get=_get_xml) -> list[str]:
    names = []
    for etype, path in SMART_PATHS.items():
        root, _ = get(cam, path.format(ch=channel))
        if root is not None and (root.findtext("enabled") or "").strip().lower() == "true":
            names.append(EVENT_NAMES[etype])
    return names


def describe_device(cam: Camera, get=_get_xml) -> list[str]:
    """Solo letture: modello, telecamere collegate (registratori), rilevazioni attive ed eventi inviati."""
    info, err = get(cam, "/ISAPI/System/deviceInfo")
    if info is None:
        return [f"{cam.name} ({cam.host}): non leggibile ({err})"]
    out = [f"{cam.name} ({cam.host}): {info.findtext('model') or '?'}, tipo {info.findtext('deviceType') or '?'}, "
           f"firmware {info.findtext('firmwareVersion') or '?'}"]
    chans, _ = get(cam, "/ISAPI/ContentMgmt/InputProxy/channels")
    if chans is not None:                                    # registratore: una riga per telecamera collegata
        status, _ = get(cam, "/ISAPI/ContentMgmt/InputProxy/channels/status")
        online = {s.findtext("id"): (s.findtext("online") or "").strip().lower() == "true"
                  for s in (status.iter("InputProxyChannelStatus") if status is not None else [])}
        for ch in chans.iter("InputProxyChannel"):
            cid = (ch.findtext("id") or "").strip()
            state = {True: "in linea", False: "NON in linea"}.get(online.get(cid), "stato sconosciuto")
            found = active_detections(cam, cid, get)
            out.append(f"  canale {cid}: {ch.findtext('name') or '?'}, "
                       f"{ch.findtext('sourceInputPortDescriptor/ipAddress') or '?'} ({state}); "
                       f"rilevazioni attive: {', '.join(found) or 'nessuna'}")
    else:
        out.append(f"  rilevazioni attive: {', '.join(active_detections(cam, '1', get)) or 'nessuna'}")
    trig, err = get(cam, "/ISAPI/Event/triggers")
    if trig is None:
        out.append(f"  eventi inviati: non leggibili ({err})")
    else:
        sent: dict[str, list[str]] = {}
        unsent: dict[str, list[str]] = {}
        for t in trig.iter("EventTrigger"):
            etype = (t.findtext("eventType") or "?").strip()
            ch = (t.findtext("videoInputChannelID") or t.findtext("dynVideoInputChannelID")
                  or t.findtext("inputIOPortID") or "").strip()
            methods = {(n.findtext("notificationMethod") or "").strip().lower()
                       for n in t.iter("EventTriggerNotification")}
            group = sent if "center" in methods else unsent
            group.setdefault(EVENT_NAMES.get(etype.lower(), etype), []).append(ch)

        def fmt(groups: dict[str, list[str]]) -> str:
            return "; ".join(f"{name}" + (f" (canali {', '.join(c for c in chs if c)})" if any(chs) else "")
                             for name, chs in groups.items()) or "nessuno"
        out.append(f"  eventi che arrivano al Raspberry: {fmt(sent)}")
        out.append(f"  eventi che non arrivano (manca 'Notifica al centro di sorveglianza'): {fmt(unsent)}")
    faces, _ = get(cam, "/ISAPI/Intelligent/FDLib/capabilities")
    out.append("  archivio volti (riconoscimento facciale nell'apparecchio): "
               + ("presente" if faces is not None else "non disponibile"))
    return out


def _isapi(cam: Camera, method: str, path: str, body: str | None = None, files: dict | None = None,
           timeout: float = 30) -> tuple[int, bytes]:
    import requests
    from requests.auth import HTTPDigestAuth
    headers = {"Content-Type": "application/xml; charset=UTF-8"} if body is not None else None
    r = requests.request(method, f"http://{cam.host}{path}", data=body.encode("utf-8") if body is not None else None,
                         files=files, headers=headers, auth=HTTPDigestAuth(cam.user, cam.password), timeout=timeout)
    return r.status_code, r.content


def isapi_error(status: int, content: bytes) -> str | None:
    """None se la risposta ISAPI dice OK, altrimenti il motivo (statusString e subStatusCode)."""
    try:
        root = _strip_ns(ET.fromstring(content))
    except ET.ParseError:
        return None if status == 200 else f"HTTP {status}"
    if root.tag == "ResponseStatus" and (root.findtext("statusCode") or "").strip() != "1":
        parts = [(root.findtext(k) or "").strip() for k in ("statusString", "subStatusCode")]
        return " ".join(p for p in parts + [f"(HTTP {status})"] if p)
    return None if status == 200 else f"HTTP {status}"


FIRMWARE_MIN_BYTES = 4 * 1024 * 1024


def firmware_version(cam: Camera) -> str:
    """Versione e data del firmware ("" se la telecamera non risponde)."""
    root, _ = _get_xml(cam, "/ISAPI/System/deviceInfo")
    if root is None:
        return ""
    return " ".join(p for p in ((root.findtext("firmwareVersion") or "").strip(),
                                (root.findtext("firmwareReleasedDate") or "").strip()) if p)


def upgrade_outcome(status: int, content: bytes) -> bool | None:
    """Risposta al caricamento del firmware: True = installato, serve il riavvio; False = installato; None = errore."""
    try:
        root = _strip_ns(ET.fromstring(content))
    except ET.ParseError:
        return False if status == 200 else None
    code = (root.findtext("statusCode") or "").strip()
    if code == "7" or (root.findtext("subStatusCode") or "").strip().lower() == "rebootrequired":
        return True
    if code == "1" or (status == 200 and root.tag != "ResponseStatus"):
        return False
    return None


def upgrade_firmware(cam: Camera, path: str, out=print, wait_s: float = 900) -> str:
    """Carica il firmware (.dav), riavvia la telecamera se serve e aspetta che torni con la nuova versione."""
    if not path.lower().endswith(".dav"):
        raise RuntimeError("serve il file del firmware Hikvision (.dav, di solito digicap.dav)")
    size = os.path.getsize(path)
    if size < FIRMWARE_MIN_BYTES:
        raise RuntimeError(f"file troppo piccolo per essere un firmware ({size} byte)")
    import requests
    from requests.auth import HTTPDigestAuth
    before = firmware_version(cam)
    if not before:
        raise RuntimeError("telecamera non raggiungibile")
    status, _ = _get_xml(cam, "/ISAPI/System/upgradeStatus")
    if status is not None and (status.findtext("upgrading") or "").strip() == "true":
        raise RuntimeError("la telecamera sta gia' aggiornando")
    out(f"{cam.name}: firmware attuale {before}; carico {os.path.basename(path)} ({size // 2**20} MB)")
    session = requests.Session()
    session.auth = HTTPDigestAuth(cam.user, cam.password)
    session.get(f"http://{cam.host}/ISAPI/System/deviceInfo", timeout=10)   # autenticazione prima: il file parte una volta
    with open(path, "rb") as f:
        # un solo tempo massimo: requests usa quello "di connessione" anche mentre invia il file, e la
        # telecamera smette di leggere per qualche secondo mentre lo riceve (con 10 s il caricamento cade)
        r = session.put(f"http://{cam.host}/ISAPI/System/updateFirmware", data=f,
                        headers={"Content-Type": "application/octet-stream"}, timeout=wait_s)
    outcome = upgrade_outcome(r.status_code, r.content)
    if outcome is None:
        raise RuntimeError(f"aggiornamento rifiutato dalla telecamera: {isapi_error(r.status_code, r.content)}")
    out("firmware accettato e scritto" + ("; riavvio la telecamera" if outcome else "; la telecamera si riavvia"))
    if outcome:
        try:
            session.put(f"http://{cam.host}/ISAPI/System/reboot", timeout=30)
        except requests.RequestException:
            pass                                             # puo' chiudere il collegamento mentre si spegne
    deadline = time.monotonic() + wait_s
    seen_down = False
    while time.monotonic() < deadline:
        time.sleep(10)
        version = firmware_version(cam)
        if not version:
            if not seen_down:
                out("telecamera in riavvio...")
            seen_down = True
        elif version != before:
            out(f"telecamera di nuovo in linea con il firmware {version}")
            return version
        elif seen_down:
            raise RuntimeError(f"la telecamera e' ripartita ma ha ancora {version}")
    raise RuntimeError("la telecamera non e' tornata con la nuova versione entro il tempo previsto: controllala")


def _write(cam: Camera, method: str, path: str, body: str | None = None, files: dict | None = None) -> bytes:
    status, content = _isapi(cam, method, path, body, files)
    err = isapi_error(status, content)
    if err:
        raise RuntimeError(f"{method} {path.split('?')[0]}: {err}")
    return content


def set_clock(cam: Camera, ntp_host: str, tz: str) -> str:
    """Ora automatica dal server NTP (ogni 60 minuti) e fuso orario; restituisce l'ora letta dopo."""
    _write(cam, "PUT", "/ISAPI/System/time/ntpServers/1",
           f'<NTPServer version="2.0" xmlns="{ISAPI_NS}"><id>1</id><addressingFormatType>hostname'
           f"</addressingFormatType><hostName>{escape(ntp_host)}</hostName><portNo>123</portNo>"
           f"<synchronizeInterval>60</synchronizeInterval></NTPServer>")
    _write(cam, "PUT", "/ISAPI/System/time",
           f'<Time version="2.0" xmlns="{ISAPI_NS}"><timeMode>NTP</timeMode><timeZone>{escape(tz)}</timeZone></Time>')
    root, err = _get_xml(cam, "/ISAPI/System/time")
    return (root.findtext("localTime") or "?") if root is not None else f"non leggibile ({err})"


def face_libraries(cam: Camera) -> list[dict]:
    root, err = _get_xml(cam, "/ISAPI/Intelligent/FDLib")
    if root is None:
        raise RuntimeError(f"archivi dei volti non leggibili ({err})")
    return [{k: (lib.findtext(k) or "").strip() for k in ("id", "FDID", "name")} for lib in root.iter("FDLibBaseCfg")]


def library_id(cam: Camera, name: str, create: bool = False) -> str:
    """FDID dell'archivio con questo nome; se manca e create=True lo crea."""
    libs = face_libraries(cam)
    for lib in libs:
        if lib["name"] == name:
            return lib["FDID"]
    if not create:
        raise RuntimeError(f"archivio '{name}' non trovato: {', '.join(lib['name'] for lib in libs) or 'nessuno'}")
    used = {int(lib["id"]) for lib in libs if lib["id"].isdigit()}
    new_id = next(i for i in range(1, 17) if i not in used)
    _write(cam, "POST", "/ISAPI/Intelligent/FDLib",
           f'<CreateFDLibList version="2.0" xmlns="{ISAPI_NS}"><CreateFDLib><id>{new_id}</id>'
           f"<name>{escape(name)}</name></CreateFDLib></CreateFDLibList>")
    return library_id(cam, name)


def prepare_photo(path: str) -> bytes:
    """JPEG semplice (non progressivo), lato massimo 1280 px, sotto MAX_FACE_KB: cosi' il registratore lo accetta."""
    with open(path, "rb") as f:
        data = f.read()
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return data
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((1280, 1280))
    for quality in (92, 85, 75, 65):
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality, progressive=False, optimize=True)
        if out.tell() <= MAX_FACE_KB * 1024:
            break
    return out.getvalue()


def upload_face(cam: Camera, fdid: str, person: str, jpeg: bytes) -> str:
    """Carica una foto nell'archivio; restituisce il PID assegnato dal registratore (se lo indica)."""
    xml = (f'<PictureUploadData version="2.0" xmlns="{ISAPI_NS}"><FDID>{escape(fdid)}</FDID><FaceAppendData>'
           f"<name>{escape(person)}</name></FaceAppendData></PictureUploadData>")
    content = _write(cam, "POST", "/ISAPI/Intelligent/FDLib/pictureUpload",
                     files={"PictureUploadData": (None, xml, "text/xml"),
                            "importImage": ("volto.jpg", jpeg, "image/jpeg")})
    try:
        return (_strip_ns(ET.fromstring(content)).findtext(".//PID") or "").strip()
    except ET.ParseError:
        return ""


MODELING = {"success": "pronto", "failed": "non riuscito (foto da cambiare)", "none": "in attesa",
            "modeling": "pronto", "unmodeled": "in attesa", "modelingfailed": "non riuscito (foto da cambiare)"}


def parse_face_search(content: bytes) -> list[dict]:
    """Foto di un archivio: PID, nome, stato del modello del volto (in chiaro) e qualita' del volto."""
    root = _strip_ns(ET.fromstring(content))
    out = []
    for m in root.iter("MatchElement"):
        status = (m.findtext("ModelingStatus/status") or m.findtext("modelStatus") or "").strip()
        out.append({"PID": (m.findtext("PID") or "").strip(), "name": (m.findtext(".//name") or "").strip(),
                    "modello": MODELING.get(status.lower(), status or "?"),
                    "qualita": (m.findtext("faceScore") or "").strip()})
    return out


def library_faces(cam: Camera, fdid: str) -> list[dict]:
    content = _write(cam, "POST", "/ISAPI/Intelligent/FDLib/FDSearch",
                     f'<FDSearchDescription version="2.0" xmlns="{ISAPI_NS}"><searchID>{uuid.uuid4().hex[:32]}'
                     f"</searchID><searchResultPosition>0</searchResultPosition><maxResults>200</maxResults>"
                     f"<FDID>{escape(fdid)}</FDID></FDSearchDescription>")
    return parse_face_search(content)


def _modify(cam: Camera, path: str, change) -> None:
    """Legge una configurazione, la cambia con change(root, q) e la riscrive: i campi non toccati restano."""
    status, content = _isapi(cam, "GET", path)
    if status != 200:
        raise RuntimeError(f"{path} non leggibile (HTTP {status})")
    root = ET.fromstring(content)
    ns = root.tag[1:].split("}")[0] if root.tag.startswith("{") else ""
    ET.register_namespace("", ns)
    change(root, (lambda tag: f"{{{ns}}}{tag}") if ns else (lambda tag: tag))
    _write(cam, "PUT", path, ET.tostring(root, encoding="unicode"))


def set_face_contrast(cam: Camera, channel: str, fdid: str | None, threshold: int) -> None:
    """Accende (fdid) o spegne (None) il riconoscimento facciale di un canale, lasciando il resto com'e'."""
    def change(root, q):
        fc = root.find(q("FaceContrast"))
        if fc is None:
            raise RuntimeError("risposta inattesa: manca FaceContrast")
        fc.find(q("enable")).text = "true" if fdid else "false"
        if fc.find(q("thresholdValue")) is not None:
            fc.find(q("thresholdValue")).text = str(threshold)
        libs = fc.find(q("FDLibList"))
        if fdid and libs is not None:
            for child in list(libs):
                libs.remove(child)
            lib = ET.SubElement(libs, q("FDLib"))
            ET.SubElement(lib, q("id")).text = "1"
            ET.SubElement(lib, q("FDID")).text = fdid
            ET.SubElement(lib, q("thresholdValue")).text = str(threshold)
    _modify(cam, f"/ISAPI/Intelligent/channels/{channel}/faceContrast", change)


def set_face_detection(cam: Camera, channel: str, enabled: bool, sensitivity: int) -> None:
    """Rilevamento dei volti del canale: senza, il registratore non cerca volti e il confronto non parte mai."""
    def change(root, q):
        root.find(q("enabled")).text = "true" if enabled else "false"
        if root.find(q("sensitivityLevel")) is not None:
            root.find(q("sensitivityLevel")).text = str(sensitivity)
    _modify(cam, f"/ISAPI/Smart/FaceDetect/{channel}", change)


def set_trigger_center(cam: Camera, trigger_id: str, on: bool) -> None:
    """Aggiunge o toglie la "Notifica al centro di sorveglianza": cosi' l'evento arriva (o no) al Raspberry."""
    def change(root, q):
        lst = root.find(q("EventTriggerNotificationList"))
        if lst is None:
            lst = ET.SubElement(root, q("EventTriggerNotificationList"))
        centers = [n for n in lst.findall(q("EventTriggerNotification"))
                   if (n.findtext(q("notificationMethod")) or "").strip() == "center"]
        if on and not centers:
            n = ET.SubElement(lst, q("EventTriggerNotification"))
            ET.SubElement(n, q("id")).text = "center"
            ET.SubElement(n, q("notificationMethod")).text = "center"
        for n in centers if not on else []:
            lst.remove(n)
    _modify(cam, f"/ISAPI/Event/triggers/{trigger_id}", change)


def parse_face_results(content: bytes) -> list[dict]:
    """Storico dei confronti dei volti (FCSearch): ora, canale, persona riconosciuta e somiglianza."""
    root = _strip_ns(ET.fromstring(content))
    out = []
    for m in root.iter("MatchElement"):
        person, similarity = face_match(m)
        out.append({"ora": (m.findtext(".//snapTime") or m.findtext(".//time") or "?").strip(),
                    "canale": (m.findtext(".//channelID") or m.findtext(".//channel") or "?").strip(),
                    "persona": person, "somiglianza": similarity})
    return out


def face_results(cam: Camera, hours: float, raw: bool = False) -> list[dict] | str:
    """Confronti fatti dal registratore nelle ultime ore (anche quelli non arrivati come eventi)."""
    now = time.time()
    fmt = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + time.strftime("%z")[:3] + ":00"
    body = (f'<FCSearchDescription version="2.0" xmlns="{ISAPI_NS}"><searchID>{uuid.uuid4().hex[:32]}</searchID>'
            f"<searchResultPosition>0</searchResultPosition><maxResults>100</maxResults>"
            f"<snapStartTime>{fmt(now - (hours + 2) * 3600)}</snapStartTime>"      # 2 ore di margine: fuso
            f"<snapEndTime>{fmt(now + 2 * 3600)}</snapEndTime><eventType>unlimit</eventType></FCSearchDescription>")
    content = _write(cam, "POST", "/ISAPI/Intelligent/FDLib/FCSearch", body)
    if raw:
        root = _strip_ns(ET.fromstring(content))
        ET.indent(root)
        return ET.tostring(redact_xml(root), encoding="unicode")
    return parse_face_results(content)


def analysis_engines(cam: Camera) -> list[dict]:
    root, err = _get_xml(cam, "/ISAPI/Intelligent/analysisEngines")
    if root is None:
        raise RuntimeError(f"motori di analisi non leggibili ({err})")
    return [{k: (e.findtext(k) or "").strip() for k in ("id", "mode", "state")} for e in root.iter("AnalysisEngine")]


def set_engine_mode(cam: Camera, mode: str) -> None:
    """Assegna i motori di analisi del registratore (es. face_recognition per il riconoscimento facciale)."""
    def change(engine, q):
        engine.find(q("mode")).text = mode
        for readonly in ("state", "utilization"):          # stato e carico li decide il registratore
            el = engine.find(q(readonly))
            if el is not None:
                engine.remove(el)
    for engine in analysis_engines(cam):                    # l'elenco non si scrive: un motore alla volta
        path = f"/ISAPI/Intelligent/analysisEngines/{engine['id']}"
        status, _ = _isapi(cam, "GET", path)
        if status == 200:
            _modify(cam, path, change)
        else:
            _write(cam, "PUT", path, f'<AnalysisEngine version="2.0" xmlns="{ISAPI_NS}"><id>{escape(engine["id"])}'
                                     f"</id><mode>{escape(mode)}</mode></AnalysisEngine>")


def faces_command(args, cam: Camera) -> None:
    if args.azione == "elenco":
        for lib in face_libraries(cam) or [{"name": None}]:
            if lib["name"] is None:
                print("nessun archivio di volti")
                break
            faces = library_faces(cam, lib["FDID"])
            print(f"archivio {lib['name']} (FDID {lib['FDID']}): {len(faces)} foto")
            for face in faces:
                print(f"  {face['name']}  PID {face['PID']}  modello: {face['modello']}"
                      + (f", qualita' del volto {face['qualita']}/100" if face["qualita"] else ""))
        return
    if args.azione == "aggiungi":
        fdid = library_id(cam, args.archivio, create=True)
        for photo in args.foto:
            pid = upload_face(cam, fdid, args.persona, prepare_photo(photo))
            print(f"{os.path.basename(photo)}: caricata" + (f" (PID {pid})" if pid else ""))
        return
    if args.azione == "togli":
        _write(cam, "DELETE", f"/ISAPI/Intelligent/FDLib/{library_id(cam, args.archivio)}/picture/{args.pid}")
        print(f"foto {args.pid} tolta dall'archivio {args.archivio}")
        return
    if args.azione == "rilevamento":
        set_face_detection(cam, args.canale, not args.spento, args.sensibilita)
        set_trigger_center(cam, f"facedetection-{args.canale}", not args.spento)   # il Raspberry sa quando vede un volto
        print(f"rilevamento dei volti sul canale {args.canale}: "
              + ("spento" if args.spento else f"acceso, sensibilita' {args.sensibilita}, avvisa il Raspberry"))
        return
    if args.azione == "risultati":
        found = face_results(cam, args.ore, raw=args.xml)
        if isinstance(found, str):
            print(found)
            return
        if not found:
            print(f"nessun volto confrontato nelle ultime {args.ore:g} ore")
        for r in found:
            sim = f"{r['somiglianza']:.0f}%" if r["somiglianza"] is not None else "?"
            print(f"{r['ora']}  canale {r['canale']}  {r['persona'] or 'sconosciuto'}  somiglianza {sim}")
        return
    if args.azione == "motore":
        if args.modo:
            set_engine_mode(cam, args.modo)
        for e in analysis_engines(cam):
            print(f"motore {e['id']}: {e['mode']}, stato {e['state']}")
        return
    fdid = None if args.spento else library_id(cam, args.archivio)
    set_face_contrast(cam, args.canale, fdid, args.soglia)
    print(f"riconoscimento facciale sul canale {args.canale}: "
          + ("spento" if args.spento else f"acceso con l'archivio {args.archivio}, soglia {args.soglia}%"))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Ponte telecamere Hikvision -> Loxone")
    ap.add_argument("--config", default=CONFIG_PATH)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run")
    a = sub.add_parser("configura", help="chiede e verifica utenti e password, poi salva la configurazione")
    a.add_argument("--miniserver", default="", help="IP del Miniserver proposto")
    a.add_argument("--telecamere", default="", help="IP delle telecamere separati da virgola")
    sub.add_parser("info", help="modello, telecamere collegate, rilevazioni attive ed eventi inviati (solo letture)")
    a = sub.add_parser("leggi", help="mostra una risposta ISAPI (solo letture, password nascoste)")
    a.add_argument("telecamera")
    a.add_argument("percorso", help="es. /ISAPI/Smart/LineDetection/1")
    a.add_argument("--attesa", type=float, default=10, help="secondi massimi di attesa della risposta")
    a = sub.add_parser("ora", help="ora automatica (NTP) e fuso orario")
    a.add_argument("telecamera")
    a.add_argument("--ntp", default="pool.ntp.org")
    a.add_argument("--fuso", default=CET_TZ, help="fuso nel formato Hikvision (predefinito: Europa centrale)")
    a = sub.add_parser("volti", help="archivio dei volti e riconoscimento facciale di un registratore")
    faces = a.add_subparsers(dest="azione", required=True)
    f = faces.add_parser("elenco", help="archivi e foto, con lo stato del modello del volto")
    f.add_argument("telecamera")
    f = faces.add_parser("aggiungi", help="carica foto di una persona (crea l'archivio se manca)")
    f.add_argument("telecamera")
    f.add_argument("archivio")
    f.add_argument("persona")
    f.add_argument("foto", nargs="+")
    f = faces.add_parser("togli", help="toglie una foto dall'archivio")
    f.add_argument("telecamera")
    f.add_argument("archivio")
    f.add_argument("pid")
    f = faces.add_parser("motore", help="mostra o cambia l'uso del motore di analisi (es. face_recognition)")
    f.add_argument("telecamera")
    f.add_argument("modo", nargs="?")
    f = faces.add_parser("rilevamento", help="accende o spegne la ricerca dei volti nelle immagini di un canale")
    f.add_argument("telecamera")
    f.add_argument("canale")
    f.add_argument("--sensibilita", type=int, default=3, choices=range(1, 6), metavar="1-5")
    f.add_argument("--spento", action="store_true")
    f = faces.add_parser("risultati", help="confronti dei volti fatti dal registratore nelle ultime ore")
    f.add_argument("telecamera")
    f.add_argument("--ore", type=float, default=2)
    f.add_argument("--xml", action="store_true", help="mostra la risposta completa (password nascoste)")
    f = faces.add_parser("confronto", help="accende o spegne il riconoscimento su un canale")
    f.add_argument("telecamera")
    f.add_argument("canale")
    f.add_argument("archivio")
    f.add_argument("--soglia", type=int, default=80, help="somiglianza minima in percento (predefinito 80)")
    f.add_argument("--spento", action="store_true")
    a = sub.add_parser("regole", help="elenca, aggiunge o toglie regole di una telecamera (le modifiche con sudo)")
    a.add_argument("telecamera")
    a.add_argument("--aggiungi", metavar="TOML", help='es. {type="linedetection", region=1, inputs=["scale"]}')
    a.add_argument("--togli", type=int, metavar="N", help="numero della regola da togliere")
    a.add_argument("--prova", nargs=2, metavar=("N", "si|no"), help="regola N solo nel log (si) o attiva (no)")
    a = sub.add_parser("firmware", help="aggiorna il firmware di una telecamera (si riavvia)")
    a.add_argument("telecamera")
    a.add_argument("file", help="file del firmware Hikvision (digicap.dav)")
    a.add_argument("--conferma", action="store_true", help="senza, mostra solo cosa farebbe")
    a = sub.add_parser("volti-locali", help="riconoscimento facciale sul Raspberry con le foto della telecamera")
    local = a.add_subparsers(dest="azione", required=True)
    f = local.add_parser("impara", help="impara un volto da foto (jpg)")
    f.add_argument("persona")
    f.add_argument("foto", nargs="+")
    f = local.add_parser("impara-da", help="impara un volto dalle foto della telecamera (la persona ci passa davanti)")
    f.add_argument("telecamera")
    f.add_argument("persona")
    f.add_argument("--secondi", type=float, default=10)
    f.add_argument("--massimo", type=int, default=8, help="impronte nuove al massimo")
    f.add_argument("--forza", action="store_true", help="impara anche volti poco somiglianti a quelli gia' noti")
    local.add_parser("elenco", help="volti imparati")
    f = local.add_parser("dimentica")
    f.add_argument("persona")
    f = local.add_parser("prova", help="foto della telecamera adesso: chi riconosce e con che somiglianza")
    f.add_argument("telecamera")
    f.add_argument("--secondi", type=float, default=6)
    a = sub.add_parser("eventi", help="mostra gli eventi di una telecamera")
    a.add_argument("telecamera")
    a.add_argument("secondi", nargs="?", type=float, default=60)
    a = sub.add_parser("prova", help="manda il comando a un ingresso di Loxone")
    a.add_argument("ingresso")
    args = ap.parse_args(argv)

    def camera(name: str) -> Camera:
        cam = next((c for c in cams if c.name.lower() == name.lower()), None)
        if cam is None:
            raise SystemExit(f"telecamera '{name}' non configurata: {', '.join(c.name for c in cams)}")
        return cam
    if args.cmd == "configura":
        try:
            configure(args.config, args.miniserver, [h.strip() for h in args.telecamere.split(",") if h.strip()])
        except (KeyboardInterrupt, EOFError):
            raise SystemExit("\nannullato: niente e' stato salvato")
        return
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    with open(args.config, encoding="utf-8") as f:
        ms, cams = load_config(f.read())
    if args.cmd == "info":
        for cam in cams:
            print("\n".join(describe_device(cam)))
        return
    if args.cmd == "leggi":
        if PRIVATE_PATH_RE.search(args.percorso):
            raise SystemExit("percorso con utenti o dati di accesso: non lo leggo")
        root, err = _get_xml(camera(args.telecamera), args.percorso, args.attesa)
        if root is None:
            raise SystemExit(f"non leggibile ({err})")
        ET.indent(root)
        print(ET.tostring(redact_xml(root), encoding="unicode"))
        return
    if args.cmd == "regole":
        cam = camera(args.telecamera)
        if args.aggiungi or args.togli or args.prova:
            try:
                change_rules(cam, args.aggiungi, args.togli, args.prova)
            except (ValueError, IndexError, tomllib.TOMLDecodeError) as e:
                raise SystemExit(f"ERRORE: {e}")
            if os.geteuid() != 0:
                raise SystemExit("per salvare le regole esegui con sudo")
            write_private(args.config, dump_config(ms, cams))
            subprocess.run(["systemctl", "restart", "hikvision-loxone.service"], check=False)
            print("regole salvate, servizio riavviato")
        for i, rule in enumerate(cam.rules, 1):
            print(f"{i}. {describe_rule(rule)}")
        if not cam.rules:
            print(f"{cam.name}: nessuna regola")
        return
    if args.cmd == "volti-locali":
        try:
            local_faces_command(args, camera)
        except (RuntimeError, OSError) as e:
            raise SystemExit(f"ERRORE: {e}")
        return
    if args.cmd == "firmware":
        cam = camera(args.telecamera)
        if not args.conferma:
            print(f"{cam.name}: firmware attuale {firmware_version(cam) or 'non leggibile'}; "
                  f"con --conferma carico {args.file}, la telecamera si riavvia (qualche minuto senza video)")
            return
        try:
            upgrade_firmware(cam, args.file)
        except (RuntimeError, OSError) as e:
            raise SystemExit(f"ERRORE: {e}")
        return
    if args.cmd in ("ora", "volti"):
        try:
            if args.cmd == "ora":
                print(f"{args.telecamera}: ora automatica da {args.ntp}, ora attuale {set_clock(camera(args.telecamera), args.ntp, args.fuso)}")
            else:
                faces_command(args, camera(args.telecamera))
        except (RuntimeError, OSError) as e:
            raise SystemExit(f"ERRORE: {e}")
        return
    loxone = Loxone(ms)
    if args.cmd == "prova":
        sys.exit(0 if loxone.trigger(args.ingresso) else 1)
    if args.cmd == "eventi":
        cam = camera(args.telecamera)
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
