"""Lettura dei file di programma Loxone.

Il Miniserver salva il programma (lo stesso progetto che apri in Loxone Config)
come file .LoxCC: un header di 16 byte seguito da dati compressi in formato
LZ4-block. Gli archivi sps_XXXX_YYYYMMDDhhmmss.zip in /prog contengono
sps0.LoxCC (programma) e LoxAPP3.json (file di struttura per le app).
"""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path

MAGIC = 0xAABBCCEE


class LoxCCError(ValueError):
    pass


def decompress(data: bytes) -> bytes:
    """Decomprime un .LoxCC e restituisce l'XML del progetto (bytes)."""
    if len(data) < 16:
        raise LoxCCError("file troppo corto per essere un .LoxCC")
    magic, comp_size, uncomp_size, _checksum = struct.unpack_from("<4I", data, 0)
    if magic != MAGIC:
        raise LoxCCError(f"header non valido ({magic:#010x}), non e' un file .LoxCC")
    src = memoryview(data)[16:16 + comp_size]
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:  # l'ultima sequenza contiene solo letterali
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        mlen = (token & 15) + 4
        if (token & 15) == 15:
            while True:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        start = len(out) - offset
        if offset == 0 or start < 0:
            raise LoxCCError("dati corrotti (offset non valido)")
        if offset >= mlen:
            out += out[start:start + mlen]
        else:  # copia sovrapposta: byte per byte
            for k in range(mlen):
                out.append(out[start + k])
    if len(out) != uncomp_size:
        raise LoxCCError(f"dimensione decompressa inattesa: {len(out)} invece di {uncomp_size}")
    return bytes(out)


def read_program(path: str | Path) -> tuple[bytes, bytes | None]:
    """Legge un programma da .zip / .LoxCC / .Loxone.

    Restituisce (xml_progetto, loxapp3_json_o_None).
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            prog_name = next((n for n in names if n.lower() == "sps0.loxcc"), None) or next(
                (n for n in names if n.lower().endswith(".loxcc") and "emergency" not in n.lower()), None)
            if prog_name is None:
                raise LoxCCError(f"nessun .LoxCC dentro {path.name}: {names}")
            xml = decompress(z.read(prog_name))
            app = z.read("LoxAPP3.json") if "LoxAPP3.json" in names else None
            return xml, app
    data = path.read_bytes()
    if suffix == ".loxcc":
        return decompress(data), None
    return data, None  # .Loxone = XML in chiaro
