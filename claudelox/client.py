"""Client per il Miniserver Loxone tramite l'API ufficiale WebSocket, solo in rete locale.

Flusso ("Communicating with the Loxone Miniserver", v16.0):
 1. chiave pubblica RSA del Miniserver (jdev/sys/getPublicKey, nessun login)
 2. WebSocket ws://<host>/ws/rfc6455 con protocollo "remotecontrol"
 3. chiave di sessione AES cifrata con RSA -> jdev/sys/keyexchange/...
 4. login: token salvato (authwithtoken) oppure password (getkey2 + getjwt)
 5. comandi jdev/sps/io/<uuid>/<comando>; stati con jdev/sps/enablebinstatusupdate
"""
from __future__ import annotations

import http.client
import json
import struct
import time
import urllib.parse
import urllib.request
import uuid as uuidlib
from dataclasses import asdict, dataclass, field

import websocket

from . import crypto
from .config import STATE_DIR, Settings

LOX_EPOCH = 1230768000  # 1.1.2009 00:00 UTC, base dei timestamp Loxone
# Diritti per le operazioni da amministratore (caricamento programma): token "Web" (2, breve durata)
# + FTP (0x10) + Webservice di sistema (0x100). Il token non viene salvato su disco.
ADMIN_OPS_PERMISSION = 0x2 | 0x10 | 0x100
RIGHT_SYS_WS = 0x100
CLIENT_INFO = "ClaudeLox (Claude Code)"
REFRESH_BELOW = 3 * 24 * 3600  # rinnova il token se scade entro 3 giorni

# identificatori dell'header binario di 8 byte che precede ogni messaggio
HDR_TEXT, HDR_FILE, HDR_VALUES, HDR_TEXTS, HDR_DAYTIMER, HDR_OUT_OF_SERVICE, HDR_KEEPALIVE, HDR_WEATHER = range(8)

CODE_MEANING = {
    400: "richiesta non valida",
    401: "credenziali non valide (utente o password errati) oppure token scaduto",
    403: "l'utente non ha i permessi necessari",
    404: "comando, controllo o utente non trovato",
    420: "autenticazione non completata in tempo",
    423: "utente disabilitato",
    429: "troppi tentativi: il Miniserver ha bloccato temporaneamente gli accessi",
    500: "errore interno del Miniserver",
    503: "il Miniserver si sta riavviando",
    901: "troppe connessioni contemporanee",
}
CLOSE_MEANING = {
    4003: "troppi tentativi di accesso falliti: accesso bloccato temporaneamente",
    4004: "un utente e' stato modificato",
    4005: "l'utente connesso e' stato modificato",
    4006: "l'utente e' disabilitato",
    4007: "il Miniserver sta eseguendo un aggiornamento",
    4008: "nessuno slot libero per gli aggiornamenti di stato",
}

TOKEN_FILE = STATE_DIR / "token.json"
CLIENT_FILE = STATE_DIR / "client.json"
STRUCTURE_CACHE = STATE_DIR / "LoxAPP3.json"


class LoxoneError(Exception):
    pass


class AuthError(LoxoneError):
    pass


def ll_code(ll: dict) -> int:
    for k in ("Code", "code"):
        if k in ll:
            try:
                return int(ll[k])
            except (TypeError, ValueError):
                return -1
    return -1


def explain(code: int) -> str:
    return f"codice {code}: {CODE_MEANING.get(code, 'errore sconosciuto')}"


