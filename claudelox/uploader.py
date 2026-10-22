"""Caricamento diretto di un programma sul Miniserver tramite la sua interfaccia FTP.

Riproduce ESATTAMENTE cio' che fa Loxone Config (ricavato dal log del Miniserver):
 1. impacchetta il programma come /prog/sps_new.zip (nome fisso usato da Config);
 2. invia il comando di riavvio della logica (dev/sps/restart) con un utente admin;
 3. al riavvio il Miniserver carica sps_new.zip, lo COMPILA e lo valida (riporta Errors),
    lo attiva e lo rinomina nel nome definitivo con la data.

Sicurezza: prima di attivare si fa sempre un backup della versione corrente (rollback).
Il Miniserver stesso valida il programma: se la compilazione da' errori, va interrotto
e ripristinata la versione precedente. L'attivazione riavvia brevemente la logica di casa
(~10 s), quindi richiede il consenso esplicito dell'utente e non va fatta ad allarme inserito.

Formato .LoxCC: header 16 byte `<4I` = magic 0xAABBCCEE, dimensione compressa, dimensione
non compressa, CRC32 del contenuto non compresso; poi un blocco LZ4. Qui si usa LZ4 in
modalita' "store" (solo letterali): output valido e piu' semplice, verificato col round-trip.
"""
from __future__ import annotations

import io
import ssl
import struct
import zipfile
import zlib
from dataclasses import dataclass

from .loxcc import MAGIC, decompress

PROGRAM_MEMBER = "sps0.LoxCC"
APP_MEMBER = "LoxAPP3.json"
PERMISSIONS_MEMBER = "permissions.bin"
UPLOAD_NAME = "sps_new.zip"
FTP_DIR = "/prog"


def compress_loxcc(xml: bytes) -> bytes:
    """Costruisce un .LoxCC valido (LZ4 store-mode) con CRC32 corretto nell'header."""
    body = bytearray()
    length = len(xml)
    if length < 15:
        body.append(length << 4)
    else:
        body.append(0xF0)  # 15 letterali nel token, resto nelle estensioni
        rem = length - 15
        while rem >= 255:
            body.append(255)
            rem -= 255
        body.append(rem)
    body += xml
    header = struct.pack("<4I", MAGIC, len(body), length, zlib.crc32(xml) & 0xFFFFFFFF)
    out = header + bytes(body)
    if decompress(out) != xml:  # garanzia: deve ridecomprimersi identico
        raise ValueError("compressione .LoxCC non verificata (round-trip fallito)")
    return out


def build_program_zip(source_zip: bytes, new_xml: bytes, replace: dict[str, bytes] | None = None) -> bytes:
    """Ricostruisce lo zip del programma sostituendo sps0.LoxCC con new_xml compresso.

    Il Miniserver NON rigenera la struttura dell'app ne' i permessi: usa LoxAPP3.json e permissions.bin
    dello zip. `replace` sostituisce questi membri (vedi appstructure); gli altri (Emergency.LoxCC,
    Music.json...) restano com'erano.
    """
    replace = replace or {}
    with zipfile.ZipFile(io.BytesIO(source_zip)) as zin:
        names = zin.namelist()
        missing = [n for n in [PROGRAM_MEMBER, *replace] if n not in names]
        if missing:
            raise ValueError(f"{', '.join(missing)} assente nello zip di partenza: {names}")
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                data = compress_loxcc(new_xml) if name == PROGRAM_MEMBER else replace.get(name) or zin.read(name)
                zout.writestr(name, data)
    return out.getvalue()


def read_member(zip_bytes: bytes, name: str) -> bytes | None:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        return z.read(name) if name in z.namelist() else None


def _tls_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False      # certificato CloudDNS non valido per l'IP locale
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


@dataclass
class FtpResult:
    uploaded: int
    verified: bool


def ftp_put_program(host: str, user: str, password: str, zip_bytes: bytes,
                    name: str = UPLOAD_NAME, verify: bool = True) -> FtpResult:
    """Carica lo zip in /prog via FTP+TLS e (opzionale) verifica la dimensione sul server."""
    import ftplib
    ftp = ftplib.FTP_TLS(context=_tls_ctx())
    ftp.connect(host, 21, timeout=20)
    ftp.login(user, password)
    ftp.prot_p()
    try:
        ftp.cwd(FTP_DIR)
        ftp.storbinary(f"STOR {name}", io.BytesIO(zip_bytes))
        verified = False
        if verify:
            try:
                verified = ftp.size(name) == len(zip_bytes)
            except ftplib.all_errors:
                verified = False
        return FtpResult(uploaded=len(zip_bytes), verified=verified)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def ftp_download(host: str, user: str, password: str, path: str) -> bytes:
    import ftplib
    ftp = ftplib.FTP_TLS(context=_tls_ctx())
    ftp.connect(host, 21, timeout=20)
    ftp.login(user, password)
    ftp.prot_p()
    buf = io.BytesIO()
    try:
        ftp.retrbinary(f"RETR {path}", buf.write)
        return buf.getvalue()
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def ftp_delete(host: str, user: str, password: str, path: str) -> None:
    import ftplib
    ftp = ftplib.FTP_TLS(context=_tls_ctx())
    ftp.connect(host, 21, timeout=20)
    ftp.login(user, password)
    try:
        ftp.delete(path)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass
