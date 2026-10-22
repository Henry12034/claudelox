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

import websocket

from . import appstructure, backups, editor, logs
from . import program as programmod
from . import shelly as shellymod
from . import uploader
from .client import (LOX_EPOCH, STRUCTURE_CACHE, AuthError, LoxoneError, Miniserver, delete_token, explain,
                     ll_code, load_token, loose_json)
from .config import DATA_DIR, Settings, get_settings
from .loxcc import LoxCCError, read_program
from .program import Program
from .structure import (COMMAND_HINTS, READ_ONLY_TYPES, SENSITIVE_TYPES, Structure, mood_list, summarize)

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
    found = backups.latest_structure(s.backups_dir, DOWNLOADS)
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
        if not sys.stdin.isatty():
            raise AuthError("password non presente nel .env: esegui 'lox login' in un terminale interattivo")
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
        flag = " [SICUREZZA]" if c["type"] in SENSITIVE_TYPES else ""
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
            print(f"  dettagli: {json.dumps(c['details'], ensure_ascii=False)[:500]}")
        if ms is not None:
            states = st.states_of(c, ms.values, ms.texts)
            print(f"  stato: {summarize(c, states)}")
            for name, val in states.items():
                print(f"    .{name} = {str(val)[:300]}")
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
        if c["type"] in SENSITIVE_TYPES:
            print("  ATTENZIONE: controllo di sicurezza, i comandi richiedono --conferma "
                  "e il consenso esplicito dell'utente")
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
        if c["type"] in SENSITIVE_TYPES and not args.conferma:
            raise LoxoneError(f"{st.label(c)} e' un controllo di SICUREZZA. Ripeti con --conferma solo dopo "
                              "il consenso esplicito dell'utente.")
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
    for b in reversed(backups.list_backups(s.backups_dir)):
        f = b["path"] / "log" / "def.log"
        if f.exists():
            return f.read_bytes().decode("utf-8", "replace"), f"backup del {b['date']:%d/%m/%Y %H:%M}"
    raise LoxoneError("nessun log disponibile nei backup: usa --live (richiede login)")


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
    text, src = read_log_text(args, s)
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
    return backups.newest_program(s.backups_dir, DOWNLOADS)


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


def download_current_program(s) -> Path:
    """Scarica dal Miniserver l'ultima versione del programma in data/programmi/."""
    with open_ms(s) as ms:
        names = [f["filename"] for f in ms.list_files("prog")
                 if backups.PROGRAM_RE.match(f.get("filename", "")) and f["filename"].lower().endswith(".zip")]
        if not names:
            raise LoxoneError("nessun programma trovato sul Miniserver")
        def key(n):
            m = backups.PROGRAM_RE.match(n)
            return m.group(2), int(m.group(1))
        name = max(names, key=key)
        data = ms.http_download(f"dev/fsget/prog/{name}")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise LoxoneError(f"il file scaricato ({name}) non e' un archivio valido")
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    dest = DOWNLOADS / name
    dest.write_bytes(data)
    return dest


def print_diff(d: dict) -> None:
    print(f"Aggiunti: {len(d['aggiunti'])}  Rimossi: {len(d['rimossi'])}  Modificati: {len(d['modificati'])}")
    for x in d["aggiunti"][:60]:
        print(f"  + {x}")
    for x in d["rimossi"][:60]:
        print(f"  - {x}")
    for ch in d["modificati"][:80]:
        print(f"  ~ {ch['oggetto']}")
        for note in ch["modifiche"][:12]:
            print(f"      {note}")


def cmd_program(args, s):
    if args.azione == "scarica":
        dest = download_current_program(s)
        print(f"Programma attuale del Miniserver salvato in {dest} ({dest.stat().st_size} byte).")
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
        hits = prog.find(args.testo)
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


