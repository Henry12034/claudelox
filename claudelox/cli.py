"""Riga di comando "lox": controllare e analizzare il Miniserver Loxone da Claude Code.

Esempi:
  lox ping                      raggiungibilita' e versione (nessun login)
  lox login                     ottiene il token (chiede la password se non e' nel .env)
  lox stato --stanza Bagno      stato attuale dei controlli di una stanza
  lox comando "Luci Bagno" changeTo/778
  lox salute                    controllo generale dell'impianto
  lox programma riepilogo       analisi del programma (dall'ultimo backup di Loxone Config)
"""
from __future__ import annotations

import argparse
import dataclasses
import ftplib
import getpass
import io
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import websocket

from . import appstructure, backups, editor, logs
from . import program as programmod
from . import shelly as shellymod
from . import telefono, uploader
from .client import (LOX_EPOCH, STRUCTURE_CACHE, AuthError, HttpError, LoxoneError, Miniserver, delete_token,
                     explain, listing_time, ll_code, load_token, loose_json)
from .config import DATA_DIR, get_settings
from .loxcc import LoxCCError, read_program
from .loxcc import decompress as decompress_loxcc
from .program import Program
from .structure import COMMAND_HINTS, READ_ONLY_TYPES, Structure, mood_list, summarize

DEFAULT_CONFIG_EXE = r"C:\Program Files (x86)\Loxone\LoxoneConfig\LoxoneConfig.exe"
MS_TYPES = {0: "Miniserver (Gen 1)", 1: "Miniserver Go (Gen 1)", 2: "Miniserver (Gen 2)",
            3: "Miniserver Go (Gen 2)", 4: "Miniserver Compact"}
RIGHTS = {0x1: "Admin", 0x2: "Web", 0x4: "App", 0x8: "Loxone Config", 0x10: "FTP", 0x20: "Cambio password",
          0x40: "Modalita' esperto", 0x80: "Modalita' operative", 0x100: "Webservice di sistema",
          0x200: "Automatic Designer", 0x400: "Expert mode light", 0x800: "Gestione utenti",
          0x1000: "Ricerca dispositivi", 0x2000: "Gestione plugin", 0x4000: "Trust JWT",
          0x8000: "Avvio aggiornamenti", 0x10000: "Avvio backup"}
# comandi "raw" ammessi senza --conferma: solo letture
READONLY_RAW = [re.compile(p) for p in (
    r"^j?dev/cfg/[A-Za-z0-9]+$",
    r"^j?dev/sps/(LoxAPPversion3|state|status|getstatsdate)$",
    r"^j?dev/sys/(numtasks|cpu|heap|ints|comints|contextswitches|lastcpu|date|time)$",
    r"^j?dev/(lan|bus)/[A-Za-z0-9]+$",
    r"^j?dev/fslist(/[^?]*)?$",
    r"^j?dev/sps/io/[0-9a-f-]+/controlnotes$",
)]


def warn(msg: str) -> None:
    print(f"[!] {msg}", file=sys.stderr)


def interactive() -> bool:
    """C'e' una persona davanti a un terminale vero? Serve prima di chiedere una password.

    Su Windows isatty() e' vero anche quando l'ingresso e' il dispositivo NUL (comando lanciato da un altro
    programma): getpass resterebbe in attesa per sempre. GetConsoleMode riesce solo su una console vera."""
    try:
        if not sys.stdin.isatty():
            return False
        if os.name == "nt":
            import ctypes
            import msvcrt
            mode = ctypes.c_uint32()
            handle = ctypes.c_void_p(msvcrt.get_osfhandle(sys.stdin.fileno()))
            return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except (OSError, ValueError, AttributeError):
        return False
    return True


def fmt_lox_time(t: float | None) -> str:
    return datetime.fromtimestamp(LOX_EPOCH + t).strftime("%d/%m/%Y %H:%M") if t else "?"


def rights_text(bits: int | None) -> str:
    if bits is None:
        return "?"
    return ", ".join(name for bit, name in RIGHTS.items() if bits & bit) or "nessuno"


def has_credentials(s) -> bool:
    return bool(s.user and (s.password or load_token(s.host, s.user)))


def open_ms(s) -> Miniserver:
    return Miniserver(s).connect()


def load_structure(args, s) -> tuple[Structure, str]:
    """Struttura dal Miniserver se possibile, altrimenti cache locale, altrimenti ultimo backup."""
    if not getattr(args, "offline", False) and has_credentials(s):
        try:
            with open_ms(s) as ms:
                return Structure(ms.structure(refresh=getattr(args, "aggiorna", False))), "Miniserver (live)"
        except (LoxoneError, OSError, websocket.WebSocketException) as e:
            warn(f"Miniserver non disponibile ({e}): uso i dati locali")
    if STRUCTURE_CACHE.exists():
        return Structure(json.loads(STRUCTURE_CACHE.read_text(encoding="utf-8"))), "cache locale"
    found = backups.latest_structure(s.backups_dir, DOWNLOADS, s.host)
    if found:
        data, p = found
        return Structure(data), f"programma v{p['version']} del {p['saved']:%d/%m/%Y %H:%M} ({p['origine']})"
    raise LoxoneError("nessuna struttura disponibile: configura il login (vedi README) o indica i backup")


def live_states(s) -> tuple[Structure, Miniserver]:
    ms = open_ms(s)
    st = Structure(ms.structure())
    ms.enable_status_updates()
    return st, ms


# ------------------------------------------------------------------ comandi

def cmd_ping(args, s):
    ms = Miniserver(s)
    info = ms.api_info()
    api = loose_json(ms.http_public("jdev/cfg/api")["value"])
    https = {1: "si'", 2: "certificato scaduto"}.get(info.get("httpsStatus"), "no")
    print(f"Miniserver {s.host}: raggiungibile")
    print(f"  seriale: {info.get('snr')}   firmware: {info.get('version')}")
    print(f"  connessione considerata locale: {'si' if info.get('local') else 'no'}   HTTPS: {https}   "
          f"slot per aggiornamenti di stato liberi: {'si' if api.get('hasEventSlots') else 'no'}")
    if args.ws:
        with Miniserver(s).connect(login=False):
            print("  WebSocket e scambio chiavi cifrato: OK")
    tok = load_token(s.host, s.user) if s.user else None
    if not s.user:
        print("  credenziali: LOXONE_USER non impostato nel file .env")
    elif tok:
        print(f"  utente '{s.user}': token salvato, valido fino al {fmt_lox_time(tok.valid_until)}")
    else:
        print(f"  utente '{s.user}': nessun token salvato" +
              (" (password presente nel .env)" if s.password else " -> esegui 'lox login'"))


def cmd_login(args, s):
    if not s.user:
        raise AuthError("imposta LOXONE_USER nel file .env (vedi .env.example)")
    password = s.password
    if not password:
        if not interactive():
            raise AuthError("la password va digitata da una persona: apri un terminale nella cartella del progetto "
                            "ed esegui 'lox login' (su Windows: .\\lox.cmd login)")
        password = getpass.getpass(f"Password Loxone per l'utente '{s.user}' (non viene salvata): ")
    with Miniserver(s).connect(login=False) as ms:
        tok = ms.login_with_password(s.user, password)
    print(f"Login riuscito come '{s.user}'. Token salvato in .state/token.json "
          f"(valido fino al {fmt_lox_time(tok.valid_until)}, si rinnova automaticamente).")
    print(f"Permessi del token: {rights_text(tok.rights)}")
    if tok.unsecure_pass:
        warn("il Miniserver considera DEBOLE la password di questo utente: cambiala in Loxone Config")


def cmd_logout(args, s):
    if not (s.user and load_token(s.host, s.user)):
        delete_token()
        print("Nessun token salvato.")
        return
    with open_ms(s) as ms:
        ok = ms.kill_token()
    print("Token revocato sul Miniserver e cancellato in locale." if ok
          else "Token cancellato in locale (revoca sul Miniserver non confermata).")


def cmd_info(args, s):
    with open_ms(s) as ms:
        info = ms.api_info()
        st = Structure(ms.structure())
        tok = ms.token
    m = st.ms
    print(f"Nome: {m.get('msName')}   Progetto: {m.get('projectName')}   Luogo: {m.get('location')}")
    print(f"Modello: {MS_TYPES.get(m.get('miniserverType'), m.get('miniserverType'))}   "
          f"Seriale: {m.get('serialNr')}   Firmware: {info.get('version')}")
    print(f"Ultima modifica alla configurazione: {st.last_modified}")
    print(f"Stanze: {len(st.rooms)}   Categorie: {len(st.cats)}   Controlli: {len(st.top_level())}")
    if tok:
        print(f"Collegato come '{tok.user}' - permessi: {rights_text(tok.rights)} - "
              f"token valido fino al {fmt_lox_time(tok.valid_until)}")


def cmd_rooms(args, s):
    st, src = load_structure(args, s)
    print(f"Fonte: {src}")
    counts: dict[str, int] = {}
    for c in st.top_level():
        counts[st.room_name(c)] = counts.get(st.room_name(c), 0) + 1
    for room, n in sorted(counts.items()):
        print(f"  {room}: {n} controlli")


def cmd_controls(args, s):
    st, src = load_structure(args, s)
    items = st.find(args.testo, room=args.stanza, type_=args.tipo, cat=args.categoria, include_sub=args.sotto)
    if args.json:
        print(json.dumps([{"nome": c["name"], "tipo": c["type"], "stanza": st.room_name(c),
                           "categoria": st.cat_name(c), "uuid": c.get("uuidAction", c["_uuid"])} for c in items],
                         ensure_ascii=False, indent=2))
        return
    print(f"Fonte: {src} - {len(items)} controlli")
    for c in items:
        flag = " [SICUREZZA]" if st.sensitive_reason(c, s.protected) else ""
        print(f"  - {st.label(c)} cat={st.cat_name(c)} uuid={c.get('uuidAction', c['_uuid'])}{flag}")


