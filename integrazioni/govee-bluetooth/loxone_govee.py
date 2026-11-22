#!/usr/bin/env python3
"""Ponte Loxone -> luci LED Govee via Bluetooth (BLE), pensato per un Raspberry Pi.

Il Miniserver manda comandi di testo via UDP (uscita virtuale "/dev/udp/<raspberry>/55000"):
  on | accendi led          accende
  off | spegni led          spegne
  brightness:<0-100>        luminosita' in percentuale (0 oppure "off" = spegne)
  color:<R>-<G>-<B>         colore, valori 0-255 (0-0-0 = spegne)
  <nome scena>              scena Govee, es. Movie, Romantic, Snowflake (vedi GOVEE_SCENES), oppure una
  scena:<nome scena>        scena del catalogo del modello (Aurora, Fire, Forest...: vedi "catalogo")
  test                      rosso, verde, blu: serve a controllare il collegamento
Piu' comandi nello stesso pacchetto si separano con ';' o a capo; un pacchetto piu' lungo di 256 byte si
ignora. I comandi che arrivano insieme si uniscono: il colore nero e la luminosita' 0 spengono solo se nel
gruppo non c'e' altro che accende (passando alla scena "Neve", Loxone manda "color:0-0-0" e subito dopo
"Snowflake").
Colori tenui: Loxone manda il colore gia' attenuato (rosso al 30% = "color:76-0-0"); il ponte lo manda pieno
(255-0-0) e abbassa invece la luminosita' della striscia, cosi' anche i colori deboli restano precisi.

Scene: quelle "classiche" (Sunrise, Movie, Snowflake...) sono un solo comando con un codice; quelle animate
delle strisce RGBIC (Aurora, Fire...) vogliono prima i dati dell'animazione in piu' pacchetti 0xA3. Il catalogo
delle scene di un modello (nomi, codici, dati) e' pubblico nella libreria di Govee: "catalogo" lo scarica UNA
volta e lo salva in GOVEE_SCENE_FILE; poi il ponte lavora solo in locale.

Protocollo Govee (documentato da vari progetti open source): pacchetti da 20 byte scritti sulla
caratteristica 00010203-0405-0607-0809-0a0b0c0d2b11; il primo byte e' 0x33 (comando) o 0xAA
(richiesta, usata anche come keep-alive), l'ultimo e' lo XOR dei 19 precedenti. La striscia accetta un
solo collegamento alla volta: se l'app Govee del telefono e' collegata, il Raspberry non riesce a entrare.
Il Bluetooth si usa con la libreria bleak (https://github.com/hbldh/bleak, licenza MIT).

Configurazione da variabili d'ambiente (sul Raspberry: /etc/default/loxone-govee):
  GOVEE_ADDRESS          indirizzo Bluetooth della luce (obbligatorio: senza, il ponte esce con il codice 78
                         e il servizio non viene riavviato); piu' luci separate da virgola
  UDP_PORT               porta su cui ascoltare (55000)
  ALLOW_FROM             IP ammessi, separati da virgola, es. l'IP del Miniserver e 127.0.0.1 (per "send");
                         vuoto = tutti: chiunque in rete puo' comandare le luci (il ponte lo scrive all'avvio)
  GOVEE_COLOR_MODE       variante del comando colore: 02 (classico), 0d, 15 (strisce RGBIC)
  GOVEE_BRIGHTNESS_MAX   valore massimo della luminosita' per il modello: 255 oppure 100
  GOVEE_SCENES           scene aggiuntive o corrette, es. "movie=4,romantic=7,snowflake=15"
  GOVEE_SCENE_FILE       catalogo delle scene del modello (/var/lib/loxone-govee/scene.json)

Uso:
  loxone_govee.py run                   avvia il ponte (e' cio' che fa il servizio systemd)
  loxone_govee.py scan                  cerca le luci Govee vicine
  loxone_govee.py send "color:255-0-0"  manda un comando al ponte in esecuzione (prova); il ponte lo accetta
                                        solo se 127.0.0.1 e' in ALLOW_FROM (o se ALLOW_FROM e' vuoto)
  loxone_govee.py query                 legge lo stato della luce (con il servizio fermo)
  loxone_govee.py prova "color:255-0-0" manda comandi direttamente alla luce e ne legge l'effetto
                                        (query e prova vogliono GOVEE_ADDRESS e le varianti del modello
                                        nell'ambiente: set -a; . /etc/default/loxone-govee; set +a)
  loxone_govee.py catalogo H6126        scarica il catalogo pubblico delle scene del modello (una volta)
  loxone_govee.py scene                 elenca le scene del catalogo
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import signal
import socket
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

WRITE_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
NOTIFY_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"
KEEPALIVE_S = 2.0      # la striscia chiude il collegamento se non riceve niente per un po'
SEND_GAP_S = 0.08      # comandi troppo ravvicinati possono andare persi
BURST_S = 0.15         # attesa dopo il primo comando, per unire quelli che Loxone manda insieme
TEST_STEP_S = 0.8
MAX_DATAGRAM = 256     # i comandi di Loxone stanno in poche decine di byte: un pacchetto piu' lungo si ignora
EXIT_CONFIG = 78       # EX_CONFIG (sysexits.h): manca la configurazione; il servizio non viene riavviato
GOVEE_NAME_PREFIXES = ("GBK_", "ihoment_", "Govee_", "Minger_", "GVH")

# scene "classiche" dei modelli Govee Bluetooth (comando 0x05, modo 0x04): da verificare sul modello
# (sulla H6126 le stesse 12 sono nel catalogo come scene senza dati: Sunrise 0 ... Rainbow 22)
DEFAULT_SCENES = {"sunrise": 0, "sunset": 1, "movie": 4, "dating": 5, "romantic": 7, "blinking": 8,
                  "candlelight": 9, "snowflake": 15}
SCENE_FILE = "/var/lib/loxone-govee/scene.json"
# il comando "<v>" di Loxone legato al blocco di stato delle scene manda "0" quando il testo torna vuoto (si lascia
# un'atmosfera con una scena Govee): vuol dire "nessuna scena", non e' un comando da eseguire
NO_SCENE = "0"
# libreria pubblica delle scene di Govee (la usa l'app): niente account, si scarica una volta con "catalogo"
LIBRARY_URL = "https://app2.govee.com/appsku/v1/light-effect-libraries?sku={sku}"
LIBRARY_HEADERS = {"AppVersion": "6.5.00", "User-Agent": "GoveeHome/6.5.00 (loxone-govee)"}

log = logging.getLogger("loxone-govee")


def scene_key(name: str) -> str:
    """'Snow flake', 'snowflake', 'Starry  Sky' -> chiave senza spazi e maiuscole (i nomi Govee variano)."""
    return re.sub(r"[\s_\-\xa0]+", "", name).lower()


def frame(head: int, cmd: int, payload: bytes | list[int] = b"") -> bytes:
    """Pacchetto Govee da 20 byte: intestazione, comando, dati, zeri, XOR finale."""
    body = bytes([head, cmd]) + bytes(payload)
    if len(body) > 19:
        raise ValueError("dati troppo lunghi per un pacchetto Govee")
    body = body.ljust(19, b"\0")
    checksum = 0
    for b in body:
        checksum ^= b
    return body + bytes([checksum])


POWER_ON = frame(0x33, 0x01, [0x01])
POWER_OFF = frame(0x33, 0x01, [0x00])
KEEPALIVE = frame(0xAA, 0x01)
QUERIES = {"accensione": frame(0xAA, 0x01), "luminosita": frame(0xAA, 0x04), "modo e colore": frame(0xAA, 0x05),
           "firmware": frame(0xAA, 0x06)}


@dataclass
class Settings:
    address: str = ""
    port: int = 55000
    allow_from: set[str] = field(default_factory=set)
    color_mode: str = "02"
    brightness_max: int = 255
    scenes: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SCENES))
    scene_file: str = SCENE_FILE
    catalog: dict[str, "Scene"] = field(default_factory=dict)   # chiave (scene_key) -> scena del catalogo

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        s = cls(address=env.get("GOVEE_ADDRESS", "").strip().upper(),
                port=int(env.get("UDP_PORT", "55000")),
                allow_from={ip.strip() for ip in env.get("ALLOW_FROM", "").split(",") if ip.strip()},
                color_mode=env.get("GOVEE_COLOR_MODE", "02").strip().lower(),
                brightness_max=int(env.get("GOVEE_BRIGHTNESS_MAX", "255")),
                scene_file=env.get("GOVEE_SCENE_FILE", "").strip() or SCENE_FILE)
        for item in env.get("GOVEE_SCENES", "").split(","):
            if "=" in item:
                name, value = item.split("=", 1)
                s.scenes[scene_key(name)] = int(value.strip(), 0)
        if s.color_mode not in ("02", "0d", "15"):
            raise ValueError(f"GOVEE_COLOR_MODE non valido: {s.color_mode} (usa 02, 0d o 15)")
        if s.brightness_max not in (100, 255):
            raise ValueError("GOVEE_BRIGHTNESS_MAX deve essere 100 o 255")
        # il catalogo e' facoltativo: se manca o e' rovinato il ponte (e ogni altro comando) funziona lo stesso
        s.catalog = read_catalog(Path(s.scene_file), wanted=bool(env.get("GOVEE_SCENE_FILE", "").strip()))
        return s

    @property
    def addresses(self) -> list[str]:
        """GOVEE_ADDRESS puo' elencare piu' luci separate da virgola: ricevono tutte gli stessi comandi."""
        return [a.strip() for a in self.address.split(",") if a.strip()]


def color_frame(mode: str, r: int, g: int, b: int) -> bytes:
    if mode == "02":
        return frame(0x33, 0x05, [0x02, r, g, b])
    if mode == "0d":
        return frame(0x33, 0x05, [0x0D, r, g, b])
    return frame(0x33, 0x05, [0x15, 0x01, r, g, b, 0, 0, 0, 0, 0, 0xFF, 0x7F])  # tutti i segmenti


def _clamp(value: float, low: int, high: int) -> int:
    return max(low, min(high, int(round(value))))


# --- scene del catalogo ---
@dataclass
class Scene:
    name: str              # nome nel catalogo Govee (inglese), es. "Aurora"
    code: int              # codice della scena: 33 05 04 <basso> <alto>
    param: bytes = b""     # dati dell'animazione (vuoto = scena "classica", basta il codice)
    category: str = ""


def multi_packet(head: int, kind: int, data: bytes) -> list[bytes]:
    """Dati lunghi in piu' pacchetti Govee (come fa l'app per le scene animate, head 0xA3, tipo 0x02).

    Primo: head 00 01 <totale pacchetti> <tipo> + 14 byte; poi head <n> + 17 byte; ultimo: head FF + il resto
    (anche vuoto). Ogni pacchetto ha il suo XOR finale.
    """
    first, rest = data[:14], data[14:]
    pieces = [rest[i:i + 17] for i in range(0, len(rest), 17)] or [b""]
    total = 1 + len(pieces)
    if total > 0xFE:
        raise ValueError("dati della scena troppo lunghi")
    out = [frame(head, 0x00, bytes([0x01, total, kind]) + first)]
    out += [frame(head, i, piece) for i, piece in enumerate(pieces[:-1], start=1)]
    out.append(frame(head, 0xFF, pieces[-1]))
    return out


def scene_frames(scene: Scene) -> list[bytes]:
    """Pacchetti per attivare una scena: i dati dell'animazione (se ci sono), poi il codice."""
    frames = multi_packet(0xA3, 0x02, scene.param) if scene.param else []
    return frames + [frame(0x33, 0x05, [0x04, scene.code & 0xFF, (scene.code >> 8) & 0xFF])]


