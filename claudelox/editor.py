"""Modifiche strutturali al progetto Loxone (nuovi blocchi, collegamenti, eliminazioni), fatte sul testo XML.

Ogni funzione restituisce il nuovo testo e lascia identico, byte per byte, tutto il resto del file.
Il risultato va sempre verificato (Program(...) e program.diff) prima di caricarlo sul Miniserver
(lox carica, oppure Loxone Config con "Salva nel Miniserver").

Formato (osservato nei progetti Loxone): blocchi <C Type=.. U=uuid Title=.. Px Py Px2 Py2 ...>,
connettori <Co K=nome [Inv="true"] [Def=valore] [Nc=numero_collegamenti] U=uuid>, collegamenti
<In Input=uuid_connettore_sorgente />. UUID: 8-4-4-16 esadecimali, il primo gruppo e' l'ora di
creazione in secondi dal 1.1.2009 (come l'attributo LtE).
"""
from __future__ import annotations

import re
import secrets
import time

LOX_EPOCH = 1230768000
UUID_ATTR_RE = re.compile(r'\bU="([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{16})"')
TAG_RE = re.compile(r"<(/?)C\b[^>]*?(/?)>")
LAYOUT = ("Px", "Py", "Px2", "Py2")


class EditError(ValueError):
    pass


def lox_now() -> int:
    return int(time.time()) - LOX_EPOCH


def escape_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def newline_of(text: str) -> str:
    return "\r\n" if "\r\n" in text[:4000] else "\n"


class UuidFactory:
    """UUID nuovi nel formato Loxone, garantiti unici nel progetto."""

    def __init__(self, text: str):
        self.used = set(UUID_ATTR_RE.findall(text))
        self.stamp = lox_now()

    def _fresh(self, make) -> str:
        while True:
            u = make()
            if u not in self.used:
                self.used.add(u)
                return u

    def block(self) -> str:
        return self._fresh(lambda: f"{self.stamp:08x}-{secrets.randbits(16):04x}-"
                                   f"{secrets.randbits(16):04x}-ffff{secrets.token_hex(6)}")

    def connector(self, block_uuid: str, index: int) -> str:
        return self._fresh(lambda: f"{block_uuid[:13]}-{secrets.randbits(16):04x}-{index:02x}ff{secrets.token_hex(6)}")


def element_span(text: str, uuid: str) -> tuple[int, int]:
    """Inizio e fine (nel testo) dell'elemento <C ... U="uuid"> ... </C>."""
    m = re.search(r'<C\b[^>]*\sU="%s"[^>]*>' % re.escape(uuid), text)
    if not m:
        raise EditError(f"oggetto {uuid} non trovato")
    if m.group(0).endswith("/>"):
        return m.start(), m.end()
    depth = 1
    for t in TAG_RE.finditer(text, m.end()):
        if t.group(1):
            depth -= 1
            if depth == 0:
                return m.start(), t.end()
        elif not t.group(2):
            depth += 1
    raise EditError(f"oggetto {uuid} non chiuso correttamente")


def clone_block(template, ids: UuidFactory, *, title: str | None = None, position: tuple[int, int] | None = None,
                overrides: dict[str, dict] | None = None) -> tuple[str, str, dict[str, str]]:
    """Testo XML di un nuovo blocco copiato da un blocco esistente (ElementTree).

    overrides: {nome_connettore: {"inputs": [uuid,...], "Inv": True|False, "Def": "valore"|None}}.
    Restituisce (testo, uuid_blocco, {nome_connettore: uuid_connettore}).
    """
    overrides = overrides or {}
    block_u = ids.block()
    attrs = dict(template.attrib)
    attrs["U"] = block_u
    if title is not None:
        attrs["Title"] = title
    if position is not None and all(k in attrs for k in LAYOUT):
        w = int(attrs["Px2"]) - int(attrs["Px"])
        h = int(attrs["Py2"]) - int(attrs["Py"])
        x, y = position
        attrs.update(Px=str(x), Py=str(y), Px2=str(x + w), Py2=str(y + h))
    if "LtE" in attrs:
        attrs["LtE"] = str(ids.stamp)
    lines = ["<C " + " ".join(f'{k}="{escape_attr(v)}"' for k, v in attrs.items()) + ">"]
    connectors: dict[str, str] = {}
    for i, co in enumerate(template.findall("Co")):
        k = co.get("K")
        ov = overrides.get(k, {})
        cu = ids.connector(block_u, i)
        connectors[k] = cu
        inv = ov.get("Inv", co.get("Inv") == "true")
        default = ov["Def"] if "Def" in ov else co.get("Def")
        if "inputs" in ov:
            inputs = [f'<In Input="{u}" />' for u in ov["inputs"]]
        else:
            inputs = ["<In " + " ".join(f'{a}="{escape_attr(v)}"' for a, v in i_el.attrib.items()) + " />"
                      for i_el in co.findall("In")]
        parts = [f'K="{escape_attr(k)}"']
        if inv:
            parts.append('Inv="true"')
        if default is not None:
            parts.append(f'Def="{escape_attr(default)}"')
        if inputs:
            parts.append(f'Nc="{len(inputs)}"')
        parts.append(f'U="{cu}"')
        if inputs:
            lines.append("\t<Co " + " ".join(parts) + ">")
            lines += [f"\t\t{x}" for x in inputs]
            lines.append("\t</Co>")
        else:
            lines.append("\t<Co " + " ".join(parts) + " />")
    lines.append("</C>")
    return "\n".join(lines), block_u, connectors