def cmd_show(args, s):
    ms = None
    try:
        if has_credentials(s) and not args.offline:
            st, ms = live_states(s)
            src = "Miniserver (live)"
        else:
            st, src = load_structure(args, s)
        c = st.resolve(args.controllo, room=args.stanza)
        print(f"{st.label(c)}   (fonte: {src})")
        print(f"  uuidAction: {c.get('uuidAction')}   categoria: {st.cat_name(c)}   "
              f"protetto da password visu: {'si' if c.get('isSecured') else 'no'}")
        if c.get("details"):
            print(f"  dettagli: {json.dumps(programmod.redact_data(c['details']), ensure_ascii=False)[:500]}")
        if ms is not None:
            states = st.states_of(c, ms.values, ms.texts)
            print(f"  stato: {summarize(c, states)}")
            for name, val in states.items():
                print(f"    .{name} = {programmod.redact(str(val))[:300]}")
            if c["type"] == "LightControllerV2":
                print("  scene disponibili (id: nome): " +
                      ", ".join(f"{m.get('id')}: {m.get('name')}" for m in mood_list(states)))
        else:
            print("  (stato attuale non disponibile: serve il login al Miniserver)")
        for sub in (c.get("subControls") or {}):
            print(f"  sotto-controllo: {st.label(st.controls[sub])} uuid={sub}")
        if c["type"] in READ_ONLY_TYPES:
            print("  comandi: nessuno (controllo di sola lettura)")
        else:
            hints = COMMAND_HINTS.get(c["type"])
            print(f"  comandi tipici: {', '.join(hints)}" if hints else
                  "  comandi: vedi la documentazione Loxone 'Structure File' per questo tipo")
        reason = st.sensitive_reason(c, s.protected)
        if reason:
            print(f"  ATTENZIONE: {reason}: i comandi richiedono --conferma e il consenso esplicito dell'utente")
    finally:
        if ms is not None:
            ms.close()


def cmd_state(args, s):
    st, ms = live_states(s)
    with ms:
        if args.controlli:
            sel = [st.resolve(ref, room=args.stanza) for ref in args.controlli]
        else:
            sel = st.find(room=args.stanza, include_sub=False)
        snapshot = {}
        for c in sel:
            states = st.states_of(c, ms.values, ms.texts)
            snapshot[c["_uuid"]] = {"nome": c["name"], "tipo": c["type"], "stanza": st.room_name(c),
                                    "riepilogo": summarize(c, states), "stati": states}
            if not args.json:
                print(f"  {st.label(c)}: {summarize(c, states)}")
        if args.json:
            print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
        if args.salva:
            DATA_DIR.mkdir(exist_ok=True)
            path = DATA_DIR / f"snapshot-{datetime.now():%Y%m%d-%H%M%S}.json"
            path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print(f"Snapshot salvato in {path}")


def cmd_command(args, s):
    st, ms = live_states(s)
    with ms:
        c = st.resolve(args.controllo, room=args.stanza)
        if c["type"] in READ_ONLY_TYPES or (c.get("restrictions") or 0) & 0b10:
            raise LoxoneError(f"{st.label(c)} e' di sola lettura: non accetta comandi")
        if c.get("isSecured"):
            raise LoxoneError(f"{st.label(c)} e' protetto da password visu: comando non supportato da questo tool")
        reason = st.sensitive_reason(c, s.protected)
        if reason and not args.conferma:
            raise LoxoneError(f"{st.label(c)}: {reason}. E' un comando di SICUREZZA: ripeti con --conferma solo "
                              "dopo il consenso esplicito dell'utente per questa azione.")
        before = summarize(c, st.states_of(c, ms.values, ms.texts))
        ll = ms.io(c["uuidAction"], args.comando)
        code = ll_code(ll)
        for _ in ms.watch(args.attesa):
            pass
        after = summarize(c, st.states_of(c, ms.values, ms.texts))
    print(f"Comando '{args.comando}' -> {st.label(c)}: " + ("OK" if code == 200 else explain(code)))
    print(f"  prima: {before}\n  dopo:  {after}")
    if code != 200:
        sys.exit(1)


def cmd_watch(args, s):
    st, ms = live_states(s)
    with ms:
        wanted = {st.resolve(ref, room=args.stanza)["_uuid"] for ref in args.controlli} if args.controlli else None
        print(f"In ascolto per {args.secondi:g} secondi (Ctrl+C per interrompere)...", flush=True)
        try:
            for uid, val in ms.watch(args.secondi):
                owner = st.state_owner.get(uid)
                if not owner or (wanted and owner[0] not in wanted):
                    continue
                c = st.controls[owner[0]]
                print(f"{datetime.now():%H:%M:%S} {st.label(c)} .{owner[1]} = {str(val)[:200]}", flush=True)
        except KeyboardInterrupt:
            pass


def read_log_text(args, s) -> tuple[str, str]:
    if getattr(args, "live", False):
        with open_ms(s) as ms:  # via HTTP: il WebSocket va in timeout sui file grandi (il log supera 1 MB)
            data = ms.http_download("dev/fsget/log/def.log", timeout=60)
        return data.decode("utf-8", "replace"), "Miniserver (live)"
    for b in reversed(backups.list_backups(s.backups_dir, s.host)):
        f = b["path"] / "log" / "def.log"
        if f.exists():
            return f.read_bytes().decode("utf-8", "replace"), f"backup del {b['date']:%d/%m/%Y %H:%M}"
    raise LoxoneError("nessun log nei backup di Loxone Config su questo computer")


def print_log_summary(text: str, src: str, days: int) -> None:
    since = datetime.now() - timedelta(days=days) if days else None
    rep = logs.summarize(logs.parse(text), since)
    if not rep["periodo"]:
        print(f"Log ({src}): nessuna riga nel periodo")
        return
    a, b = rep["periodo"]
    print(f"Log di sistema ({src}): {a:%d/%m/%Y} -> {b:%d/%m/%Y}, {rep['righe']} righe")
    print("  Avvisi/errori (raggruppati):")
    for g in rep["eventi"][:25]:
        if g["con_chiusura"] or g["livello"] in ("Important", "Critical", "Fatal", "Error"):
            stato = "rientrato" if g["risolto"] else "ANCORA ATTIVO"
        else:
            stato = "avviso"
        print(f"   - {g['livello']} {g['codice']}: {g['messaggio']}  x{g['volte']}  "
              f"(ultima {g['ultima']:%d/%m/%Y}, {stato})")
    if rep["avvii"]:
        last = rep["avvii"][-1]
        print(f"  Avvii del programma: {len(rep['avvii'])} (ultimo {last[0]:%d/%m/%Y %H:%M}: {last[1]})")
    if rep["errori_script"]:
        ts, txt = rep["errori_script"][-1]
        print(f"  Errori di script/compilazione: {len(rep['errori_script'])} (ultimo {ts:%d/%m/%Y}):")
        print("    " + txt.replace("\n", "\n    ")[:800])


def cmd_log(args, s):
    try:
        text, src = read_log_text(args, s)
    except LoxoneError as e:
        if args.live:
            raise
        raise LoxoneError(f"{e}: con 'lox log --live' lo leggi dal Miniserver (serve il login)")
    print_log_summary(text, src, args.giorni)


def cmd_backups(args, s):
    items = backups.list_backups(s.backups_dir)
    if not items:
        print(f"Nessun backup trovato in {s.backups_dir}")
        return
    for b in items:
        progs = backups.list_programs(b["path"])
        last = progs[-1] if progs else None
        print(f"- {b['date']:%d/%m/%Y %H:%M}  firmware {b['firmware']}  {b['path'].name}")
        if last:
            print(f"    {len(progs)} versioni del programma, ultima v{last['version']} "
                  f"salvata il {last['saved']:%d/%m/%Y %H:%M}")


DOWNLOADS = DATA_DIR / "programmi"


def newest_program(s) -> dict | None:
    return backups.newest_program(s.backups_dir, DOWNLOADS, s.host)


def load_program(args, s) -> tuple[Program, str]:
    if args.file:
        xml, _ = read_program(args.file)
        return Program(xml), str(args.file)
    p = newest_program(s)
    if not p:
        raise LoxoneError("nessun programma locale: esegui 'lox programma scarica' oppure indica --file")
    xml, _ = read_program(p["path"])
    return Program(xml), f"v{p['version']} salvata il {p['saved']:%d/%m/%Y %H:%M} ({p['origine']})"


def program_source(args, s) -> Path:
    if args.file:
        return Path(args.file)
    p = newest_program(s)
    if not p:
        raise LoxoneError("nessun programma locale: esegui 'lox programma scarica' oppure indica --file")
    return p["path"]


def download_current_program(s, notes: list[str] | None = None) -> Path:
    """Scarica dal Miniserver l'ultima versione del programma in data/programmi/, sempre come zip completo.

    Di solito sul Miniserver c'e' lo zip salvato da Loxone Config (o da 'lox carica'). Dopo un aggiornamento del
    firmware il Miniserver salva invece il programma convertito come .LoxCC, con struttura dell'app e permessi
    in file separati: in quel caso lo zip viene ricomposto qui (uploader.pack_program). `notes` raccoglie le
    spiegazioni da mostrare a chi ha chiesto lo scaricamento.
    """
    with open_ms(s) as ms:
        names = [f["filename"] for f in ms.list_files("prog") if backups.PROGRAM_RE.match(f.get("filename", ""))]
        if not names:
            raise LoxoneError("nessun programma trovato sul Miniserver (cartella /prog vuota o non leggibile "
                              "con questo utente)")
        def key(n):
            m = backups.PROGRAM_RE.match(n)
            return m.group(2), int(m.group(1))
        name = max(names, key=key)
        data = ms.http_download(f"dev/fsget/prog/{name}")
        if name.lower().endswith(".loxcc"):
            companions = {}
            for member, path in uploader.COMPANION_FILES.items():
                try:
                    companions[member] = ms.http_download(f"dev/fsget/{path}")
                except HttpError as e:
                    if e.status != 404:  # 404 = il file non esiste; altro = non so cosa c'e': mi fermo
                        raise LoxoneError(f"non riesco a leggere /{path} (HTTP {e.status}): pacchetto incompleto")
            try:
                data = uploader.pack_program(data, companions)
            except LoxCCError as e:
                raise LoxoneError(f"il programma scaricato ({name}) non e' leggibile: {e}")
            if notes is not None:
                missing = [m for m in uploader.COMPANION_FILES if m not in companions]
                notes.append(f"il Miniserver lo tiene come {name} (salvato dal firmware, di solito dopo un "
                             "aggiornamento): ricomposto il pacchetto completo con struttura dell'app e permessi"
                             + (f"; non trovati: {', '.join(missing)}" if missing else ""))
            name = Path(name).stem + ".zip"
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise LoxoneError(f"il file scaricato ({name}) non e' un archivio valido")
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    dest = DOWNLOADS / name
    dest.write_bytes(data)
    return dest