def loose_json(text: str):
    """Alcune risposte (es. jdev/cfg/apiKey) usano apici singoli invece dei doppi."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(text.replace("'", '"'))


# ------------------------------------------------------------- eventi di stato

def fmt_uuid(b: bytes, off: int = 0) -> str:
    d1, d2, d3 = struct.unpack_from("<IHH", b, off)
    return f"{d1:08x}-{d2:04x}-{d3:04x}-{bytes(b[off + 8:off + 16]).hex()}"


def parse_value_events(p: bytes) -> dict[str, float]:
    return {fmt_uuid(p, o): struct.unpack_from("<d", p, o + 16)[0] for o in range(0, len(p) - 23, 24)}


def parse_text_events(p: bytes) -> dict[str, str]:
    out, o = {}, 0
    while o + 36 <= len(p):
        uid = fmt_uuid(p, o)
        (n,) = struct.unpack_from("<I", p, o + 32)
        out[uid] = bytes(p[o + 36:o + 36 + n]).decode("utf-8", "replace")
        o += 36 + n + ((-n) % 4)
    return out


def parse_daytimer_events(p: bytes) -> dict[str, dict]:
    out, o = {}, 0
    while o + 28 <= len(p):
        uid = fmt_uuid(p, o)
        default, n = struct.unpack_from("<di", p, o + 16)
        o += 28
        entries = []
        for _ in range(max(n, 0)):
            mode, frm, to, need, val = struct.unpack_from("<iiiid", p, o)
            entries.append({"mode": mode, "from": frm, "to": to, "needActivate": need, "value": val})
            o += 24
        out[uid] = {"default": default, "entries": entries}
    return out


WEATHER_FIELDS = ("timestamp", "weatherType", "windDirection", "solarRadiation", "relativeHumidity",
                  "temperature", "perceivedTemperature", "dewPoint", "precipitation", "windSpeed",
                  "barometricPressure")


def parse_weather_events(p: bytes) -> dict[str, dict]:
    out, o = {}, 0
    while o + 24 <= len(p):
        uid = fmt_uuid(p, o)
        last, n = struct.unpack_from("<Ii", p, o + 16)
        o += 24
        entries = []
        for _ in range(max(n, 0)):
            entries.append(dict(zip(WEATHER_FIELDS, struct.unpack_from("<5i6d", p, o))))
            o += 68
        out[uid] = {"lastUpdate": last, "entries": entries}
    return out


EVENT_PARSERS = {
    HDR_VALUES: (parse_value_events, "values"),
    HDR_TEXTS: (parse_text_events, "texts"),
    HDR_DAYTIMER: (parse_daytimer_events, "daytimers"),
    HDR_WEATHER: (parse_weather_events, "weather"),
}


# ------------------------------------------------------------------- token

@dataclass
class Token:
    host: str
    user: str
    token: str
    valid_until: int          # secondi dal 1.1.2009
    rights: int | None
    hash_alg: str
    unsecure_pass: bool | None = None

    def seconds_left(self) -> float:
        return LOX_EPOCH + self.valid_until - time.time()


def load_token(host: str, user: str) -> Token | None:
    try:
        tok = Token(**json.loads(TOKEN_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None
    return tok if (tok.host == host and tok.user == user) else None


def save_token(tok: Token) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(asdict(tok), indent=2), encoding="utf-8")


def delete_token() -> None:
    TOKEN_FILE.unlink(missing_ok=True)


def client_uuid() -> str:
    """Identificativo fisso di questo client, nel formato richiesto da getjwt."""
    try:
        return json.loads(CLIENT_FILE.read_text(encoding="utf-8"))["uuid"]
    except (OSError, ValueError, KeyError):
        h = uuidlib.uuid4().hex
        uid = f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:32]}"
        STATE_DIR.mkdir(exist_ok=True)
        CLIENT_FILE.write_text(json.dumps({"uuid": uid}), encoding="utf-8")
        return uid


# ------------------------------------------------------------------ client

@dataclass
class Message:
    kind: int
    payload: bytes | str | None
    events: dict = field(default_factory=dict)


class Miniserver:
    def __init__(self, settings: Settings, timeout: float = 15):
        self.s = settings
        self.timeout = timeout
        self.ws: websocket.WebSocket | None = None
        self.session: crypto.Session | None = None
        self.token: Token | None = None
        self.values: dict[str, float] = {}
        self.texts: dict[str, str] = {}
        self.daytimers: dict[str, dict] = {}
        self.weather: dict[str, dict] = {}

    # --- HTTP pubblico (non richiede login) ---
    def http_public(self, path: str) -> dict:
        with urllib.request.urlopen(f"http://{self.s.host}/{path}", timeout=8) as r:
            return json.loads(r.read().decode("utf-8", "replace"))["LL"]

    def api_info(self) -> dict:
        return loose_json(self.http_public("jdev/cfg/apiKey")["value"])

    # --- connessione e login ---
    def connect(self, login: bool = True) -> "Miniserver":
        public_key = crypto.load_public_key(self.http_public("jdev/sys/getPublicKey")["value"])
        self.ws = websocket.create_connection(f"ws://{self.s.host}/ws/rfc6455",
                                              subprotocols=["remotecontrol"], timeout=self.timeout)
        self.session = crypto.Session()
        ll = self.request(f"jdev/sys/keyexchange/{self.session.encrypted_session_key(public_key)}")
        if ll_code(ll) != 200:
            raise LoxoneError(f"scambio chiavi rifiutato ({explain(ll_code(ll))})")
        if login:
            self._login()
        return self

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def __enter__(self) -> "Miniserver":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _login(self) -> None:
        user = self.s.user
        if not user:
            raise AuthError("manca LOXONE_USER nel file .env")
        tok = load_token(self.s.host, user)
        if tok and tok.seconds_left() > 60:
            if self._auth_with_token(tok):
                self.token = tok
                if tok.seconds_left() < REFRESH_BELOW:
                    self._refresh(tok)
                return
            delete_token()
        if not self.s.password:
            raise AuthError("nessun token valido salvato: esegui 'lox login' (ti chiede la password) "
                            "oppure imposta LOXONE_PASSWORD nel file .env")
        self.login_with_password(user, self.s.password)

    def _getkey(self) -> str:
        ll = self.request("jdev/sys/getkey")
        if ll_code(ll) != 200:
            raise AuthError(f"getkey fallito ({explain(ll_code(ll))})")
        return str(ll["value"])

    def _auth_with_token(self, tok: Token) -> bool:
        h = crypto.token_hash(tok.token, self._getkey(), tok.hash_alg)
        ll = self.request(self.session.wrap(f"authwithtoken/{h}/{urllib.parse.quote(tok.user, safe='')}"))
        return ll_code(ll) == 200

    def login_with_password(self, user: str, password: str, permission: int | None = None,
                            persist: bool = True) -> Token:
        """Ottiene un token. Il Miniserver concede SOLO i diritti richiesti in `permission`
        (e posseduti dall'utente): un token "App" (4) non include mai i diritti admin.
        Con persist=False il token resta solo in memoria (usato per le operazioni admin)."""
        q_user = urllib.parse.quote(user, safe="")
        ll = self.request(f"jdev/sys/getkey2/{q_user}")
        if ll_code(ll) == 404:
            raise AuthError(f"utente '{user}' non trovato sul Miniserver: controlla il nome in Loxone Config "
                            "(maiuscole e minuscole comprese)")
        if ll_code(ll) != 200:
            raise AuthError(f"impossibile avviare il login per '{user}' ({explain(ll_code(ll))})")
        v = ll["value"] if isinstance(ll["value"], dict) else loose_json(ll["value"])
        alg = v.get("hashAlg", "SHA1")
        h = crypto.password_hash(user, password, v["key"], v["salt"], alg)
        info = urllib.parse.quote(CLIENT_INFO, safe="")
        perm = self.s.permission if permission is None else permission
        cmd = f"jdev/sys/getjwt/{h}/{q_user}/{perm}/{client_uuid()}/{info}"
        ll = self.request(self.session.wrap(cmd, full=True), encrypted_response=True)
        if ll_code(ll) == 401:
            raise AuthError(f"login rifiutato per l'utente '{user}' (codice 401): password errata oppure nome "
                            "utente diverso da quello in Loxone Config (conta anche maiuscole/minuscole)")
        if ll_code(ll) != 200:
            raise AuthError(f"login rifiutato ({explain(ll_code(ll))})")
        t = ll["value"] if isinstance(ll["value"], dict) else loose_json(ll["value"])
        self.token = Token(host=self.s.host, user=user, token=t["token"], valid_until=int(t["validUntil"]),
                           rights=t.get("tokenRights"), hash_alg=alg, unsecure_pass=t.get("unsecurePass"))
        if persist:
            save_token(self.token)
        return self.token

    def _refresh(self, tok: Token) -> None:
        h = crypto.token_hash(tok.token, self._getkey(), tok.hash_alg)
        cmd = f"jdev/sys/refreshjwt/{h}/{urllib.parse.quote(tok.user, safe='')}"
        ll = self.request(self.session.wrap(cmd, full=True), encrypted_response=True)
        if ll_code(ll) == 200:
            v = ll["value"] if isinstance(ll["value"], dict) else loose_json(ll["value"])
            tok.token = v.get("token", tok.token)
            tok.valid_until = int(v.get("validUntil", tok.valid_until))
            tok.unsecure_pass = v.get("unsecurePass", tok.unsecure_pass)
            save_token(tok)

    def kill_token(self, forget_saved: bool = True) -> bool:
        """Invalida il token sul Miniserver e (salvo forget_saved=False) lo cancella dal disco.

        Con forget_saved=False si revoca un token temporaneo (es. admin) senza toccare quello salvato.
        """
        ok = False
        if self.token:
            h = crypto.token_hash(self.token.token, self._getkey(), self.token.hash_alg)
            cmd = f"jdev/sys/killtoken/{h}/{urllib.parse.quote(self.token.user, safe='')}"
            try:
                ok = ll_code(self.request(self.session.wrap(cmd))) == 200
            except LoxoneError:
                ok = True  # il Miniserver puo' chiudere la connessione dopo aver revocato il token
        if forget_saved:
            delete_token()
        return ok

    # --- ricezione messaggi ---
    def _recv_frame(self):
        opcode, data = self.ws.recv_data()
        if opcode == websocket.ABNF.OPCODE_CLOSE:
            code = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else None
            self.ws = None
            raise LoxoneError(f"il Miniserver ha chiuso la connessione (codice {code}: "
                              f"{CLOSE_MEANING.get(code, 'motivo non specificato')})")
        return opcode, data

    def _recv(self) -> Message:
        kind = None
        previous = self.ws.gettimeout()
        try:
            while True:
                opcode, data = self._recv_frame()
                if opcode == websocket.ABNF.OPCODE_BINARY and len(data) == 8 and data[0] == 0x03:
                    if data[1] == HDR_KEEPALIVE:
                        return Message(HDR_KEEPALIVE, None)
                    if data[1] == HDR_OUT_OF_SERVICE:
                        raise LoxoneError("il Miniserver e' fuori servizio (aggiornamento o riavvio in corso)")
                    # un header con dimensione stimata (flag 0x80 sul firmware 17) e' seguito da
                    # quello definitivo: vale sempre l'ultimo header ricevuto
                    kind = data[1]
                    self.ws.settimeout(max(previous or 0, 30))  # il contenuto segue subito l'header
                    continue
                if opcode == websocket.ABNF.OPCODE_TEXT:
                    data = data.decode("utf-8", "replace")
                if kind is None:
                    return Message(HDR_TEXT if isinstance(data, str) else -1, data)
                return self._apply_events(Message(kind, data))
        finally:
            if self.ws is not None:
                self.ws.settimeout(previous)

    def _apply_events(self, msg: Message) -> Message:
        entry = EVENT_PARSERS.get(msg.kind)
        if entry and isinstance(msg.payload, (bytes, bytearray)):
            parser, store = entry
            try:
                msg.events = parser(msg.payload)
            except (struct.error, IndexError):
                msg.events = {}
            getattr(self, store).update(msg.events)
        return msg

    def request(self, cmd: str, encrypted_response: bool = False, timeout: float | None = None) -> dict:
        """Invia un comando e restituisce il blocco "LL" della risposta."""
        self.ws.settimeout(timeout or self.timeout)
        self.ws.send(cmd)
        while True:
            msg = self._recv()
            if msg.kind != HDR_TEXT or not isinstance(msg.payload, str):
                continue  # eventi di stato o keepalive arrivati nel frattempo
            text = msg.payload.strip()
            if encrypted_response and not text.startswith("{"):
                text = self.session.decrypt(text)
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as e:
                raise LoxoneError(f"risposta non valida dal Miniserver: {text[:200]!r}") from e
            return obj.get("LL", obj) if isinstance(obj, dict) else {"value": obj}

    # --- funzioni di alto livello ---
    def io(self, uuid_action: str, command: str) -> dict:
        """Comando a un controllo: jdev/sps/io/<uuidAction>/<comando>."""
        cmd = urllib.parse.quote(command, safe="/(),:.;=+-_~!*'%")
        return self.request(f"jdev/sps/io/{uuid_action}/{cmd}")

    def enable_status_updates(self, settle: float = 1.5, max_wait: float = 30) -> None:
        """Riceve la tabella iniziale con lo stato di tutti i controlli visibili."""
        self.ws.settimeout(self.timeout)
        self.ws.send("jdev/sps/enablebinstatusupdate")
        got_reply = False
        deadline = time.monotonic() + max_wait
        self.ws.settimeout(settle)
        try:
            while time.monotonic() < deadline:
                try:
                    msg = self._recv()
                except websocket.WebSocketTimeoutException:
                    if got_reply or self.values or self.texts:
                        break
                    continue
                if msg.kind == HDR_TEXT:
                    got_reply = True
        finally:
            if self.ws is not None:
                self.ws.settimeout(self.timeout)

    def watch(self, seconds: float):
        """Generatore di (uuid_stato, valore) per ogni cambiamento ricevuto."""
        end = time.monotonic() + seconds
        last_keepalive = time.monotonic()
        while (remaining := end - time.monotonic()) > 0:
            self.ws.settimeout(min(remaining, 30))
            try:
                msg = self._recv()
            except websocket.WebSocketTimeoutException:
                msg = None
            if msg is not None and msg.events:
                yield from msg.events.items()
            if time.monotonic() - last_keepalive > 120:  # il Miniserver chiude dopo 5 minuti di silenzio
                self.ws.send("keepalive")
                last_keepalive = time.monotonic()
        self.ws.settimeout(self.timeout)

    def download(self, path: str, timeout: float = 60) -> bytes | str:
        """Scarica un file (es. data/LoxAPP3.json): testo o binario a seconda del file."""
        self.ws.settimeout(timeout)
        self.ws.send(path)
        try:
            while True:
                msg = self._recv()
                if msg.kind not in (HDR_TEXT, HDR_FILE):
                    continue
                if isinstance(msg.payload, str) and msg.payload.lstrip().startswith('{"LL"'):
                    ll = json.loads(msg.payload)["LL"]
                    if ll_code(ll) != 200:
                        raise LoxoneError(f"impossibile scaricare {path} ({explain(ll_code(ll))})")
                return msg.payload
        finally:
            if self.ws is not None:
                self.ws.settimeout(self.timeout)

    def http_download(self, path: str, timeout: float = 60) -> bytes:
        """Scarica un file via HTTP (es. dev/fsget/prog/<file>.zip).

        Il WebSocket restituisce i file dentro un testo JSON e tronca quelli binari, quindi
        qui si usa HTTP con l'autenticazione "autht": un hash del token calcolato con una chiave
        monouso (il token vero non viaggia mai in rete).
        """
        if not self.token:
            raise AuthError("serve il login (token) per scaricare file")
        conn = http.client.HTTPConnection(self.s.host, 80, timeout=timeout)
        try:
            conn.request("GET", "/jdev/sys/getkey")
            key = json.loads(conn.getresponse().read().decode("utf-8", "replace"))["LL"]["value"]
            h = crypto.token_hash(self.token.token, str(key), self.token.hash_alg)
            query = urllib.parse.urlencode({"autht": h, "user": self.token.user})
            conn.request("GET", f"/{path.lstrip('/')}?{query}")
            resp = conn.getresponse()
            data = resp.read()
        finally:
            conn.close()
        if resp.status != 200:
            raise LoxoneError(f"download di {path} rifiutato (HTTP {resp.status})")
        return data

    def connect_admin(self, rights: int = ADMIN_OPS_PERMISSION) -> "Miniserver":
        """Connessione con un token TEMPORANEO (non salvato) che include i diritti admin richiesti.

        Serve per le operazioni di sistema (es. riavvio della logica dopo un caricamento): il
        token normale "App" non le autorizza. Verifica che il diritto Sys-WS sia stato concesso.
        """
        if not (self.s.user and self.s.password):
            raise AuthError("per le operazioni admin serve la password dell'utente nel file .env")
        self.connect(login=False)
        tok = self.login_with_password(self.s.user, self.s.password, permission=rights, persist=False)
        if not (tok.rights or 0) & RIGHT_SYS_WS:
            self.close()
            raise AuthError(f"l'utente '{self.s.user}' non ha il diritto 'Webservice di sistema' (Sys-WS): "
                            "rendilo amministratore in Loxone Config")
        return self

    def restart_program(self) -> dict:
        """Riavvia la logica del Miniserver (dev/sps/restart): carica /prog/sps_new.zip se presente.

        Richiede i diritti Sys-WS. Il riavvio e' breve (~10 s) ma interrompe la logica di casa.
        La connessione viene quasi sempre chiusa dal Miniserver durante il riavvio.
        """
        try:
            return self.request("jdev/sps/restart", timeout=20)
        except LoxoneError:
            return {"Code": "200", "value": "restart (connessione chiusa dal Miniserver)"}

    def list_files(self, folder: str) -> list[dict]:
        """Elenco dei file di una cartella della scheda SD (dev/fslist)."""
        ll = self.request(f"dev/fslist/{folder.strip('/')}")
        if ll_code(ll) != 200:
            raise LoxoneError(f"impossibile elencare /{folder} ({explain(ll_code(ll))})")
        value = ll.get("value")
        data = value if isinstance(value, dict) else loose_json(value)
        return data.get("content", [])

    def structure(self, refresh: bool = False) -> dict:
        """File di struttura LoxAPP3.json, con cache locale aggiornata quando cambia."""
        current = str(self.request("jdev/sps/LoxAPPversion3").get("value", ""))
        if STRUCTURE_CACHE.exists() and not refresh:
            cached = json.loads(STRUCTURE_CACHE.read_text(encoding="utf-8"))
            if cached.get("lastModified") == current:
                return cached
        payload = self.download("data/LoxAPP3.json")
        text = payload if isinstance(payload, str) else payload.decode("utf-8", "replace")
        data = json.loads(text)
        STATE_DIR.mkdir(exist_ok=True)
        STRUCTURE_CACHE.write_text(text, encoding="utf-8")
        return data