def insert_children(text: str, parent_uuid: str, blocks: list[str]) -> str:
    """Aggiunge blocchi in fondo a un contenitore (es. una pagina), con il rientro del file."""
    nl = newline_of(text)
    _, end = element_span(text, parent_uuid)
    close = end - len("</C>")
    line_start = text.rfind("\n", 0, close) + 1
    indent = text[line_start:close]
    if indent.strip():
        raise EditError("formato inatteso del contenitore")
    lines = [indent + "\t" + line for b in blocks for line in b.split("\n")]
    return text[:line_start] + nl.join(lines) + nl + text[line_start:]


def _with_nc(tag: str, n: int) -> str:
    if re.search(r'\sNc="\d+"', tag):
        return re.sub(r'(\sNc=")\d+(")', rf"\g<1>{n}\g<2>", tag, count=1)
    return tag.replace(' U="', f' Nc="{n}" U="', 1)


def add_input(text: str, connector_uuid: str, source_uuid: str) -> str:
    """Collega il connettore sorgente all'ingresso indicato (si aggiunge ai collegamenti esistenti)."""
    nl = newline_of(text)
    m = re.search(r'<Co\b[^>]*\sU="%s"[^>]*>' % re.escape(connector_uuid), text)
    if not m:
        raise EditError(f"connettore {connector_uuid} non trovato")
    tag = m.group(0)
    line_start = text.rfind("\n", 0, m.start()) + 1
    indent = text[line_start:m.start()]
    new_in = f'{indent}\t<In Input="{source_uuid}" />'
    if tag.endswith("/>"):
        open_tag = _with_nc(re.sub(r"\s*/>$", ">", tag), 1)
        return text[:m.start()] + open_tag + nl + new_in + nl + indent + "</Co>" + text[m.end():]
    close = text.find("</Co>", m.end())
    count = text.count("<In ", m.end(), close)
    return text[:m.start()] + _with_nc(tag, count + 1) + nl + new_in + text[m.end():]


def bump_counters(text: str, added: int) -> str:
    """Aggiorna i contatori di oggetti del progetto (NextObj, NumO) dopo un'aggiunta."""
    def inc(m):
        return f"{m.group(1)}{int(m.group(2)) + added}{m.group(3)}"
    text = re.sub(r'(<ControlList\b[^>]*\sNextObj=")(\d+)(")', inc, text, count=1)
    return re.sub(r'(<C Type="Document"[^>]*\sNumO=")(\d+)(")', inc, text, count=1)


CO_TAG_RE = re.compile(r"<Co\b[^>]*?(/?)>")
IN_TAG_RE = re.compile(r'<In\b[^>]*\sInput="([0-9a-f-]+)"[^>]*?/>')


def _whole_lines(text: str, start: int, end: int) -> tuple[int, int]:
    """Allarga [start, end) a righe intere (rientro e a capo) se l'elemento le occupa da solo."""
    line_start = text.rfind("\n", 0, start) + 1
    if not text[line_start:start].strip():
        start = line_start
    nl = text.find("\n", end)
    if nl != -1 and not text[end:nl].strip():
        end = nl + 1
    return start, end