def print_diff(d: dict) -> None:
    print(f"Aggiunti: {len(d['aggiunti'])}  Rimossi: {len(d['rimossi'])}  Modificati: {len(d['modificati'])}")
    def more(items: list, shown: int, what: str, indent: str = "  ") -> None:
        if len(items) > shown:
            print(f"{indent}... e altri {len(items) - shown} {what} (elenco accorciato)")

    for x in d["aggiunti"][:60]:
        print(f"  + {x}")
    more(d["aggiunti"], 60, "aggiunti")
    for x in d["rimossi"][:60]:
        print(f"  - {x}")
    more(d["rimossi"], 60, "rimossi")
    for ch in d["modificati"][:80]:
        print(f"  ~ {ch['oggetto']}")
        for note in ch["modifiche"][:12]:
            print(f"      {note}")
        more(ch["modifiche"], 12, "cambiamenti di questo oggetto", "      ")
    more(d["modificati"], 80, "oggetti modificati")


def cmd_program(args, s):
    if args.azione == "scarica":
        notes: list[str] = []
        dest = download_current_program(s, notes)
        print(f"Programma attuale del Miniserver salvato in {dest} ({dest.stat().st_size} byte).")
        for note in notes:
            print(f"  Nota: {note}.")
        warn("contiene dati riservati dell'impianto: non condividerlo")
        return
    if args.azione == "estrai":
        src_path = program_source(args, s)
        xml, app = read_program(src_path)
        out_dir = Path(args.out) if args.out else DATA_DIR / "programmi"
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / (src_path.stem + ".Loxone")
        dest.write_bytes(xml)
        print(f"Programma estratto in {dest} (si apre con Loxone Config).")
        if app:
            (out_dir / (src_path.stem + "_LoxAPP3.json")).write_bytes(app)
        warn("il file contiene anche dati riservati dell'impianto (utenti, password di dispositivi): "
             "non condividerlo")
        return
    if args.azione == "confronta":
        if not args.testo:
            raise LoxoneError("indica il secondo file: lox programma confronta NUOVO --file VECCHIO")
        old, _ = read_program(program_source(args, s))
        new, _ = read_program(args.testo)
        print_diff(programmod.diff(Program(old), Program(new)))
        return
    if args.azione == "parametro":
        if not (args.testo and args.connettore and args.valore is not None):
            raise LoxoneError("uso: lox programma parametro <blocco> --connettore K --valore V [--file F]")
        src_path = program_source(args, s)
        xml, _ = read_program(src_path)
        prog = Program(xml)
        block = prog.block(args.testo)
        cos = {co.get("K"): co for co in block.findall("Co")}
        co = next((c for k, c in cos.items() if k.lower() == args.connettore.lower()), None)
        if co is None:
            raise LookupError(f"connettore '{args.connettore}' non presente in {prog.label(block)}. "
                              f"Disponibili: {', '.join(cos)}")
        new_xml = programmod.set_parameter(xml, co.get("U"), args.valore)
        new_prog = Program(new_xml)  # verifica che il file resti XML valido
        out_dir = DATA_DIR / "programmi"
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = Path(args.out) if args.out else out_dir / f"{src_path.stem}-mod-{datetime.now():%Y%m%d-%H%M%S}.Loxone"
        dest.write_bytes(new_xml)
        print(f"Copia modificata salvata in {dest}")
        print_diff(programmod.diff(prog, new_prog))
        print("Nulla e' stato inviato al Miniserver. Per applicare: 'lox carica <file>' (prova, poi --applica "
              "--conferma) oppure 'lox programma apri <file>' e 'Salva nel Miniserver' in Loxone Config.")
        return
    if args.azione == "rimuovi":
        refs = [r for r in [args.testo, *args.altri] if r]
        if not refs:
            raise LoxoneError("uso: lox programma rimuovi <blocco> [<blocco> ...] [--file F] (nome o UUID)")
        src_path = program_source(args, s)
        xml, _ = read_program(src_path)
        prog = Program(xml)
        blocks = [prog.block(r) for r in refs]
        new_text, n = editor.remove_blocks(xml.decode("utf-8"), [b.get("U") for b in blocks])
        new_xml = new_text.encode("utf-8")
        new_prog = Program(new_xml)  # verifica che il file resti XML valido
        out_dir = DATA_DIR / "programmi"
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = Path(args.out) if args.out else out_dir / f"{src_path.stem}-rimossi-{datetime.now():%Y%m%d-%H%M%S}.Loxone"
        dest.write_bytes(new_xml)
        print(f"Partenza: {src_path.name}. Copia senza i {n} blocchi salvata in {dest}")
        print_diff(programmod.diff(prog, new_prog))
        print("Nulla e' stato inviato al Miniserver. Per applicare: 'lox carica <file>' (prova, poi --applica "
              "--conferma).")
        return
    if args.azione == "apri":
        target = Path(args.testo) if args.testo else None
        if target is None or not target.exists():
            raise LoxoneError("indica il file .Loxone da aprire in Loxone Config")
        exe = Path(os.environ.get("LOXONE_CONFIG_EXE", DEFAULT_CONFIG_EXE))
        if not exe.exists():
            raise LoxoneError(f"Loxone Config non trovato in {exe} (imposta LOXONE_CONFIG_EXE)")
        subprocess.Popen([str(exe), str(target.resolve())])
        print(f"Aperto {target} in Loxone Config. Il Miniserver NON e' stato modificato: per applicare "
              "il programma premi 'Salva nel Miniserver' in Config.")
        return
    prog, src = load_program(args, s)
    if args.azione == "riepilogo":
        summ = prog.summary()
        if args.json:
            print(json.dumps(summ, ensure_ascii=False, indent=2))
            return
        print(f"Programma {src}: progetto '{summ['progetto']}', Miniserver '{summ['miniserver']}'")
        print(f"  Stanze: {', '.join(summ['stanze'])}")
        print(f"  Categorie: {', '.join(summ['categorie'])}")
        print(f"  Utenti configurati: {summ['utenti_configurati']} (dettagli esclusi per privacy)")
        print("  Pagine di programma:")
        for p in summ["pagine"]:
            print(f"   - {p['titolo']}: {p['oggetti']} oggetti {p['tipi']}")
        print("  Hardware e I/O:")
        for cap, items in summ["hardware_e_io"].items():
            print(f"   [{cap}] " + "; ".join(items))
        top = list(summ["blocchi_per_tipo"].items())[:40]
        print("  Oggetti per tipo: " + ", ".join(f"{t}={n}" for t, n in top))
    elif args.azione == "cerca":
        if not args.testo:
            raise LoxoneError("indica il testo da cercare")
        hits = prog.find(args.testo)
        print(f"{len(hits)} risultati per '{args.testo}' nel programma {src}:")
        for e in hits[:100]:
            print(f"  - {prog.title(e)} <{e.get('Type')}> in {prog.path(e)}  uuid={e.get('U')}")
    elif args.azione == "blocco":
        if not args.testo:
            raise LoxoneError("indica il nome del blocco")
        hits = [prog.by_uuid[args.testo]] if args.testo in prog.by_uuid else prog.find(args.testo)
        if not hits:
            raise LoxoneError(f"nessun blocco '{args.testo}'")
        for e in hits[:10]:
            print(json.dumps(prog.wiring(e, detailed=True), ensure_ascii=False, indent=2))
    elif args.azione == "pagina":
        if not args.testo:
            raise LoxoneError("indica il nome della pagina")
        page = prog.page(args.testo)
        print(f"Pagina '{page.get('Title')}' (programma {src}):")
        for e in page:
            if e.tag != "C":
                continue
            w = prog.wiring(e, detailed=True)
            print(f"- {w['titolo']} <{w['tipo']}>" + (f"  formula: {w['formula']}" if "formula" in w else "")
                  + (f"  rif: {w['riferimento']}" if "riferimento" in w else ""))
            if w.get("parametri"):
                print("    parametri: " + ", ".join(f"{k}={v}" for k, v in w["parametri"].items()))
            for line in w["ingressi"] + w["uscite"]:
                print(f"    {line}")