def build_catalog(library: dict, sku: str, cmd_version: int = 0) -> dict:
    """Libreria di Govee (light-effect-libraries) -> catalogo compatto da salvare in GOVEE_SCENE_FILE.

    Una scena puo' avere piu' versioni (A, B...) per firmware diversi: si tiene la prima con il cmdVersion
    chiesto (0 = firmware piu' vecchi, come le H6126 con firmware 2.06). Si saltano le scene musicali (tipo 1).
    """
    scenes, seen = [], set()
    for cat in library.get("data", {}).get("categories", []):
        for sc in cat.get("scenes", []):
            name = re.sub(r"\s+", " ", (sc.get("sceneName") or "").replace("\xa0", " ")).strip()
            if not name or scene_key(name) in seen:
                continue
            for eff in sc.get("lightEffects") or []:
                if eff.get("sceneType") not in (0, 2) or int(eff.get("cmdVersion") or 0) != cmd_version:
                    continue
                param = eff.get("scenceParam") or ""
                if eff.get("sceneType") == 2 and not param:
                    continue
                base64.b64decode(param, validate=True)    # dati corrotti: meglio fermarsi qui che sulla striscia
                scenes.append({"nome": name, "categoria": cat.get("categoryName", ""),
                               "codice": int(eff["sceneCode"]), "dati": param})
                seen.add(scene_key(name))
                break
    if not scenes:
        raise ValueError(f"nessuna scena per {sku} nella libreria (cmdVersion {cmd_version})")
    return {"sku": sku, "cmdVersion": cmd_version, "fonte": LIBRARY_URL.format(sku=sku), "scene": scenes}