def pending_app_changes(s) -> list[str]:
    """Modifiche fatte dall'app (timer, scene, Autopilot...) non ancora integrate nel programma.

    Il Miniserver le tiene in /prog/custom e le CANCELLA quando carica un nuovo programma
    ("Remove old custom changes"): Loxone Config le integra prima di salvare, il caricamento diretto no.
    """
    with open_ms(s) as ms:
        return [f.get("filename", "?") for f in ms.list_files("prog/custom")]


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
    if not (d["aggiunti"] or d["rimossi"] or d["modificati"]):
        print("Nessuna differenza rispetto al programma attuale: niente da caricare.")
        return
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
    app_changes = pending_app_changes(s)
    if app_changes:
        print(f"ATTENZIONE: sul Miniserver ci sono {len(app_changes)} modifiche fatte dall'app (timer, scene, "
              "Autopilot...) non ancora integrate nel programma: il caricamento diretto le cancellerebbe. "
              "Usa Loxone Config, che le integra prima di salvare.")

    # 2) controlli di sicurezza prima di toccare il Miniserver
    alarm_armed, alarm_unknown = None, not has_credentials(s)
    if not alarm_unknown:
        try:
            st, ms = live_states(s)
            with ms:
                for c in st.find(type_="Alarm"):
                    if st.states_of(c, ms.values, ms.texts).get("armed"):
                        alarm_armed = c["name"]
        except (LoxoneError, OSError, websocket.WebSocketException):
            alarm_unknown = True
    alarm_problem = (f"antifurto '{alarm_armed}' INSERITO" if alarm_armed else
                     "stato dell'antifurto non leggibile" if alarm_unknown else None)
    built = uploader.build_program_zip(backup.read_bytes(), new_xml, replace)
    print(f"Pacchetto sps_new.zip pronto: {len(built)} byte.")

    if not args.applica:
        if impact or app_changes:
            print("\n[PROVA] Nulla inviato al Miniserver. Questa modifica va caricata con Loxone Config (vedi sopra).")
        else:
            print("\n[PROVA] Nulla inviato al Miniserver. Tutto pronto per il caricamento diretto.")
            print("Per attivarlo davvero: aggiungi --applica --conferma (riavvia la logica per ~10 s).")
        if alarm_problem:
            print(f"ATTENZIONE: {alarm_problem}: non caricare ora.")
        return

    if alarm_problem and not args.forza:
        raise LoxoneError(f"{alarm_problem}: caricamento annullato (usa --forza per ignorare)")
    if impact and not args.ignora_app:
        raise LoxoneError("caricamento diretto annullato: la modifica riguarda controlli visibili nell'app (vedi "
                          "sopra). Usa Loxone Config, oppure --ignora-app se sei certo che l'app non cambi.")
    if app_changes:
        raise LoxoneError("caricamento diretto annullato: cancellerebbe le modifiche fatte dall'app (vedi sopra). "
                          "Usa Loxone Config ('lox programma apri <file>' e 'Salva nel Miniserver').")
    if not args.conferma:
        raise LoxoneError("il caricamento riavvia la logica di casa: aggiungi --conferma (serve il consenso dell'utente)")

    ftp_user, ftp_pw = s.upload_creds()
    admin = dataclasses.replace(s, user=ftp_user, password=ftp_pw)
    # verifica dei diritti PRIMA di caricare: mai lasciare un sps_new.zip che non si puo' attivare
    try:
        with Miniserver(admin).connect_admin() as ms:
            ms.kill_token(forget_saved=False)  # revoca subito il token temporaneo di verifica
    except AuthError as e:
        raise LoxoneError(f"caricamento annullato prima di iniziare: {e}")
    print(f"\nDiritti admin verificati per '{ftp_user}'. Carico sps_new.zip via FTP...")
    res = uploader.ftp_put_program(s.host, ftp_user, ftp_pw, built)
    if not res.verified:
        raise LoxoneError("upload FTP non verificato: interrotto prima del riavvio")
    print(f"  caricato e verificato ({res.uploaded} byte). Riavvio la logica...")

    restart_at = datetime.now() - timedelta(seconds=120)  # margine per orologi di PC e Miniserver non allineati
    with Miniserver(admin).connect_admin() as ms:
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
        print("CARICAMENTO DIRETTO RIUSCITO.")
        return

    # rollback: riporta la versione precedente (anche se il nuovo non si e' attivato)
    print("PROBLEMA nell'attivazione: ripristino la versione precedente...")
    rb = uploader.build_program_zip(backup.read_bytes(), cur_xml)
    uploader.ftp_put_program(s.host, ftp_user, ftp_pw, rb)
    try:
        with Miniserver(admin).connect_admin() as ms:
            ms.restart_program()
        back_ok = wait_for_program(s, Program(cur_xml))
        print("Versione precedente ripristinata e verificata." if back_ok
              else "ATTENZIONE: ripristino inviato ma non confermato: controlla l'impianto.")
    except LoxoneError:
        print("ATTENZIONE: ripristino inviato ma non confermato: controlla l'impianto.")
    raise LoxoneError("attivazione fallita: ripristinata la versione precedente. Controlla il file e riprova.")


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