LOG_TS_RE = re.compile(r"^\ufeff?(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def activation_outcome(log_text: str, since: datetime | None = None) -> dict:
    """Esito dell'ultimo riavvio della logica, letto dal log (solo le righe successive al riavvio).

    Con `since` conta solo un riavvio registrato da quel momento in poi (riavvio_visto=False se non c'e' ancora).
    """
    lines = log_text.splitlines()

    def recent(ln: str) -> bool:
        m = LOG_TS_RE.match(ln)
        return since is None or (m is not None and datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") >= since)

    restarts = [i for i, ln in enumerate(lines)
                if ("Restart SPS by user" in ln or "PRG got restart command" in ln) and recent(ln)]
    if not restarts and since is not None:
        return {"errori": None, "caricato": False, "avviato": False, "emergenza": False, "riavvio_visto": False}
    start = restarts[-1] if restarts else max(len(lines) - 80, 0)
    errors, started, emergency, loaded = None, False, False, False
    for ln in lines[start:]:
        m = re.search(r"Errors:\s*(\d+)", ln)
        if m:
            errors = max(errors or 0, int(m.group(1)))
        if "Loading sps_new.zip" in ln:
            loaded = True
        if "Program started" in ln:
            started = True
        if "Emergency" in ln:
            emergency = True
    return {"errori": errors, "caricato": loaded, "avviato": started, "emergenza": emergency,
            "riavvio_visto": bool(restarts)}


CUSTOM_DIRS = ("sys/custom", "prog/custom")


def program_saved(path: Path) -> datetime | None:
    """Momento in cui il Miniserver ha salvato il programma, dal nome del file (sps_<versione>_<data>)."""
    m = backups.PROGRAM_RE.match(path.name)
    return datetime.strptime(m.group(2), "%Y%m%d%H%M%S") if m else None


def pending_app_changes(s, current: Program, saved: datetime | None = None) -> tuple[list[str], int]:
    """Modifiche fatte dall'app (atmosfere, orari, regole...) non ancora dentro il programma `current`, e quanti
    telefoni si sono registrati per le notifiche push dopo il suo salvataggio (`saved`).

    Il Miniserver le tiene in /sys/custom, un file per oggetto, e le CANCELLA quando carica un nuovo programma
    ("Remove old custom changes"): Loxone Config le integra prima di salvare, il caricamento diretto no.
    Contano solo i file piu' recenti del programma (gli altri sono gia' dentro: il Miniserver li lascia li') e
    diversi da cio' che il programma contiene. Un file che non si riesce a leggere conta come modifica in sospeso.
    """
    pending, registrations = [], 0
    saved_minute = saved.replace(second=0, microsecond=0) if saved else None
    with open_ms(s) as ms:
        for folder in CUSTOM_DIRS:
            for f in ms.list_files(folder):
                name = str(f.get("filename") or "")
                if f.get("dir") or not name or "/" in name or "\\" in name or name.startswith("."):
                    continue
                when = listing_time(str(f.get("date") or ""))
                if saved_minute and when and when <= saved_minute:
                    continue  # scritto prima del salvataggio del programma (o nello stesso minuto, all'avvio)
                try:
                    kind, label = programmod.custom_change(
                        decompress_loxcc(ms.http_download(f"dev/fsget/{folder}/{name}")), current)
                except (LoxoneError, ValueError, IndexError, SyntaxError) as e:  # LoxCC o XML non validi
                    kind, label = "in sospeso", f"{name} (non leggibile: {e})"
                if kind == "registrazione":
                    registrations += 1
                elif kind == "in sospeso":
                    pending.append(label)
    return sorted(set(pending)), registrations


def cmd_upload(args, s):
    src_path = Path(args.file) if args.file else None
    if src_path is None:
        raise LoxoneError("indica il file del programma da caricare (.Loxone/.zip/.LoxCC)")
    new_xml, _ = read_program(src_path)
    new_prog = Program(new_xml)  # deve essere un programma valido
    print(f"Programma da caricare: {src_path.name} (progetto '{new_prog.summary()['progetto']}', "
          f"{len(new_prog.components())} oggetti)")

    # 1) backup della versione ATTUALE del Miniserver (per confronto e ripristino)
    backup = download_current_program(s)
    cur_xml, cur_app = read_program(backup)
    print(f"Backup della versione attuale: {backup.name}")
    cur_prog = Program(cur_xml)
    d = programmod.diff(cur_prog, new_prog)
    print_diff(d)
    access = programmod.access_changes(cur_prog, new_prog)
    if not (d["aggiunti"] or d["rimossi"] or d["modificati"] or access):
        print("Nessuna differenza rispetto al programma attuale: niente da caricare.")
        return
    if access:
        print("ATTENZIONE: il file cambia utenti o permessi (" + "; ".join(access) + "). Il caricamento diretto "
              "non li tocca: si cambiano con Loxone Config.")
    old_format = format_mismatch(cur_prog, new_prog)
    if old_format:
        print(f"ATTENZIONE: {old_format}")
    if (new_prog.version or "").isdigit() and (cur_prog.version or "").isdigit() and \
            int(new_prog.version) < int(cur_prog.version):
        print(f"ATTENZIONE: il file viene dalla versione {new_prog.version}, sul Miniserver c'e' la "
              f"{cur_prog.version}: cio' che e' cambiato nel frattempo verrebbe perso (vedi il confronto).")
    # la struttura dell'app (LoxAPP3.json) la genera Loxone Config e il Miniserver usa quella dello zip:
    # per le modifiche visibili nell'app la aggiorna il toolkit (appstructure), se sa farlo
    impact = programmod.app_impact(cur_prog, new_prog, cur_app)
    new_app, replace = None, {}
    if impact:
        print("La modifica cambia cio' che si vede nell'app Loxone:")
        for line in impact:
            print(f"  - {line}")
        perms = uploader.read_member(backup.read_bytes(), uploader.PERMISSIONS_MEMBER)
        upd = appstructure.update_app(cur_prog, new_prog, cur_app, perms) if cur_app else None
        if upd is None or upd.unsupported:
            print("ATTENZIONE: il caricamento diretto non sa riportare nell'app queste modifiche:")
            for line in (upd.unsupported if upd else ["nel programma attuale manca la struttura dell'app"]):
                print(f"  - {line}")
            print("Per queste usa 'lox programma apri <file>' e 'Salva nel Miniserver' in Loxone Config.")
        else:
            new_app, impact = upd.data, []
            replace[uploader.APP_MEMBER] = upd.data
            if upd.permissions is not None:
                replace[uploader.PERMISSIONS_MEMBER] = upd.permissions
            print("Struttura dell'app e permessi aggiornati dal toolkit (come li scriverebbe Loxone Config):")
            for line in upd.done:
                print(f"  + {line}")
    app_changes, registrations = pending_app_changes(s, cur_prog, program_saved(backup))
    if app_changes:
        print(f"ATTENZIONE: sul Miniserver ci sono {len(app_changes)} modifiche fatte dall'app (atmosfere, orari, "
              "regole...) non ancora integrate nel programma: il caricamento diretto le cancellerebbe.")
        for line in app_changes[:20]:
            print(f"  - {line}")
        print("Usa Loxone Config, che le integra prima di salvare.")
    if registrations:
        print(f"Nota: {registrations} registrazioni di telefoni per le notifiche push sono piu' recenti del "
              "programma. Con il caricamento il Miniserver le azzera: ogni telefono si registra di nuovo quando "
              "si riapre l'app, fino ad allora potrebbe non ricevere le notifiche.")
    # il caricamento diretto usa l'FTP del Miniserver, che dal firmware 16.1 e' spento finche' non lo si attiva
    ftp_ok = uploader.ftp_reachable(s.host)
    if not ftp_ok:
        print("ATTENZIONE: il server FTP del Miniserver non risponde. Dal firmware 16.1 e' disattivato per "
              "impostazione predefinita: attivalo in Loxone Config (proprieta' del Miniserver, server FTP: "
              "\"Enabled - TLS only\") e salva nel Miniserver, oppure carica il programma con Loxone Config.")

    # 2) controlli di sicurezza prima di toccare il Miniserver
    alarm_problem = "stato dell'antifurto non leggibile"
    if has_credentials(s):
        try:
            st, ms = live_states(s)
            with ms:
                blocks = sum(1 for e in cur_prog.components() if e.get("Type") == "Alarm")
                alarm_problem = alarm_block_reason(st, ms.values, ms.texts, blocks)
        except (LoxoneError, OSError, websocket.WebSocketException):
            pass
    built = uploader.build_program_zip(backup.read_bytes(), new_xml, replace)
    print(f"Pacchetto sps_new.zip pronto: {len(built)} byte.")

    ftp_user, ftp_pw = s.upload_creds()
    no_app = cur_app is None  # senza la struttura dell'app nel pacchetto il Miniserver resterebbe senza
    if not args.applica:
        if old_format or access:
            print("\n[PROVA] Nulla inviato al Miniserver. Questo file non va caricato direttamente (vedi sopra): "
                  "rifai la modifica partendo dal programma attuale ('lox programma scarica') o usa Loxone Config.")
        elif impact or app_changes or no_app:
            print("\n[PROVA] Nulla inviato al Miniserver. Questa modifica va caricata con Loxone Config (vedi sopra)."
                  + (" Nel programma attuale manca la struttura dell'app." if no_app else ""))
        elif not ftp_ok:
            print("\n[PROVA] Nulla inviato al Miniserver. Per il caricamento diretto va prima attivato l'FTP "
                  "(vedi sopra).")
        else:
            print("\n[PROVA] Nulla inviato al Miniserver. Tutto pronto per il caricamento diretto.")
            print("Per attivarlo davvero: aggiungi --applica --conferma (riavvia la logica per ~10 s: alcune luci "
                  "possono cambiare).")
            if not (ftp_user and ftp_pw):
                print("Prima pero' serve la password dell'utente nel file .env (LOXONE_PASSWORD): il caricamento "
                      "usa l'FTP e un token amministratore temporaneo.")
        if alarm_problem:
            print(f"ATTENZIONE: {alarm_problem}: non caricare ora.")
        return

    if alarm_problem and not args.forza:
        raise LoxoneError(f"{alarm_problem}: caricamento annullato (usa --forza per ignorare)")
    if access:
        raise LoxoneError("caricamento annullato: il file cambia utenti o permessi (vedi sopra). Usa Loxone Config.")
    if old_format:
        raise LoxoneError("caricamento annullato: il file e' in un formato diverso da quello del programma attivo "
                          "(vedi sopra). Rifai la modifica partendo da 'lox programma scarica'.")
    if no_app:
        raise LoxoneError("caricamento diretto annullato: nel programma attuale manca la struttura dell'app "
                          "(LoxAPP3.json). Usa Loxone Config ('lox programma apri <file>' e 'Salva nel Miniserver').")
    if impact and not args.ignora_app:
        raise LoxoneError("caricamento diretto annullato: la modifica riguarda controlli visibili nell'app (vedi "
                          "sopra). Usa Loxone Config, oppure --ignora-app se sei certo che l'app non cambi.")
    if app_changes:
        raise LoxoneError("caricamento diretto annullato: cancellerebbe le modifiche fatte dall'app (vedi sopra). "
                          "Usa Loxone Config ('lox programma apri <file>' e 'Salva nel Miniserver').")
    if not ftp_ok:
        raise LoxoneError("caricamento diretto annullato: il server FTP del Miniserver non risponde (vedi sopra)")
    if not args.conferma:
        raise LoxoneError("il caricamento riavvia la logica di casa: aggiungi --conferma (serve il consenso dell'utente)")

    admin = dataclasses.replace(s, user=ftp_user, password=ftp_pw)
    # verifica dei diritti PRIMA di caricare: mai lasciare un sps_new.zip che non si puo' attivare
    try:
        with Miniserver(admin).connect_admin() as ms:
            ms.kill_token(forget_saved=False)  # revoca subito il token temporaneo di verifica
    except AuthError as e:
        raise LoxoneError(f"caricamento annullato prima di iniziare: {e}")
    lights_before = read_lights(s)
    print(f"\nDiritti admin verificati per '{ftp_user}'. Carico sps_new.zip via FTP...")
    # un sps_new.zip rimasto sulla scheda (incompleto, o mai attivato) verrebbe caricato a un riavvio successivo:
    # se qualcosa va storto prima del riavvio della logica, va tolto
    try:
        res = uploader.ftp_put_program(s.host, ftp_user, ftp_pw, built)
    except (OSError, EOFError, ftplib.Error) as e:
        remove_leftover_upload(s, ftp_user, ftp_pw, quiet=True)
        raise LoxoneError(f"upload FTP interrotto ({e}): nulla e' stato attivato, la casa usa il programma di prima")
    if not res.verified:
        remove_leftover_upload(s, ftp_user, ftp_pw, quiet=True)
        raise LoxoneError("upload FTP non verificato (dimensione diversa sul Miniserver): file tolto, nulla e' "
                          "stato attivato")
    print(f"  caricato e verificato ({res.uploaded} byte). Riavvio la logica...")

    restart_at = datetime.now() - timedelta(seconds=120)  # margine per orologi di PC e Miniserver non allineati
    try:
        ms = Miniserver(admin).connect_admin()
    except (LoxoneError, OSError, websocket.WebSocketException) as e:
        remove_leftover_upload(s, ftp_user, ftp_pw, quiet=True)
        raise LoxoneError(f"non riesco a collegarmi per riavviare la logica ({e}): programma caricato tolto, "
                          "nulla e' stato attivato")
    with ms:
        ms.restart_program()

    # VERITA' VERA: il programma attivo sul Miniserver coincide con quello che volevo caricare?
    # (il Miniserver lo rinomina solo dopo averlo avviato; il riavvio dura circa 10 s)
    print("  riavvio inviato, attendo che il Miniserver avvii il nuovo programma...")
    matches = wait_for_program(s, new_prog)
    # il log e' un controllo aggiuntivo (programma di emergenza, errori). Il Miniserver lo scrive sulla
    # scheda con qualche secondo di ritardo: va riletto finche' non compare l'avvio dopo il riavvio
    outcome: dict = {}
    for _ in range(12):
        try:
            log_text = uploader.ftp_download(s.host, ftp_user, ftp_pw, "/log/def.log").decode("utf-8", "replace")
            outcome = activation_outcome(log_text, since=restart_at)
        except (OSError, EOFError, ftplib.Error):
            pass
        if outcome.get("avviato") or outcome.get("emergenza"):
            break
        time.sleep(5)
    errs = outcome.get("errori")
    print(f"Programma attivo coincide col caricato: {matches} | log: {outcome}")

    if matches and not outcome.get("emergenza") and (errs is None or errs == 0):
        if not outcome.get("avviato"):
            warn("il log non mostra ancora l'avvio, ma il programma attivo e' quello caricato")
        if new_app is not None:
            check_app_structure(s, new_app)
        report_lights(s, lights_before)
        print("CARICAMENTO DIRETTO RIUSCITO.")
        return

    # rollback: riporta la versione precedente (anche se il nuovo non si e' attivato)
    print("PROBLEMA nell'attivazione: ripristino la versione precedente...")
    back_ok = False
    try:
        rb = uploader.build_program_zip(backup.read_bytes(), cur_xml)
        uploader.ftp_put_program(s.host, ftp_user, ftp_pw, rb)
        with Miniserver(admin).connect_admin() as ms:
            ms.restart_program()
        back_ok = wait_for_program(s, Program(cur_xml))
    except (LoxoneError, OSError, EOFError, ftplib.Error, websocket.WebSocketException) as e:
        warn(f"ripristino interrotto: {e}")
    if back_ok:
        print("Versione precedente ripristinata e verificata.")
        report_lights(s, lights_before)
        raise LoxoneError("attivazione fallita: ripristinata la versione precedente. Controlla il file e riprova.")
    raise LoxoneError("attivazione fallita e ripristino NON confermato: controlla subito l'impianto. La versione "
                      f"precedente e' in {backup}: si ricarica con Loxone Config ('lox programma apri' dopo "
                      "'lox programma estrai') oppure con 'lox carica'.")


def read_lights(s) -> dict[str, dict]:
    """Stato dei controlli luci, per dire dopo un riavvio della logica se qualcuno e' cambiato. {} se non si legge."""
    try:
        st, ms = live_states(s)
        with ms:
            out = {}
            for c in st.find(type_="LightControllerV2"):
                states = st.states_of(c, ms.values, ms.texts)
                try:
                    moods = json.loads(states.get("activeMoods") or "[]")
                except (TypeError, ValueError):
                    moods = []
                out[c["_uuid"]] = {"nome": c["name"], "stato": summarize(c, states),
                                   "scene": moods if isinstance(moods, list) else []}
            return out
    except (LoxoneError, OSError, websocket.WebSocketException):
        return {}


def light_changes(before: dict, after: dict) -> list[str]:
    """Luci diverse da prima del riavvio, con il comando per tornare indietro quando c'era una scena sola."""
    lines = []
    for uid, old in before.items():
        new = after.get(uid)
        if new and new["stato"] != old["stato"]:
            back = (f"per tornare: lox comando {uid} changeTo/{old['scene'][0]}" if len(old["scene"]) == 1
                    else "prima non aveva una scena attiva")
            lines.append(f"{old['nome']}: {old['stato']} -> {new['stato']} ({back})")
    return lines


def report_lights(s, before: dict) -> None:
    """Il riavvio puo' riaccendere un controllo luci col dimmer collegato al master: chi e' in casa lo deve sapere."""
    changed = light_changes(before, read_lights(s))
    if changed:
        print("ATTENZIONE: il riavvio della logica ha cambiato queste luci (nessun comando inviato per rimetterle):")
        for line in changed:
            print(f"  - {line}")


def alarm_block_reason(st: Structure, values: dict, texts: dict, blocks: int = 0) -> str | None:
    """Perche' l'antifurto impedisce di riavviare la logica adesso (None = via libera). Nel dubbio ci si ferma:
    stato che non arriva, antifurto del programma che questo utente non vede, inserimento in corso, allarme."""
    alarms = st.find(type_="Alarm")
    if len(alarms) < blocks:
        return f"{blocks - len(alarms)} antifurto del programma non visibili a questo utente: stato non leggibile"
    for c in alarms:
        state = st.states_of(c, values, texts)
        if state.get("armed") is None:
            return f"stato dell'antifurto '{c['name']}' non leggibile"
        if state.get("armed"):
            return f"antifurto '{c['name']}' INSERITO"
        if state.get("level"):
            return f"antifurto '{c['name']}' IN ALLARME"
        if state.get("armedDelay"):
            return f"antifurto '{c['name']}' in fase di inserimento"
    return None


def format_mismatch(current: Program, new: Program) -> str | None:
    """Il programma da caricare e' nello stesso formato di quello attivo? (attributo LxAV della radice)

    A ogni aggiornamento del firmware il Miniserver converte il programma (misurato il 21/11/2026: da LxAV 87 a
    88, con oggetti e attributi nuovi). Una copia preparata prima dell'aggiornamento, o una versione vecchia da
    ripristinare, e' nel formato precedente: Loxone Config la convertirebbe aprendola, il caricamento diretto no.
    """
    cur, want = current.root.get("LxAV"), new.root.get("LxAV")
    if not cur or not want or cur == want:
        return None
    return (f"il programma da caricare e' nel formato {want}, quello attivo sul Miniserver nel formato {cur}: e' "
            "stato preparato prima di un aggiornamento del firmware (o con un'altra versione di Loxone Config). "
            "Il caricamento diretto non converte i programmi: aprilo e salvalo con Loxone Config, oppure rifai la "
            "modifica partendo dal programma attuale.")


def remove_leftover_upload(s, user: str, password: str, quiet: bool = False) -> None:
    """Toglie /prog/sps_new.zip se il Miniserver non l'ha consumato (di solito lo carica e lo rinomina).

    Lasciato li', verrebbe caricato a un riavvio successivo, cancellando cio' che e' cambiato nel frattempo."""
    path = f"{uploader.FTP_DIR}/{uploader.UPLOAD_NAME}"
    try:
        removed = uploader.ftp_delete(s.host, user, password, path)
    except (OSError, EOFError, ftplib.Error) as e:
        warn(f"non riesco a controllare se {path} e' rimasto sul Miniserver ({e}): se c'e', cancellalo "
             "(con Loxone Config o un programma FTP) prima di riavviare il Miniserver")
        return
    if removed and not quiet:  # se non c'era, il Miniserver l'ha caricato e rinominato, come previsto
        warn(f"il Miniserver non aveva caricato {path}: l'ho tolto perche' non venga caricato a un riavvio "
             "successivo")


def check_app_structure(s, app_json: bytes) -> None:
    """Dopo un caricamento con la struttura dell'app aggiornata: e' quella che il Miniserver da' alle app?"""
    expected = json.loads(app_json.decode("utf-8"))
    try:
        with open_ms(s) as ms:
            served = ms.structure(refresh=True)
    except (LoxoneError, OSError, websocket.WebSocketException) as e:
        warn(f"struttura dell'app non verificata ({e})")
        return
    # il file nello zip usa chiavi "U:<uuid>", quello che il Miniserver da' alle app no: si confronta uuidAction
    served_uuids = {c.get("uuidAction") for c in served.get("controls", {}).values()}
    missing = [c["name"] for c in expected["controls"].values() if c.get("uuidAction") not in served_uuids]
    if served.get("lastModified") != expected["lastModified"] or missing:
        warn(f"il Miniserver non mostra la struttura caricata (ultima modifica {served.get('lastModified')}, "
             f"mancano {missing[:5]}): salva il programma con Loxone Config")
    else:
        print(f"Struttura dell'app attiva: ultima modifica {served['lastModified']}, "
              f"{len(served['controls'])} controlli (le app la scaricano da sole)")


def upload_pending(s) -> bool:
    """/prog/sps_new.zip e' ancora li'? Il Miniserver lo rinomina solo dopo averlo caricato e avviato."""
    with open_ms(s) as ms:
        return any(f.get("filename") == uploader.UPLOAD_NAME for f in ms.list_files("prog"))


def wait_for_program(s, expected: Program, seconds: float = 120) -> bool:
    """Attende che il Miniserver abbia avviato il pacchetto caricato e che il programma attivo sia `expected`.

    Il solo confronto non basta: in un ripristino il file piu' recente puo' essere gia' la versione giusta prima
    ancora del riavvio. La prova e' che sps_new.zip sia stato consumato. Durante il riavvio ogni errore vuol dire
    "riprova"; gli elementi interni dei blocchi non contano perche' qui si verifica quale programma gira.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(5)
        try:
            if upload_pending(s):
                continue
            active = Program(read_program(download_current_program(s))[0])
        except (LoxoneError, OSError, EOFError, websocket.WebSocketException, ValueError, LookupError,
                zipfile.BadZipFile, ElementTree.ParseError):
            continue
        d = programmod.diff(expected, active, children=False)
        if not (d["aggiunti"] or d["rimossi"] or d["modificati"]):
            return True
    return False


def cmd_raw(args, s):
    if not any(p.match(args.comando) for p in READONLY_RAW) and not args.conferma:
        raise LoxoneError("comando non in elenco di sola lettura: ripeti con --conferma solo se sei sicuro "
                          "e l'utente ha dato il consenso (alcuni comandi riavviano o modificano il Miniserver)")
    # "dev/..." risponde spesso in XML, "jdev/..." e' lo stesso comando con la risposta in JSON
    command = f"j{args.comando}" if args.comando.startswith("dev/") else args.comando
    with open_ms(s) as ms:
        print(json.dumps(ms.request(command), ensure_ascii=False, indent=2))


def ping(host: str, count: int = 1, wait_s: float = 1.0) -> bool:
    """Un dispositivo risponde al ping? Le opzioni cambiano da un sistema all'altro (su macOS -W e' in
    millisecondi, su Linux in secondi) e Windows esce con 0 anche per "host di destinazione non raggiungibile"."""
    if os.name == "nt":
        flags = ["-n", str(count), "-w", str(int(wait_s * 1000))]
    elif sys.platform == "darwin":
        flags = ["-c", str(count), "-W", str(int(wait_s * 1000))]
    else:
        flags = ["-c", str(count), "-W", str(max(1, round(wait_s)))]
    try:
        r = subprocess.run(["ping", *flags, host], capture_output=True, text=True, errors="replace",
                           timeout=count * (wait_s + 2) + 5)
    except (OSError, subprocess.TimeoutExpired):  # comando ping assente o bloccato
        return False
    return "TTL=" in r.stdout.upper() if os.name == "nt" else r.returncode == 0


def host_reachable(host: str, port: int, proto: str) -> bool:
    """TCP: prova ad aprire la porta. UDP: non ha risposta, quindi usa un ping."""
    if proto == "tcp":
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            return False
    return ping(host, count=2, wait_s=1.5)


def shelly_info(host: str) -> str | None:
    """Modello di uno Shelly dalla sua pagina pubblica /shelly (non richiede password)."""
    try:
        with urllib.request.urlopen(f"http://{host}/shelly", timeout=3) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not ({"type", "model", "gen"} & d.keys()):
        return None
    model = d.get("model") or d.get("type")
    extra = f" ({d['app']})" if d.get("app") else ""
    return f"Shelly gen{d.get('gen', 1)} {model}{extra}, firmware {d.get('ver') or d.get('fw')}"


def check_devices(s) -> int | None:
    """Stampa lo stato dei dispositivi esterni del programma; restituisce quanti sono irraggiungibili
    (None se non c'e' un programma locale da cui leggerli)."""
    p = newest_program(s)
    if not p:
        print("Dispositivi esterni: non verificati, manca una copia locale del programma "
              "(esegui 'lox programma scarica').")
        return None
    xml, _ = read_program(p["path"])
    devices = Program(xml).device_hosts()
    by_target: dict[tuple, list[str]] = {}
    for d in devices:  # UDP: un solo ping per host, qualunque sia la porta
        port = d["porta"] if d["proto"] == "tcp" else 0
        by_target.setdefault((d["host"], port, d["proto"]), []).append(d["titolo"])
    print(f"Dispositivi esterni usati dal programma ({len(by_target)} indirizzi):")
    missing = 0
    for (host, port, proto), titles in sorted(by_target.items()):
        ok = host_reachable(host, port, proto)
        missing += not ok
        what = f"porta {port}" if proto == "tcp" else "ping"
        model = shelly_info(host) if ok and proto == "tcp" and port == 80 else None
        print(f"  {'OK         ' if ok else 'IRRAGGIUNG.'} {host} ({what}): {', '.join(sorted(set(titles)))}"
              + (f"  -> {model}" if model else ""))
    return missing


def cmd_devices(args, s):
    missing = check_devices(s)
    if missing is not None:
        print("Tutti i dispositivi rispondono." if not missing else f"{missing} indirizzi non rispondono.")


def expected_shelly(s) -> dict[str, dict]:
    """IP HTTP previsti dal programma -> nomi dei comandi che li usano ('titoli') e tipo di Shelly ('tipo')."""
    p = newest_program(s)
    if not p:
        return {}
    hosts: dict[str, dict] = {}
    for d in Program(read_program(p["path"])[0]).device_hosts():
        if d["proto"] == "tcp" and d["porta"] == 80:
            h = hosts.setdefault(d["host"], {"titoli": set(), "comandi": []})
            h["titoli"].add(d["titolo"] or "?")
            h["comandi"] += d["comandi"]
    return {ip: {"titoli": sorted(h["titoli"]), "tipo": shellymod.program_kind(h["comandi"])}
            for ip, h in hosts.items() if shellymod.looks_like_shelly(h["comandi"])}


def lan_prefix(s) -> str:
    """Prima parte dell'indirizzo della rete di casa (es. "192.168.1."), per cercare i dispositivi."""
    try:
        ip = ipaddress.IPv4Address(s.host.split(":")[0])
    except ValueError:
        raise LoxoneError("per cercare gli Shelly LOXONE_HOST deve essere un indirizzo IP (es. 192.168.1.10)")
    return str(ip).rsplit(".", 1)[0] + "."


SHELLY_WIFI_LOG = DATA_DIR / "shelly-wifi.log"
SHELLY_RESET_HELP = """
Ora metti in configurazione gli Shelly, uno alla volta: questa finestra li trova e li collega da sola.
  - lampadine: accendi l'interruttore, aspetta 5 secondi, spegni, aspetta 5 secondi; ripeti 5 volte e
    lascia acceso. Se dopo un minuto qui non succede niente, riprova con accensioni piu' brevi (1-2 secondi).
  - sensore di movimento (Shelly Motion): tieni premuto il suo tasto per 5 secondi (se non basta, 10).
  - presa (Plug S): staccala e riattaccala, poi entro un minuto tieni premuto il tasto per 10 secondi.
Quando uno Shelly e' collegato, se e' una luce lampeggia 3 volte.
"""


def address_in_use(ip: str) -> bool:
    """Qualcosa risponde su questo indirizzo (porta 80 o ping)."""
    if not shellymod.is_free(ip):
        return True
    return ping(ip, count=1, wait_s=0.8)


def ask_wifi_password(ssid: str) -> str:
    """La password del Wi-Fi la scrive l'utente qui, due volte; resta in memoria solo durante la procedura."""
    if not interactive():
        raise LoxoneError("la password del Wi-Fi va scritta in una finestra del terminale: avvia la procedura con "
                          "doppio clic sul file del Desktop")
    for _ in range(3):
        first = getpass.getpass(f'Password del Wi-Fi "{ssid}" (mentre scrivi non compare nulla), poi Invio: ')
        if getpass.getpass("Scrivila di nuovo, per controllo: ") != first:
            print("Le due password sono diverse: riprova.")
        elif not 8 <= len(first) <= 64:
            print("Una password Wi-Fi ha da 8 a 63 caratteri: riprova.")
        else:
            return first
    raise LoxoneError("password del Wi-Fi non confermata")


def setup_shelly_ap(phone, ap: dict, home: str, password: str, hosts: dict, todo: list[str], gateway: str,
                    prova: bool, log) -> bool:
    """Collega al Wi-Fi di casa lo Shelly della rete di configurazione 'ap'. False = non e' comparso in rete."""
    if not telefono.is_open(ap):
        log(f"{ap['ssid']} ha una password: per configurarlo serve il reset completo dello Shelly.")
        return True
    log(f"Trovata la rete {ap['ssid']} (segnale {ap['rssi']} dBm): il telefono si collega...")
    try:
        phone.connect_open(ap["ssid"])
        info = None
        for _ in range(6):
            code, text = phone.http(f"http://{shellymod.AP_HOST}/shelly", timeout=5)
            info = shellymod.device_info(shellymod.AP_HOST, shellymod._loads(text)) if code == 200 else None
            if info:
                break
            time.sleep(2)
        if not info:
            raise telefono.PhoneError(f"nessuno Shelly risponde su {shellymod.AP_HOST}")
        if not shellymod.ssid_matches(ap["ssid"], info["mac"]):  # la password del Wi-Fi non va a uno sconosciuto
            log(f"{ap['ssid']}: il nome della rete non corrisponde al dispositivo (MAC {info['mac']}): non gli mando "
                "la password del Wi-Fi.")
            return True
        kind = shellymod.device_kind(info)
        label = f"{info['model']}{' (' + info['app'] + ')' if info['app'] else ''} gen{info['gen']} MAC {info['mac']}"
        target = next((ip for ip in todo if hosts[ip]["tipo"] == kind), None)
        if not target:
            log(f"{label}: il programma non prevede altri indirizzi liberi per il tipo '{kind}': lo lascio cosi'.")
            return True
        names = ", ".join(hosts[target]["titoli"])
        log(f"{label}: lo collego a \"{home}\" con IP fisso {target} ({names}).")
        if prova:
            log("[prova] nessuna modifica inviata.")
            return True
        how = shellymod.configure_wifi(phone.http, info, home, password, target, gateway, gateway)
        log("Lo Shelly ha confermato la rete." if how == "ok" else
            "Dati inviati (lo Shelly ha cambiato rete senza rispondere).")
    finally:
        phone.forget(ap["ssid"])  # il telefono torna da solo alla rete di casa, che ha salvata
        if not phone.wait_for_network(home, 60):
            log(f"ATTENZIONE: il telefono non e' tornato su \"{home}\": controllalo.")
    log(f"Attendo lo Shelly su {target} (fino a 2 minuti e mezzo)...")
    dev = shellymod.wait_for(target, info["mac"], seconds=150)
    if not dev:  # collegato ma senza l'IP fisso?
        other = next((d for d in shellymod.scan(target.rsplit(".", 1)[0] + ".") if d["mac"] == info["mac"]), None)
        if other:
            log(f"Lo Shelly e' in rete su {other['ip']}: gli assegno {target}.")
            shellymod.set_static_ip(other, target, gateway, gateway)
            dev = shellymod.wait_for(target, info["mac"], seconds=60)
    if not dev:
        log(f"NON RISPONDE su {target}. Cause possibili: password sbagliata, rete non visibile a 2,4 GHz da li'.")
        return False
    todo.remove(target)
    if dev["gen"] >= 2 and not shellymod.disable_ap(dev):
        log("ATTENZIONE: la rete di configurazione dello Shelly e' rimasta accesa.")
    # lampeggiano solo le luci: far "lampeggiare" una presa o un rele' toglie corrente a cio' che e' collegato
    log(f"OK: {names} risponde su {target}" + (", lampeggia 3 volte." if kind == "luce" else "."))
    if kind == "luce":
        shellymod.blink(dev)
    return True


def shelly_wifi(args, s, hosts: dict[str, dict], gateway: str) -> None:
    """Procedura guidata: per ogni Shelly resettato il telefono (adb) si collega alla sua rete di configurazione e lo
    collega al Wi-Fi di casa con l'IP fisso previsto dal programma, poi lo ritrova in rete e lo fa lampeggiare."""
    def log(msg: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        with SHELLY_WIFI_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%d/%m/%Y} {line}\n")

    DATA_DIR.mkdir(exist_ok=True)
    prefix = lan_prefix(s)
    hosts = {ip: h for ip, h in hosts.items() if re.fullmatch(re.escape(prefix) + r"\d+", ip)}
    order = [ip if "." in ip else prefix + ip for ip in (args.ordine or "").replace(" ", "").split(",") if ip]
    unknown = [ip for ip in order if ip not in hosts]
    if unknown:
        raise LoxoneError(f"indirizzi non previsti dal programma: {', '.join(unknown)}")
    order += sorted((ip for ip in hosts if ip not in order), key=lambda ip: int(ip.rsplit(".", 1)[1]))
    phone = telefono.Phone(serial=args.telefono)
    home = args.ssid or phone.status()["ssid"]
    if not home or shellymod.SETUP_SSID_RE.match(home):
        raise LoxoneError("il telefono non e' collegato al Wi-Fi di casa: collegalo oppure indica la rete con --ssid")
    log(f"Telefono {phone.model()} collegato via USB; rete di casa: \"{home}\".")
    if not any(n["ssid"] == home and n["freq"] < 3000 for _ in range(2) for n in phone.scan()):  # scansioni parziali
        warn(f"il telefono non vede \"{home}\" a 2,4 GHz: gli Shelly usano solo i 2,4 GHz (controlla il router)")
    todo = [ip for ip in order if not address_in_use(ip)]
    if not todo:
        log("Tutti gli Shelly previsti dal programma rispondono gia'.")
        return
    log("Da ricollegare, in quest'ordine: " + "; ".join(
        f"{ip} {', '.join(hosts[ip]['titoli'])} ({hosts[ip]['tipo']})" for ip in todo))
    password = "" if args.prova else ask_wifi_password(home)
    print(SHELLY_RESET_HELP, flush=True)
    attempts: dict[str, int] = {}
    deadline = time.monotonic() + args.minuti * 60
    log(f"Cerco le reti di configurazione degli Shelly (fino a {args.minuti:g} minuti)...")
    while todo and time.monotonic() < deadline:
        aps = [n for n in phone.scan() if shellymod.SETUP_SSID_RE.match(n["ssid"]) and attempts.get(n["ssid"], 0) < 3]
        if not aps:
            time.sleep(3)
            continue
        ap = max(aps, key=lambda n: n["rssi"])
        attempts[ap["ssid"]] = attempts.get(ap["ssid"], 0) + 1
        try:
            if setup_shelly_ap(phone, ap, home, password, hosts, todo, gateway, args.prova, log):
                attempts[ap["ssid"]] = 99  # fatto: gli Shelly gen2 tengono accesa la rete fino alla fine
            else:
                log("Mi fermo per non configurare altri Shelly allo stesso modo: controlla la password e riavviami.")
                sys.exit(1)
        except (telefono.PhoneError, ValueError, OSError, subprocess.TimeoutExpired) as e:
            log(f"ERRORE con {ap['ssid']}: {e}")
    if todo:
        log("Tempo scaduto. Non ancora collegati: " + "; ".join(f"{ip} {', '.join(hosts[ip]['titoli'])}" for ip in todo))
        sys.exit(1)
    log("Tutti gli Shelly previsti dal programma sono di nuovo in rete.")


def cmd_shelly(args, s):
    hosts = expected_shelly(s)
    expected = {ip: h["titoli"] for ip, h in hosts.items()}
    gateway = os.environ.get("LOXONE_LAN_GATEWAY") or (lan_prefix(s) + "1" if args.azione in ("ip", "wifi") else "")
    if args.azione == "wifi":
        return shelly_wifi(args, s, hosts, gateway)
    if args.azione == "trova":
        prefix = lan_prefix(s)
        found = shellymod.scan(prefix)
        print(f"Shelly trovati in rete {prefix}0/24: {len(found)}")
        for d in sorted(found, key=lambda d: int(d["ip"].rsplit(".", 1)[1])):
            role = ", ".join(sorted(set(expected.get(d["ip"], [])))) or "nessun comando Loxone su questo IP"
            print(f"  {d['ip']:<15} gen{d['gen']} {d['model']}{' (' + d['app'] + ')' if d['app'] else ''} "
                  f"MAC {d['mac']} IP {d['ipv4']} -> {role}")
        missing = [ip for ip in expected if ip not in {d['ip'] for d in found}]
        if missing:
            print("Indirizzi previsti dal programma ancora liberi: " + ", ".join(sorted(missing)))
        return
    if args.azione == "attendi":
        prefix = lan_prefix(s)
        known = {d["mac"] for d in shellymod.scan(prefix)}
        print(f"In attesa di nuovi Shelly in rete (gia' presenti: {len(known)}), massimo {args.minuti} minuti...",
              flush=True)
        end = time.monotonic() + args.minuti * 60
        while time.monotonic() < end:
            time.sleep(10)
            new = [d for d in shellymod.scan(prefix) if d["mac"] not in known]
            for d in new:
                role = ", ".join(sorted(set(expected.get(d["ip"], [])))) or "IP non ancora assegnato"
                print(f"NUOVO SHELLY: {d['ip']} gen{d['gen']} {d['model']}{' (' + d['app'] + ')' if d['app'] else ''} "
                      f"MAC {d['mac']} IP {d['ipv4']} -> {role}")
            if new:
                return
        print("Nessun nuovo Shelly comparso nel tempo di attesa.")
        sys.exit(1)
    if not args.ip:
        raise LoxoneError("indica l'indirizzo attuale dello Shelly (vedi 'lox shelly trova')")
    dev = shellymod.identify(args.ip)
    if not dev:
        raise LoxoneError(f"nessuno Shelly risponde su {args.ip}")
    if args.azione == "lampeggia":
        if shellymod.device_kind(dev) != "luce" and not args.forza:
            raise LoxoneError(f"{dev['model']} su {args.ip} non e' una luce: farlo lampeggiare toglie e rida' corrente "
                              "tre volte a cio' che e' collegato. Se va bene, ripeti con --forza.")
        print(f"Faccio lampeggiare {dev['model']} su {args.ip} per 3 volte...")
        shellymod.blink(dev)
        print("Fatto.")
        return
    # azione "ip": assegna un indirizzo fisso
    new_ip = args.nuovo
    if not new_ip:
        raise LoxoneError("indica il nuovo indirizzo, es. lox shelly ip 192.168.1.57 192.168.1.50")
    if new_ip not in expected and not args.forza:
        raise LoxoneError(f"{new_ip} non e' tra gli indirizzi usati dal programma ({', '.join(sorted(expected))}); "
                          "usa --forza se sei sicuro")
    if new_ip != args.ip and not shellymod.is_free(new_ip):
        raise LoxoneError(f"{new_ip} e' gia' occupato da un altro dispositivo")
    print(f"Imposto {dev['model']} ({dev['mac']}) da {args.ip} a IP fisso {new_ip} "
          f"(maschera {shellymod.NETMASK}, gateway e DNS {gateway})...")
    shellymod.set_static_ip(dev, new_ip, gateway, gateway)
    back = shellymod.wait_for(new_ip, dev["mac"], seconds=60)
    if back:
        print(f"OK: lo Shelly ora risponde su {new_ip} ({', '.join(sorted(set(expected.get(new_ip, []))))}).")
    else:
        print(f"ATTENZIONE: lo Shelly non risponde ancora su {new_ip}. Riprova 'lox shelly trova' tra un minuto; "
              "se non compare, va ricollegato al Wi-Fi.")
        sys.exit(1)


def cmd_health(args, s):
    print("=== CONTROLLO GENERALE IMPIANTO LOXONE ===")
    try:
        cmd_ping(argparse.Namespace(ws=False), s)
    except OSError as e:
        print(f"Miniserver {s.host} NON raggiungibile: {e}")
    print()
    p = newest_program(s)
    if p:
        age = (datetime.now() - p["saved"]).days
        print(f"Ultimo programma disponibile: v{p['version']} del {p['saved']:%d/%m/%Y %H:%M} "
              f"({age} giorni fa, {p['origine']}). Per aggiornarlo: 'lox programma scarica'")
    live = has_credentials(s)
    # log: dal Miniserver se richiesto (--log-live), altrimenti dall'ultimo backup di Config; se la prima scelta
    # non c'e' si prova l'altra, e senza nessuna delle due il controllo prosegue
    text = src = None
    log_problems = []
    for from_ms in ([args.log_live, not args.log_live] if live else [False]):
        try:
            text, src = read_log_text(argparse.Namespace(live=from_ms), s)
            break
        except (LoxoneError, OSError, websocket.WebSocketException) as e:
            log_problems.append(f"{'dal Miniserver' if from_ms else 'dai backup di Config'}: {e}")
    print()
    if text is None:
        print("Log di sistema: non disponibile (" + "; ".join(log_problems) + ")."
              + ("" if live else " Dal Miniserver si legge dopo il login."))
    else:
        print_log_summary(text, src, args.giorni)
    print()
    check_devices(s)
    print()
    if not live:
        print("Stato live dei controlli: non disponibile, manca il login. Esegui 'lox login' in un terminale "
              "(vedi README, \"Installazione\").")
        return
    st, ms = live_states(s)
    with ms:
        problems = []
        for c in st.controls.values():
            states = st.states_of(c, ms.values, ms.texts)
            if isinstance(states.get("error"), (int, float)) and states["error"]:
                problems.append(f"{st.label(c)}: segnala un errore (sensore/valore non valido)")
            if "batter" in c["name"].lower() and isinstance(states.get("value"), (int, float)) and states["value"] < 20:
                problems.append(f"{st.label(c)}: batteria bassa ({states['value']:g})")
        with_values = sum(1 for c in st.top_level() if any(v is not None for v in st.states_of(c, ms.values, ms.texts).values()))
        print(f"Stato live: {len(ms.values) + len(ms.texts)} stati ricevuti, {with_values}/{len(st.top_level())} controlli con valori")
        print("Problemi rilevati sui controlli:" if problems else "Nessun controllo segnala errori.")
        for line in problems:
            print(f"  - {line}")


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lox", description="Toolkit locale per il Miniserver Loxone")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ping", help="raggiungibilita' e versione del Miniserver (nessun login)")
    a.add_argument("--ws", action="store_true", help="prova anche WebSocket e scambio chiavi (senza login)")
    a.set_defaults(fn=cmd_ping)
    sub.add_parser("login", help="ottiene e salva il token di accesso").set_defaults(fn=cmd_login)
    sub.add_parser("logout", help="revoca e cancella il token salvato").set_defaults(fn=cmd_logout)
    sub.add_parser("info", help="informazioni sul Miniserver (login)").set_defaults(fn=cmd_info)

    a = sub.add_parser("stanze", help="elenco delle stanze")
    a.add_argument("--offline", action="store_true", help="usa solo dati locali (cache/backup)")
    a.set_defaults(fn=cmd_rooms)

    a = sub.add_parser("controlli", help="elenco/ricerca dei controlli")
    a.add_argument("testo", nargs="?", help="parte del nome")
    a.add_argument("--stanza"), a.add_argument("--tipo"), a.add_argument("--categoria")
    a.add_argument("--sotto", action="store_true", help="includi i sotto-controlli")
    a.add_argument("--json", action="store_true")
    a.add_argument("--offline", action="store_true")
    a.add_argument("--aggiorna", action="store_true", help="riscarica la struttura dal Miniserver")
    a.set_defaults(fn=cmd_controls)

    a = sub.add_parser("mostra", help="dettagli e stato di un controllo")
    a.add_argument("controllo"), a.add_argument("--stanza")
    a.add_argument("--offline", action="store_true")
    a.set_defaults(fn=cmd_show)

    a = sub.add_parser("stato", help="stato attuale dei controlli (login)")
    a.add_argument("controlli", nargs="*"), a.add_argument("--stanza")
    a.add_argument("--json", action="store_true")
    a.add_argument("--salva", action="store_true", help="salva uno snapshot in data/")
    a.set_defaults(fn=cmd_state)

    a = sub.add_parser("comando", help="invia un comando a un controllo (login)")
    a.add_argument("controllo"), a.add_argument("comando"), a.add_argument("--stanza")
    a.add_argument("--conferma", action="store_true", help="necessario per i controlli di sicurezza")
    a.add_argument("--attesa", type=float, default=2.0, help="secondi di attesa per leggere il nuovo stato")
    a.set_defaults(fn=cmd_command)

    a = sub.add_parser("osserva", help="mostra i cambiamenti di stato in tempo reale (login)")
    a.add_argument("controlli", nargs="*"), a.add_argument("--stanza")
    a.add_argument("--secondi", type=float, default=60)
    a.set_defaults(fn=cmd_watch)

    a = sub.add_parser("log", help="riepilogo del log di sistema del Miniserver")
    a.add_argument("--giorni", type=int, default=90, help="ultimi N giorni (0 = tutto)")
    a.add_argument("--live", action="store_true", help="scarica il log dal Miniserver (login)")
    a.set_defaults(fn=cmd_log)

    a = sub.add_parser("salute", help="controllo generale dell'impianto")
    a.add_argument("--giorni", type=int, default=90)
    a.add_argument("--log-live", action="store_true", help="prova a scaricare il log dal Miniserver (servono permessi)")
    a.set_defaults(fn=cmd_health)

    sub.add_parser("backup", help="backup di Loxone Config presenti sul PC").set_defaults(fn=cmd_backups)
    sub.add_parser("dispositivi", help="raggiungibilita' e modello dei dispositivi esterni (Shelly, ...)"
                   ).set_defaults(fn=cmd_devices)

    a = sub.add_parser("shelly", help="trova gli Shelly in rete, assegna l'IP fisso, li fa lampeggiare, li ricollega "
                                      "al Wi-Fi con il telefono (wifi)")
    a.add_argument("azione", choices=["trova", "attendi", "ip", "lampeggia", "wifi"])
    a.add_argument("ip", nargs="?", help="indirizzo attuale dello Shelly")
    a.add_argument("nuovo", nargs="?", help="nuovo indirizzo fisso (per 'ip')")
    a.add_argument("--forza", action="store_true",
                   help="per 'ip': consente un IP non previsto dal programma; per 'lampeggia': anche prese e rele'")
    a.add_argument("--minuti", type=float, default=30, help="durata massima dell'attesa (per 'attendi' e 'wifi')")
    a.add_argument("--ssid", help="per 'wifi': rete Wi-Fi di casa (default: quella a cui e' collegato il telefono)")
    a.add_argument("--ordine", help="per 'wifi': ordine degli indirizzi da assegnare, es. 224,225,222")
    a.add_argument("--telefono", help="per 'wifi': numero di serie adb del telefono, se ce n'e' piu' di uno")
    a.add_argument("--prova", action="store_true", help="per 'wifi': trova e riconosce gli Shelly senza cambiare niente")
    a.set_defaults(fn=cmd_shelly)

    a = sub.add_parser("programma", help="analisi del programma e modifiche su una copia (il Miniserver non cambia)")
    a.add_argument("azione", choices=["scarica", "riepilogo", "cerca", "blocco", "pagina", "estrai", "confronta",
                                      "parametro", "rimuovi", "apri"])
    a.add_argument("testo", nargs="?", help="testo da cercare / blocco / pagina / file (confronta, apri)")
    a.add_argument("altri", nargs="*", help="altri blocchi da eliminare (per 'rimuovi')")
    a.add_argument("--file", help="programma .zip/.LoxCC/.Loxone di partenza (default: il piu' recente tra "
                                  "quelli scaricati e i backup di Config)")
    a.add_argument("--out", help="destinazione per 'estrai' (cartella) o 'parametro'/'rimuovi' (file)")
    a.add_argument("--connettore", help="nome del parametro/connettore, es. Delay (per 'parametro')")
    a.add_argument("--valore", help="nuovo valore (per 'parametro')")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_program)

    a = sub.add_parser("carica", help="carica un programma sul Miniserver via FTP (prova salvo --applica)")
    a.add_argument("file", help="programma da caricare (.Loxone/.zip/.LoxCC)")
    a.add_argument("--applica", action="store_true", help="esegui davvero (upload + riavvio logica)")
    a.add_argument("--conferma", action="store_true", help="necessario con --applica: riavvia la logica di casa")
    a.add_argument("--forza", action="store_true", help="carica anche con antifurto inserito o non leggibile")
    a.add_argument("--ignora-app", action="store_true",
                   help="carica anche se la modifica riguarda controlli visibili nell'app (sconsigliato)")
    a.set_defaults(fn=cmd_upload)

    a = sub.add_parser("raw", help="webservice grezzo, es. jdev/cfg/version (login)")
    a.add_argument("comando")
    a.add_argument("--conferma", action="store_true", help="necessario per comandi non di sola lettura")
    a.set_defaults(fn=cmd_raw)
    return p