def _drop_links(text: str, gone: set[str], only: str | None = None) -> str:
    """Toglie i collegamenti <In Input=...> che partono dai connettori `gone`, aggiornando Nc.

    Con `only` agisce solo sul connettore di destinazione con quell'UUID.
    """
    self_close = " />" if text.count('" />') > text.count('"/>') else "/>"
    edits = []
    for m in CO_TAG_RE.finditer(text):
        if m.group(1) or (only and f'U="{only}"' not in m.group(0)):
            continue
        close = text.find("</Co>", m.end())
        body = text[m.end():close]
        links = list(IN_TAG_RE.finditer(body))
        drop = [i for i in links if i.group(1) in gone]
        if not drop:
            continue
        left = len(links) - len(drop)
        if left == 0:  # ingresso senza piu' collegamenti: torna nella forma vuota <Co ... />
            tag = re.sub(r'\sNc="\d+"', "", m.group(0))
            edits.append((m.start(), close + len("</Co>"), tag[:-1].rstrip() + self_close))
            continue
        for i in reversed(drop):
            a, b = _whole_lines(body, i.start(), i.end())
            body = body[:a] + body[b:]
        edits.append((m.start(), close, _with_nc(m.group(0), left) + body))
    for a, b, new in reversed(edits):
        text = text[:a] + new + text[b:]
    return text


def remove_input(text: str, connector_uuid: str, source_uuid: str) -> str:
    """Toglie un solo collegamento: quello dal connettore sorgente all'ingresso indicato."""
    new = _drop_links(text, {source_uuid}, only=connector_uuid)
    if new == text:
        raise EditError(f"collegamento {source_uuid} -> {connector_uuid} non trovato")
    return new


def set_inverted(text: str, connector_uuid: str, inverted: bool) -> str:
    """Rende invertito (o non piu' invertito) un ingresso: attributo Inv="true" del connettore."""
    m = re.search(r'<Co\b[^>]*\sU="%s"[^>]*>' % re.escape(connector_uuid), text)
    if not m:
        raise EditError(f"connettore {connector_uuid} non trovato")
    tag = re.sub(r'\sInv="[^"]*"', "", m.group(0))
    if inverted:
        tag = re.sub(r'(<Co\s+K="[^"]*")', r'\1 Inv="true"', tag, count=1)
    return text[:m.start()] + tag + text[m.end():]


def remove_blocks(text: str, uuids: list[str]) -> tuple[str, int]:
    """Elimina i blocchi indicati e i collegamenti che partono dai loro connettori.

    Gli ingressi che restano senza collegamenti tornano nella forma vuota <Co ... />.
    Si ferma (EditError) se un oggetto che resta nel progetto fa ancora riferimento a un
    blocco eliminato in altro modo (Ref=, stanza/categoria, ...): quel caso va deciso a mano.
    Restituisce (nuovo_testo, numero_di_oggetti_eliminati).
    """
    spans = sorted({element_span(text, u) for u in uuids})
    for (_, end1), (start2, _) in zip(spans, spans[1:]):
        if start2 < end1:
            raise EditError("un blocco da eliminare e' contenuto in un altro: indica solo il contenitore")
    gone: set[str] = set()
    removed = 0
    for a, b in spans:
        chunk = text[a:b]
        if len(re.findall(r"<C\b", chunk)) > 1:
            raise EditError("si possono eliminare solo blocchi singoli, non contenitori (pagine, dispositivi)")
        gone.update(UUID_ATTR_RE.findall(chunk))
        removed += 1
    for a, b in reversed(spans):
        a, b = _whole_lines(text, a, b)
        text = text[:a] + text[b:]
    text = _drop_links(text, gone)
    dangling = sorted({m.group(0) for u in gone for m in re.finditer(r'\w+="[^"]*%s[^"]*"' % re.escape(u), text)})
    if dangling:
        raise EditError("altri oggetti fanno ancora riferimento ai blocchi da eliminare: " + ", ".join(dangling))

    def dec(m):
        return f"{m.group(1)}{max(int(m.group(2)) - removed, 0)}{m.group(3)}"
    return re.sub(r'(<C Type="Document"[^>]*\sNumO=")(\d+)(")', dec, text, count=1), removed


def free_area_below(page, margin: int = 480, grid: int = 96) -> tuple[int, int]:
    """Prima posizione libera sotto i blocchi gia' presenti in una pagina: (x, y)."""
    boxes = [tuple(int(e.get(k)) for k in LAYOUT) for e in page
             if e.tag == "C" and all(e.get(k) for k in LAYOUT)]
    if not boxes:
        return 960, 960
    x = min(b[0] for b in boxes)
    y = max(b[3] for b in boxes) + margin
    return x, -(-y // grid) * grid