def wait_for_program(s, expected: Program, seconds: float = 120) -> bool:
    """Attende che il programma attivo sul Miniserver coincida con `expected` (riprova durante il riavvio)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(5)
        try:
            active = Program(read_program(download_current_program(s))[0])
        except (LoxoneError, OSError, websocket.WebSocketException, ValueError):
            continue  # Miniserver ancora in riavvio
        d = programmod.diff(expected, active)
        if not (d["aggiunti"] or d["rimossi"] or d["modificati"]):
            return True
    return False


def cmd_raw(args, s):
    if not any(p.match(args.comando) for p in READONLY_RAW) and not args.conferma:
        raise LoxoneError("comando non in elenco di sola lettura: ripeti con --conferma solo se sei sicuro "
                          "e l'utente ha dato il consenso (alcuni comandi riavviano o modificano il Miniserver)")
    with open_ms(s) as ms:
        print(json.dumps(ms.request(args.comando), ensure_ascii=False, indent=2))


def host_reachable(host: str, port: int, proto: str) -> bool:
    """TCP: prova ad aprire la porta. UDP: non ha risposta, quindi usa un ping."""
    if proto == "tcp":
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            return False
    flag = ["-n", "2", "-w", "1500"] if os.name == "nt" else ["-c", "2", "-W", "2"]
    return subprocess.run(["ping", *flag, host], capture_output=True).returncode == 0


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


def check_devices(s) -> int:
    """Stampa lo stato dei dispositivi esterni del programma; restituisce quanti sono irraggiungibili."""
    p = newest_program(s)
    if not p:
        return 0
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
    print("Tutti i dispositivi rispondono." if not missing else f"{missing} indirizzi non rispondono.")


def expected_shelly_ips(s) -> dict[str, list[str]]:
    """IP HTTP previsti dal programma -> nomi dei comandi che li usano."""
    p = newest_program(s)
    if not p:
        return {}
    out: dict[str, list[str]] = {}
    for d in Program(read_program(p["path"])[0]).device_hosts():
        if d["proto"] == "tcp" and d["porta"] == 80:
            out.setdefault(d["host"], []).append(d["titolo"])
    return out


def cmd_shelly(args, s):
    expected = expected_shelly_ips(s)
    gateway = os.environ.get("LOXONE_LAN_GATEWAY") or s.host.rsplit(".", 1)[0] + ".1"
    if args.azione == "trova":
        prefix = s.host.rsplit(".", 1)[0] + "."
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
        prefix = s.host.rsplit(".", 1)[0] + "."
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
    try:
        text, src = read_log_text(argparse.Namespace(live=args.log_live), s)
    except (LoxoneError, OSError, websocket.WebSocketException) as e:
        warn(f"log live non disponibile ({e}): uso il backup")
        text, src = read_log_text(argparse.Namespace(live=False), s)
    print()
    print_log_summary(text, src, args.giorni)
    print()
    check_devices(s)
    print()
    if not live:
        print("Stato live dei dispositivi: non disponibile (manca il login). Vedi README, sezione 'Credenziali'.")
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

    a = sub.add_parser("shelly", help="trova gli Shelly in rete, assegna l'IP fisso, li fa lampeggiare")
    a.add_argument("azione", choices=["trova", "attendi", "ip", "lampeggia"])
    a.add_argument("ip", nargs="?", help="indirizzo attuale dello Shelly")
    a.add_argument("nuovo", nargs="?", help="nuovo indirizzo fisso (per 'ip')")
    a.add_argument("--forza", action="store_true", help="consente un IP non previsto dal programma")
    a.add_argument("--minuti", type=float, default=30, help="durata massima dell'attesa (per 'attendi')")
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
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    args = build_parser().parse_args(argv)
    try:
        args.fn(args, get_settings())
    except (LoxoneError, LookupError, editor.EditError, LoxCCError) as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        sys.exit(1)
    except (OSError, websocket.WebSocketException) as e:
        print(f"ERRORE di connessione verso il Miniserver: {e}", file=sys.stderr)
        sys.exit(2)