def main(argv: list[str] | None = None) -> None:
    if os.name != "nt":
        os.umask(0o077)  # programmi scaricati, cache e log contengono dati dell'impianto: solo per il proprietario
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    args = build_parser().parse_args(argv)
    try:
        args.fn(args, get_settings())
    except (LoxoneError, LookupError, editor.EditError, LoxCCError, telefono.PhoneError) as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        sys.exit(1)
    except (zipfile.BadZipFile, ElementTree.ParseError) as e:
        print(f"ERRORE: il file non e' un programma Loxone valido ({e})", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Interrotto.", file=sys.stderr)
        sys.exit(130)
    except (OSError, websocket.WebSocketException) as e:
        if isinstance(e, OSError) and e.filename:  # un file del PC, non la rete
            what = "file non trovato" if isinstance(e, FileNotFoundError) else (e.strerror or str(e))
            print(f"ERRORE: {what}: {e.filename}", file=sys.stderr)
            sys.exit(1)
        print(f"ERRORE di connessione: {e}", file=sys.stderr)
        print(f"  Controlla che il Miniserver sia acceso, che LOXONE_HOST nel file .env sia il suo indirizzo "
              f"(ora: {os.environ.get('LOXONE_HOST', '?')}) e che questo computer sia nella stessa rete.",
              file=sys.stderr)
        sys.exit(2)