def load_catalog(path: Path) -> dict[str, Scene]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, Scene] = {}
    for item in data.get("scene", []):
        sc = Scene(item["nome"], int(item["codice"]), base64.b64decode(item.get("dati") or ""),
                   item.get("categoria", ""))
        out.setdefault(scene_key(sc.name), sc)
    return out


def read_catalog(path: Path, wanted: bool = False) -> dict[str, Scene]:
    """Catalogo per il ponte: se manca, e' rovinato o troncato avvisa e vale "nessun catalogo", senza fermarsi.

    Un file che manca e' normale finche' non si usa "catalogo": si avvisa solo se e' stato chiesto (`wanted`,
    cioe' GOVEE_SCENE_FILE impostato).
    """
    try:
        return load_catalog(path)
    except FileNotFoundError:
        if wanted:
            log.warning("catalogo delle scene %s non trovato: restano le scene classiche (si crea con "
                        "'catalogo <modello>')", path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        log.warning("catalogo delle scene %s non leggibile (%s: %s): lo ignoro, restano le scene classiche; "
                    "rifallo con 'catalogo <modello>'", path, type(e).__name__, e)
    return {}


def save_catalog(path: Path, catalog: dict) -> None:
    """Salva il catalogo senza mai lasciare un file a meta': file temporaneo nella stessa cartella, poi scambio."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(catalog, ensure_ascii=False, indent=1))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def fetch_library(sku: str, timeout: float = 30) -> dict:
    req = urllib.request.Request(LIBRARY_URL.format(sku=urllib.request.quote(sku)), headers=LIBRARY_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("status") != 200:
        raise RuntimeError(f"libreria Govee: risposta inattesa ({data.get('status')} {data.get('message')})")
    return data


# --- luminosita' e colore ---
class Levels:
    """Luminosita' chiesta da Loxone e intensita' dell'ultimo colore, per mandare i colori sempre pieni.

    Loxone attenua il colore (rosso al 30% = 76-0-0): con pochi livelli per canale i colori tenui vengono
    sbagliati. Il ponte manda il colore pieno (255-0-0) e luminosita' = luminosita' di Loxone x intensita'.
    Senza una luminosita' di Loxone (0 o mai ricevuta) vale 100.
    """

    def __init__(self) -> None:
        self.level: float = 0.0      # 0-100 dall'ultimo "brightness:"
        self.factor: float = 1.0     # 0-1, intensita' dell'ultimo colore (1 con le scene)

    def brightness(self) -> float:
        return (self.level or 100.0) * self.factor


def brightness_frame(pct: float, s: Settings) -> bytes:
    return frame(0x33, 0x04, [max(1, round(_clamp(pct, 0, 100) * s.brightness_max / 100))])


def color_values(text: str) -> tuple[int, int, int] | None:
    """"R-G-B" -> tre numeri (None se il testo e' altro): tra due numeri un solo separatore tra - , ; / e spazio.

    Il testo si divide una volta sola, in tempo lineare. L'espressione regolare usata prima provava tutti i
    modi di spartire gli spazi tra i due separatori: con un pacchetto fatto di spazi bloccava il ponte per
    secondi (1,6 KB) o giorni (60 KB).
    """
    parts = re.split(r"([-,;/\s]+)", text.strip())
    if len(parts) != 5 or not all(p.isdecimal() for p in parts[0::2]):
        return None
    for gap in parts[1::2]:                      # un solo - , ; / (con spazi attorno) oppure spazi con uno spazio vero
        marks = sum(gap.count(c) for c in "-,;/")
        if marks > 1 or (marks == 0 and " " not in gap):
            return None
    r, g, b = (_clamp(int(p), 0, 255) for p in parts[0::2])
    return r, g, b


def parse(message: str, s: Settings, levels: Levels | None = None) -> list[tuple[str, object]]:
    """Traduce UN comando di Loxone in operazioni [(tipo, pacchetto o sequenza)]; [] se sconosciuto.

    "dark" (colore nero, luminosita' 0) spegne come "power", ma cede a un comando che accende (vedi Pending).
    Con `levels` i colori tenui diventano colore pieno + luminosita' piu' bassa (vedi Levels).
    """
    if len(message) > MAX_DATAGRAM:              # nessun comando vero e' cosi' lungo
        return []
    text = message.strip()
    low = text.lower()
    if low in ("on", "accendi", "accendi led", "brightness:on"):
        return [("power", POWER_ON)]
    if low in ("off", "spegni", "spegni led"):
        return [("power", POWER_OFF)]
    if low == "test":
        seq = [color_frame(s.color_mode, *rgb) for rgb in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
        return [("power", POWER_ON), ("test", seq)]
    if re.fullmatch(r"brightness\s*:\s*off", low):     # il blocco di stato di Loxone scrive "off" sotto l'1%
        if levels is not None:
            levels.level = 0.0
        return [("dark", POWER_OFF)]
    m = re.fullmatch(r"brightness\s*:\s*(-?\d+(?:[.,]\d+)?)", low)
    if m:
        pct = _clamp(float(m.group(1).replace(",", ".")), 0, 100)
        if levels is not None:
            levels.level = float(pct)
        if pct == 0:
            return [("dark", POWER_OFF)]
        if levels is not None:
            pct = levels.brightness()
        return [("power", POWER_ON), ("brightness", brightness_frame(pct, s))]
    m = re.fullmatch(r"colou?r\s*:(.*)", low, flags=re.DOTALL)
    rgb = color_values(m.group(1)) if m else None
    if rgb:
        r, g, b = rgb
        if r == g == b == 0:
            return [("dark", POWER_OFF)]
        if levels is None:
            return [("power", POWER_ON), ("color", color_frame(s.color_mode, r, g, b))]
        top = max(r, g, b)
        levels.factor = top / 255
        full = [_clamp(c * 255 / top, 0, 255) for c in (r, g, b)]
        return [("power", POWER_ON), ("color", color_frame(s.color_mode, *full)),
                ("brightness", brightness_frame(levels.brightness(), s))]
    m = re.fullmatch(r"scen[ae]\s*:\s*(.+)", text, flags=re.IGNORECASE)
    key = scene_key(m.group(1) if m else text)
    if key in s.scenes:                                  # GOVEE_SCENES e scene classiche
        code = s.scenes[key]
        payload = [0x04, code & 0xFF] + ([code >> 8] if code > 0xFF else [])
        ops: list[tuple[str, object]] = [("power", POWER_ON), ("scene", frame(0x33, 0x05, payload))]
    elif key in s.catalog:
        ops = [("power", POWER_ON), ("scene", scene_frames(s.catalog[key]))]
    else:
        return []
    if levels is not None and levels.factor != 1.0:      # dopo un colore tenue: la scena a piena intensita'
        levels.factor = 1.0
        ops.append(("brightness", brightness_frame(levels.brightness(), s)))
    return ops


LIGHT_KINDS = ("brightness", "color", "scene", "test")   # operazioni che lasciano la luce accesa


def split_messages(data: bytes) -> list[str]:
    return [p.strip() for p in re.split(r"[;\r\n]+", data.decode("utf-8", "replace")) if p.strip()]


class Pending:
    """Ultimo stato richiesto, per tipo: un cursore della luminosita' che manda 20 valori ne invia uno.

    "dark" spegne solo se tra i comandi in attesa niente accende la luce, in qualunque ordine arrivino.
    """

    def __init__(self) -> None:
        self.items: dict[str, object] = {}

    def add(self, ops: list[tuple[str, object]]) -> None:
        for kind, value in ops:
            if kind == "dark":
                if self.items.get("power") == POWER_ON or any(k in self.items for k in LIGHT_KINDS):
                    continue
                kind = "power"
            if kind == "power" and self.items.get("power") == value:   # l'accensione resta in testa
                continue
            if kind in ("color", "scene", "test"):           # colore, scena e prova si escludono
                for other in ("color", "scene", "test"):
                    self.items.pop(other, None)
            if kind == "power" and value == POWER_OFF:        # spegnere annulla tutto il resto
                self.items.clear()
            self.items.pop(kind, None)
            self.items[kind] = value

    def first(self) -> tuple[str, object] | None:
        return next(iter(self.items.items()), None)

    def done(self, kind: str, value: object) -> None:
        if self.items.get(kind) is value:
            del self.items[kind]


class GoveeBridge:
    """Collegamento a UNA luce: tiene i comandi in attesa e si ricollega da solo."""

    def __init__(self, s: Settings, address: str) -> None:
        self.s = s
        self.address = address
        self.pending = Pending()
        self.wake = asyncio.Event()

    def submit(self, ops: list[tuple[str, object]]) -> None:
        if ops:
            self.pending.add(ops)
            self.wake.set()

    async def run(self, connect_lock: asyncio.Lock | None = None) -> None:
        from bleak import BleakClient, BleakScanner
        lock = connect_lock or asyncio.Lock()
        backoff = 5
        while True:
            try:
                async with lock:      # BlueZ cerca e collega una luce alla volta ("Operation already in progress")
                    device = await BleakScanner.find_device_by_address(self.address, timeout=20)
                    client = BleakClient(device, timeout=20) if device else None
                    if client:
                        await client.connect()
                if client is None:
                    log.warning("luce %s non trovata: spenta, troppo lontana o collegata all'app del telefono",
                                self.address)
                else:
                    try:
                        log.info("collegato a %s (%s)", device.name or "?", self.address)
                        backoff = 1    # caduta di un collegamento riuscito: si riprova subito
                        await self._session(client)
                        log.warning("collegamento chiuso dalla luce")
                    finally:
                        await self._disconnect(client)
            except Exception as e:  # Bluetooth instabile: si riprova, i comandi in attesa non si perdono
                log.warning("collegamento non riuscito o perso: %s: %s", type(e).__name__, e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    @staticmethod
    async def _disconnect(client) -> None:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5)
        except Exception:     # BlueZ a volte non risponde: dopo 5 s si lascia perdere (anche all'arresto)
            pass

    async def _session(self, client) -> None:
        last_sent = 0.0
        while client.is_connected:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=KEEPALIVE_S)
                await asyncio.sleep(BURST_S)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            while (item := self.pending.first()) is not None:
                kind, value = item
                for pkt in (value if isinstance(value, list) else [value]):
                    await client.write_gatt_char(WRITE_UUID, pkt, response=False)
                    await asyncio.sleep(TEST_STEP_S if kind == "test" else SEND_GAP_S)
                self.pending.done(kind, value)
                log.info("%s: inviato %s", self.address, kind)
                last_sent = time.monotonic()
            if time.monotonic() - last_sent >= KEEPALIVE_S:
                await client.write_gatt_char(WRITE_UUID, KEEPALIVE, response=False)
                last_sent = time.monotonic()


class LoxoneUdp(asyncio.DatagramProtocol):
    def __init__(self, s: Settings, bridges: list[GoveeBridge]) -> None:
        self.s = s
        self.bridges = bridges
        self.levels = Levels()     # le luci ricevono gli stessi comandi: uno stato per tutte
        self.oversized = False     # un avviso solo per i pacchetti troppo lunghi, poi solo a livello debug

    def datagram_received(self, data: bytes, addr) -> None:
        if self.s.allow_from and addr[0] not in self.s.allow_from:
            log.warning("comando da %s ignorato (non in ALLOW_FROM)", addr[0])
            return
        if len(data) > MAX_DATAGRAM:           # non viene da Loxone: non si elabora e non si copia nel log
            log.log(logging.DEBUG if self.oversized else logging.WARNING,
                    "pacchetto di %d byte da %s ignorato (il massimo e' %d)", len(data), addr[0], MAX_DATAGRAM)
            self.oversized = True
            return
        for msg in split_messages(data):
            if msg == NO_SCENE:
                log.info("da %s: nessuna scena Govee (atmosfera senza scena)", addr[0])
                continue
            ops = parse(msg, self.s, self.levels)
            log.info("da %s: %r -> %s", addr[0], msg, ", ".join(k for k, _ in ops) or "comando sconosciuto")
            for bridge in self.bridges:
                bridge.submit(ops)


def require_address(s: Settings) -> None:
    """Senza GOVEE_ADDRESS non c'e' una luce da comandare: messaggio chiaro e uscita con EXIT_CONFIG."""
    if not s.addresses:
        log.error("GOVEE_ADDRESS e' vuoto: manca l'indirizzo Bluetooth della luce. Trovalo con 'loxone_govee.py "
                  "scan' e scrivilo in /etc/default/loxone-govee (GOVEE_ADDRESS=AA:BB:CC:DD:EE:FF), poi: sudo "
                  "systemctl restart loxone-govee. Per i comandi lanciati a mano carica prima quel file: "
                  "set -a; . /etc/default/loxone-govee; set +a")
        raise SystemExit(EXIT_CONFIG)


def warn_if_open(s: Settings) -> None:
    """ALLOW_FROM vuoto resta il predefinito, ma all'avvio si dice chiaro cosa vuol dire e come restringerlo."""
    if not s.allow_from:
        log.warning("ALLOW_FROM e' vuoto: il ponte accetta comandi UDP da chiunque in rete (chiunque puo' "
                    "comandare le luci). Per limitarli scrivi in /etc/default/loxone-govee "
                    "ALLOW_FROM=<IP del Miniserver>,127.0.0.1 e riavvia il servizio")


async def run_bridge(s: Settings) -> None:
    require_address(s)
    bridges = [GoveeBridge(s, a) for a in s.addresses]
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: LoxoneUdp(s, bridges), local_addr=("0.0.0.0", s.port))
    log.info("in ascolto su UDP %d per %s (colore %s, luminosita' max %d, %d scene nel catalogo)", s.port,
             ", ".join(s.addresses), s.color_mode, s.brightness_max, len(s.catalog))
    warn_if_open(s)
    # all'arresto (systemctl stop, riavvio) chiude i collegamenti Bluetooth: se restano appesi, alla
    # ripartenza la luce si scollega dopo pochi secondi
    loop.add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    connect_lock = asyncio.Lock()
    try:
        await asyncio.gather(*(b.run(connect_lock) for b in bridges))
    except asyncio.CancelledError:
        log.info("arresto: collegamenti Bluetooth chiusi")


async def scan(seconds: float) -> None:
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    govee = [(d, adv) for d, adv in found.values() if (d.name or "").startswith(GOVEE_NAME_PREFIXES)]
    print(f"Dispositivi Bluetooth trovati: {len(found)}, luci Govee: {len(govee)}")
    for d, adv in sorted(govee, key=lambda x: -x[1].rssi):
        print(f"  {d.address}  {d.name}  segnale {adv.rssi} dBm")


async def _read_state(client, answers: list[bytes], names=("accensione", "luminosita", "modo e colore")) -> str:
    out = []
    for name in names:
        answers.clear()
        await client.write_gatt_char(WRITE_UUID, QUERIES[name], response=False)
        await asyncio.sleep(0.8)
        out.append(f"{name}: " + (" ".join(a[:8].hex(" ") for a in answers) or "nessuna risposta"))
    return " | ".join(out)


async def direct(s: Settings, messages: list[str], pause: float) -> None:
    """Collegamento diretto (a servizio fermo): stato della luce, poi i comandi dati, ognuno col suo effetto."""
    from bleak import BleakClient, BleakScanner
    for address in s.addresses:
        device = await BleakScanner.find_device_by_address(address, timeout=20)
        if device is None:
            print(f"{address}: non trovata")
            continue
        answers: list[bytes] = []
        client = BleakClient(device, timeout=20)
        await client.connect()
        try:
            await client.start_notify(NOTIFY_UUID, lambda _, data: answers.append(bytes(data)))
            print(f"{address} ({device.name})")
            if not messages:
                for name in QUERIES:
                    print("  " + await _read_state(client, answers, (name,)))
            for msg in messages:
                ops = parse(msg, s)
                if not ops:
                    print(f"  {msg!r}: comando sconosciuto")
                    continue
                for kind, value in ops:
                    for pkt in (value if isinstance(value, list) else [value]):
                        await client.write_gatt_char(WRITE_UUID, pkt, response=False)
                        await asyncio.sleep(TEST_STEP_S if kind == "test" else SEND_GAP_S)
                await asyncio.sleep(pause)
                print(f"  {msg!r} -> " + await _read_state(client, answers))
        finally:
            try:
                await client.disconnect()
            except Exception:  # BlueZ a volte chiude il canale mentre si scollega: non e' un errore
                pass


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Ponte Loxone -> luci Govee Bluetooth")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="avvia il ponte")
    a = sub.add_parser("scan", help="cerca le luci Govee vicine")
    a.add_argument("--secondi", type=float, default=10)
    a = sub.add_parser("send", help="manda un comando al ponte in esecuzione")
    a.add_argument("messaggio")
    a.add_argument("--host", default="127.0.0.1")
    sub.add_parser("query", help="legge lo stato della luce (a servizio fermo)")
    a = sub.add_parser("prova", help="manda comandi direttamente alla luce e ne legge l'effetto (a servizio fermo)")
    a.add_argument("messaggi", nargs="+")
    a.add_argument("--pausa", type=float, default=2.0, help="secondi tra un comando e la lettura dello stato")
    a = sub.add_parser("catalogo", help="scarica (una volta) il catalogo pubblico delle scene del modello")
    a.add_argument("sku", help="modello, es. H6126 (e' nel nome Bluetooth: GBK_H6126_xxxx)")
    a.add_argument("--da-file", help="libreria gia' scaricata (JSON della libreria Govee) invece di scaricarla")
    a.add_argument("--cmd-version", type=int, default=0, help="versione dei comandi delle scene (0 = firmware vecchi)")
    sub.add_parser("scene", help="elenca le scene del catalogo")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    s = Settings.from_env()
    if args.cmd == "catalogo":
        library = (json.loads(Path(args.da_file).read_text(encoding="utf-8")) if args.da_file
                   else fetch_library(args.sku))
        cat = build_catalog(library, args.sku, args.cmd_version)
        out = Path(s.scene_file)
        save_catalog(out, cat)
        simple = sum(1 for x in cat["scene"] if not x["dati"])
        print(f"{len(cat['scene'])} scene per {args.sku} ({simple} classiche, {len(cat['scene']) - simple} animate) "
              f"salvate in {out}. Riavvia il ponte: sudo systemctl restart loxone-govee")
        return
    if args.cmd == "scene":
        if not s.catalog:
            print(f"nessun catalogo in {s.scene_file}: usa prima 'catalogo <modello>'")
        for sc in s.catalog.values():
            kind = f"animata, {len(sc.param)} byte" if sc.param else "classica"
            print(f"  {sc.name:<28} codice {sc.code:<6} {sc.category} ({kind})")
        return
    if args.cmd == "scan":
        asyncio.run(scan(args.secondi))
    elif args.cmd == "send":
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(args.messaggio.encode("utf-8"), (args.host, s.port))
        print(f"inviato {args.messaggio!r} a {args.host}:{s.port}")
    elif args.cmd == "query":
        require_address(s)
        asyncio.run(direct(s, [], 0))
    elif args.cmd == "prova":
        require_address(s)
        asyncio.run(direct(s, args.messaggi, args.pausa))
    else:
        require_address(s)     # prima di aprire porta e Bluetooth: codice 78, il servizio non riparte a vuoto
        asyncio.run(run_bridge(s))


if __name__ == "__main__":
    main()
