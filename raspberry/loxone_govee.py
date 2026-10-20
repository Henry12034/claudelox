#!/usr/bin/env python3
"""Ponte Loxone -> luci LED Govee via Bluetooth (BLE), pensato per un Raspberry Pi.

Il Miniserver manda comandi di testo via UDP (uscita virtuale "/dev/udp/<raspberry>/55000"):
  on | accendi led          accende
  off | spegni led          spegne
  brightness:<0-100>        luminosita' in percentuale (0 oppure "off" = spegne)
  color:<R>-<G>-<B>         colore, valori 0-255 (0-0-0 = spegne)
  <nome scena>              scena Govee, es. Movie, Romantic, Snowflake (vedi GOVEE_SCENES)
  test                      rosso, verde, blu: serve a controllare il collegamento
Piu' comandi nello stesso pacchetto si separano con ';' o a capo. I comandi che arrivano insieme si
uniscono: il colore nero e la luminosita' 0 spengono solo se nel gruppo non c'e' altro che accende
(passando alla scena "Neve", Loxone manda "color:0-0-0" e subito dopo "Snowflake").

Protocollo Govee (documentato da vari progetti open source): pacchetti da 20 byte scritti sulla
caratteristica 00010203-0405-0607-0809-0a0b0c0d2b11; il primo byte e' 0x33 (comando) o 0xAA
(richiesta, usata anche come keep-alive), l'ultimo e' lo XOR dei 19 precedenti. La striscia accetta un
solo collegamento alla volta: se l'app Govee del telefono e' collegata, il Raspberry non riesce a entrare.

Configurazione da variabili d'ambiente (sul Raspberry: /etc/default/loxone-govee):
  GOVEE_ADDRESS          indirizzo Bluetooth della luce (obbligatorio); piu' luci separate da virgola
  UDP_PORT               porta su cui ascoltare (55000)
  ALLOW_FROM             IP ammessi, separati da virgola (vuoto = tutti); es. l'IP del Miniserver
  GOVEE_COLOR_MODE       variante del comando colore: 02 (classico), 0d, 15 (strisce RGBIC)
  GOVEE_BRIGHTNESS_MAX   valore massimo della luminosita' per il modello: 255 oppure 100
  GOVEE_SCENES           scene aggiuntive o corrette, es. "movie=4,romantic=7,snowflake=15"

Uso:
  loxone_govee.py run                   avvia il ponte (e' cio' che fa il servizio systemd)
  loxone_govee.py scan                  cerca le luci Govee vicine
  loxone_govee.py send "color:255-0-0"  manda un comando al ponte in esecuzione (prova)
  loxone_govee.py query                 legge lo stato della luce (con il servizio fermo)
  loxone_govee.py prova "color:255-0-0" manda comandi direttamente alla luce e ne legge l'effetto
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import signal
import socket
import sys
import time
from dataclasses import dataclass, field

WRITE_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
NOTIFY_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"
KEEPALIVE_S = 2.0      # la striscia chiude il collegamento se non riceve niente per un po'
SEND_GAP_S = 0.08      # comandi troppo ravvicinati possono andare persi
BURST_S = 0.15         # attesa dopo il primo comando, per unire quelli che Loxone manda insieme
TEST_STEP_S = 0.8
GOVEE_NAME_PREFIXES = ("GBK_", "ihoment_", "Govee_", "Minger_", "GVH")

# scene "classiche" dei modelli Govee Bluetooth (comando 0x05, modo 0x04): da verificare sul modello
DEFAULT_SCENES = {"sunrise": 0, "sunset": 1, "movie": 4, "dating": 5, "romantic": 7, "blinking": 8,
                  "candlelight": 9, "snowflake": 15}

log = logging.getLogger("loxone-govee")


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

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        s = cls(address=env.get("GOVEE_ADDRESS", "").strip().upper(),
                port=int(env.get("UDP_PORT", "55000")),
                allow_from={ip.strip() for ip in env.get("ALLOW_FROM", "").split(",") if ip.strip()},
                color_mode=env.get("GOVEE_COLOR_MODE", "02").strip().lower(),
                brightness_max=int(env.get("GOVEE_BRIGHTNESS_MAX", "255")))
        for item in env.get("GOVEE_SCENES", "").split(","):
            if "=" in item:
                name, value = item.split("=", 1)
                s.scenes[name.strip().lower()] = int(value.strip(), 0)
        if s.color_mode not in ("02", "0d", "15"):
            raise ValueError(f"GOVEE_COLOR_MODE non valido: {s.color_mode} (usa 02, 0d o 15)")
        if s.brightness_max not in (100, 255):
            raise ValueError("GOVEE_BRIGHTNESS_MAX deve essere 100 o 255")
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


def parse(message: str, s: Settings) -> list[tuple[str, object]]:
    """Traduce UN comando di Loxone in operazioni [(tipo, pacchetto o sequenza)]; [] se sconosciuto.

    "dark" (colore nero, luminosita' 0) spegne come "power", ma cede a un comando che accende (vedi Pending).
    """
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
        return [("dark", POWER_OFF)]
    m = re.fullmatch(r"brightness\s*:\s*(-?\d+(?:[.,]\d+)?)", low)
    if m:
        pct = _clamp(float(m.group(1).replace(",", ".")), 0, 100)
        if pct == 0:
            return [("dark", POWER_OFF)]
        level = max(1, round(pct * s.brightness_max / 100))
        return [("power", POWER_ON), ("brightness", frame(0x33, 0x04, [level]))]
    m = re.fullmatch(r"colou?r\s*:\s*(\d+)\s*[-,;/ ]\s*(\d+)\s*[-,;/ ]\s*(\d+)", low)
    if m:
        r, g, b = (_clamp(int(x), 0, 255) for x in m.groups())
        if r == g == b == 0:
            return [("dark", POWER_OFF)]
        return [("power", POWER_ON), ("color", color_frame(s.color_mode, r, g, b))]
    if low in s.scenes:
        code = s.scenes[low]
        payload = [0x04, code & 0xFF] + ([code >> 8] if code > 0xFF else [])
        return [("power", POWER_ON), ("scene", frame(0x33, 0x05, payload))]
    return []


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

    async def run(self) -> None:
        from bleak import BleakClient, BleakScanner
        backoff = 5
        while True:
            try:
                device = await BleakScanner.find_device_by_address(self.address, timeout=20)
                if device is None:
                    log.warning("luce %s non trovata: spenta, troppo lontana o collegata all'app del telefono",
                                self.address)
                else:
                    async with BleakClient(device, timeout=20) as client:
                        log.info("collegato a %s (%s)", device.name or "?", self.address)
                        backoff = 1    # caduta di un collegamento riuscito: si riprova subito
                        await self._session(client)
                    log.warning("collegamento chiuso dalla luce")
            except Exception as e:  # Bluetooth instabile: si riprova, i comandi in attesa non si perdono
                log.warning("collegamento non riuscito o perso: %s: %s", type(e).__name__, e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

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

    def datagram_received(self, data: bytes, addr) -> None:
        if self.s.allow_from and addr[0] not in self.s.allow_from:
            log.warning("comando da %s ignorato (non in ALLOW_FROM)", addr[0])
            return
        for msg in split_messages(data):
            ops = parse(msg, self.s)
            log.info("da %s: %r -> %s", addr[0], msg, ", ".join(k for k, _ in ops) or "comando sconosciuto")
            for bridge in self.bridges:
                bridge.submit(ops)


async def run_bridge(s: Settings) -> None:
    if not s.addresses:
        raise SystemExit("ERRORE: imposta GOVEE_ADDRESS (indirizzo Bluetooth della luce, vedi 'scan')")
    bridges = [GoveeBridge(s, a) for a in s.addresses]
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: LoxoneUdp(s, bridges), local_addr=("0.0.0.0", s.port))
    log.info("in ascolto su UDP %d per %s (colore %s, luminosita' max %d)", s.port, ", ".join(s.addresses),
             s.color_mode, s.brightness_max)
    # all'arresto (systemctl stop, riavvio) chiude i collegamenti Bluetooth: se restano appesi, alla
    # ripartenza la luce si scollega dopo pochi secondi
    loop.add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    try:
        await asyncio.gather(*(b.run() for b in bridges))
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
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    s = Settings.from_env()
    if args.cmd == "scan":
        asyncio.run(scan(args.secondi))
    elif args.cmd == "send":
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(args.messaggio.encode("utf-8"), (args.host, s.port))
        print(f"inviato {args.messaggio!r} a {args.host}:{s.port}")
    elif args.cmd == "query":
        asyncio.run(direct(s, [], 0))
    elif args.cmd == "prova":
        asyncio.run(direct(s, args.messaggi, args.pausa))
    else:
        asyncio.run(run_bridge(s))


if __name__ == "__main__":
    main()
