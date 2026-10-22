#!/usr/bin/env python3
"""Ponte sensore della qualita' dell'aria Amazon -> Loxone, pensato per un Raspberry Pi.

L'"Amazon Smart Air Quality Monitor" non ha un accesso locale: manda i valori solo al cloud Amazon (li
mostra l'app Alexa). Questo ponte li legge da li' con la libreria aioamazondevices (la stessa
dell'integrazione ufficiale "Alexa Devices" di Home Assistant) e li offre in rete locale come pagina di
testo, che il Miniserver legge con un ingresso virtuale HTTP:

  GET http://<raspberry>:8095/aria      online=1
                                        qualita=95
                                        pm25=3.0
                                        ...
  comando di riconoscimento in Loxone, per esempio:  \\ipm25=\\i\\v

Valori: qualita (indice Amazon, piu' alto = aria migliore), pm25 e pm10 (microgrammi/m3), voc (indice),
co (ppm), umidita (%), temperatura (gradi C), online (1 = valori aggiornati e sensore raggiungibile),
eta (secondi dall'ultima misura). Un valore che Amazon non fornisce manca dalla pagina: Loxone tiene
l'ultimo. /aria/<n> mostra l'n-esimo sensore dell'account (in ordine di nome), /aria.json tutti.

Configurazione da variabili d'ambiente (sul Raspberry: /etc/default/amazon-loxone):
  HTTP_PORT     porta della pagina (8095)
  ALLOW_FROM    IP ammessi, separati da virgola (vuoto = tutti): di solito il Miniserver e 127.0.0.1
  INTERVAL_S    ogni quanti secondi chiedere i valori ad Amazon (300, minimo 60)
  DEVICE        nome del sensore nell'app Alexa o numero di serie (vuoto = il primo)
  STATE_DIR     cartella dei dati di accesso (/var/lib/amazon-loxone)

Uso:
  amazon_loxone.py accedi    accesso ad Amazon: email, password e codice di verifica (una volta sola)
  amazon_loxone.py leggi     chiede i valori ad Amazon e li stampa (prova)
  amazon_loxone.py run       avvia il ponte (e' cio' che fa il servizio systemd)
  amazon_loxone.py esci      cancella i dati di accesso salvati
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import math
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEVICE_TYPE_AQM = "AEZME1X38KDRA"  # tipo Amazon del sensore (aioamazondevices.const.devices)
LOGIN_FILE = "accesso.json"
LOGIN_STEPS = 3      # codici di verifica accettati di fila (uno sbagliato si puo' riscrivere)
AUTH_RETRY_S = 1800  # con l'accesso scaduto si riprova di rado, per non insistere con Amazon
RENEW_S = 3000       # rinnovo del token di Amazon, che dura un'ora (le letture usano i cookie)

# chiave del sensore nella libreria -> (nome nella pagina, decimali)
VALUES = {
    "Air Quality": ("qualita", 0),
    "PM25": ("pm25", 1),
    "PM10": ("pm10", 1),
    "VOC": ("voc", 1),
    "CO": ("co", 1),
    "Humidity": ("umidita", 0),
    "temperature": ("temperatura", 1),
}
DECIMALS = dict(VALUES.values())

log = logging.getLogger("amazon-loxone")


@dataclass
class Settings:
    port: int = 8095
    allow_from: frozenset[str] = frozenset()
    interval_s: int = 300
    device: str = ""
    state_dir: Path = Path("/var/lib/amazon-loxone")

    @classmethod
    def from_env(cls, env=None) -> Settings:
        env = os.environ if env is None else env
        return cls(port=int(env.get("HTTP_PORT") or 8095),
                   allow_from=frozenset(ip.strip() for ip in env.get("ALLOW_FROM", "").split(",") if ip.strip()),
                   interval_s=max(60, int(env.get("INTERVAL_S") or 300)),
                   device=env.get("DEVICE", "").strip(),
                   state_dir=Path(env.get("STATE_DIR") or "/var/lib/amazon-loxone"))

    @property
    def stale_s(self) -> int:
        """Oltre quest'eta' della lettura i valori non contano piu' come aggiornati (online=0)."""
        return 3 * self.interval_s


@dataclass
class Reading:
    """Valori di un sensore, come li ha restituiti Amazon."""
    name: str
    serial: str
    reachable: bool
    values: dict[str, float] = field(default_factory=dict)
    sampled: float | None = None  # ora (epoch) della misura piu' recente secondo Amazon
    fetched: float = 0.0          # ora (epoch) della lettura dal cloud
    software: str | None = None


# --- valori -------------------------------------------------------------------------------------------

def sensor_values(sensors: dict) -> dict[str, float]:
    """Valori numerici dei sensori della libreria (AmazonDeviceSensor), con i nomi della pagina."""
    out: dict[str, float] = {}
    for key, (name, _) in VALUES.items():
        s = sensors.get(key)
        if s is None or s.error:
            continue
        try:
            v = float(s.value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        if key == "temperature" and str(s.scale or "").upper().startswith("F"):
            v = (v - 32) * 5 / 9
        out[name] = v
    return out


def latest_sample(sensors: dict) -> float | None:
    times = [s.time_of_sample.timestamp() for s in sensors.values()
             if not s.error and getattr(s, "time_of_sample", None) is not None]
    return max(times) if times else None


def is_air_monitor(device) -> bool:
    return device.device_type == DEVICE_TYPE_AQM or "Air Quality" in device.sensors


def readings_from_devices(devices: dict, fetched: float) -> list[Reading]:
    """I sensori della qualita' dell'aria tra i dispositivi dell'account, in ordine di nome."""
    out = []
    for d in devices.values():
        if not is_air_monitor(d):
            continue
        reach = d.sensors.get("reachability")
        reachable = reach.value == "OK" if reach is not None and not reach.error else bool(d.online)
        out.append(Reading(d.account_name or d.serial_number, d.serial_number, reachable,
                           sensor_values(d.sensors), latest_sample(d.sensors), fetched,
                           getattr(d, "software_version", None)))
    return sorted(out, key=lambda r: (r.name.lower(), r.serial))


def pick(readings: list[Reading], wanted: str = "", index: int | None = None) -> Reading | None:
    """Il sensore della pagina: l'n-esimo (da 1), quello indicato in DEVICE (nome o serie) o il primo."""
    if index is not None:
        return readings[index - 1] if 1 <= index <= len(readings) else None
    if wanted:
        w = wanted.lower()
        return next((r for r in readings if w in (r.name.lower(), r.serial.lower())), None)
    return readings[0] if readings else None


def render(reading: Reading | None, now: float, stale_s: int) -> str:
    """Pagina di testo per l'ingresso virtuale HTTP di Loxone: una riga chiave=valore."""
    if reading is None:
        return "online=0\n"
    fresh = reading.reachable and now - reading.fetched <= stale_s
    lines = [f"online={int(fresh)}"]
    lines += [f"{k}={v:.{DECIMALS[k]}f}" for k, v in reading.values.items()]
    lines.append(f"eta={max(0, int(now - (reading.sampled or reading.fetched)))}")
    return "\n".join(lines) + "\n"


def summary(r: Reading) -> str:
    vals = " ".join(f"{k}={v:.{DECIMALS[k]}f}" for k, v in r.values.items()) or "nessun valore"
    return f"'{r.name}': {vals}" + ("" if r.reachable else " (sensore non raggiungibile)")


def allowed(remote: str | None, allow: frozenset[str]) -> bool:
    if not allow:
        return True
    if remote and remote.startswith("::ffff:"):
        remote = remote[7:]
    return remote in allow


# --- dati di accesso ----------------------------------------------------------------------------------

def save_login(state_dir: Path, data: dict) -> Path:
    """Salva i dati di accesso (token di Amazon, non la password), leggibili solo dall'utente del servizio."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / LOGIN_FILE
    tmp = path.with_name(LOGIN_FILE + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return path


def load_login(state_dir: Path) -> dict | None:
    try:
        data = json.loads((state_dir / LOGIN_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return data if isinstance(data, dict) and data.get("refresh_token") else None


class LoginStopped(Exception):
    """Amazon ha mostrato una pagina che il ponte non sa gestire: il messaggio la descrive."""


def _is_hidden(el) -> bool:
    """Elemento che la pagina non mostra: nascosto, solo per chi non ha JavaScript, oppure uno dei messaggi
    che il browser fa comparire dopo i suoi controlli (Amazon li mette tutti nella pagina in anticipo)."""
    while el is not None and getattr(el, "name", None) not in (None, "[document]"):
        classes = el.get("class") or []
        if (el.name == "noscript" or el.has_attr("hidden") or el.get("id") == "auth-alert-window"
                or {"aok-hidden", "a-hidden", "a-alert-inline", "auth-client-side-message-box"} & set(classes)
                or "display:none" in (el.get("style") or "").replace(" ", "")):
            return True
        el = el.parent
    return False


def describe_page(soup, url) -> str:
    """Cosa mostra una pagina di Amazon, senza dati riservati: indirizzo, titolo, messaggi, campi da compilare."""
    def text(el):
        return " ".join(el.get_text(" ", strip=True).split())[:200]

    parts = [f"pagina {getattr(url, 'path', url)}"]
    title = soup.find("title")
    if title is not None and text(title):
        parts.append(f"titolo «{text(title)}»")
    for label, found in (("intestazioni", soup.find_all(["h1", "h2"])),
                         ("messaggi", soup.find_all(class_="a-alert-content"))):
        shown = [text(e) for e in found if text(e) and not _is_hidden(e)][:3]
        if shown:
            parts.append(f"{label}: " + " | ".join(f"«{t}»" for t in shown))
    fields = sorted({i.get("name") or i.get("id") or "?" for i in soup.find_all("input")
                     if i.get("type") not in ("hidden", "submit", "checkbox", "radio", "button")
                     and not _is_hidden(i)})
    if fields:
        parts.append("campi: " + ", ".join(fields))
    forms = sorted({f.get("name") or f.get("id") or "?" for f in soup.find_all("form")})
    if forms:
        parts.append("moduli: " + ", ".join(forms))
    return "; ".join(parts)


async def submit(http, soup, page_url, *, form=None, **fields):
    """Invia un modulo di una pagina di Amazon: i suoi campi nascosti piu' quelli indicati."""
    from yarl import URL
    form = form or soup.find("form", {"name": "signIn"}) or soup.find("form")
    if form is None:
        raise LoginStopped(describe_page(soup, page_url))
    data = {i.get("name"): i.get("value", "") for i in form.find_all("input")
            if i.get("type") == "hidden" and i.get("name")}
    data.update(fields)
    return await http.session_request(method=(form.get("method") or "post").upper(),
                                      url=page_url.join(URL(form.get("action") or "")), input_data=data)


async def login_flow(api, ask) -> dict:
    """Accesso interattivo ad Amazon, passo per passo come login_mode_interactive di aioamazondevices.

    In piu' della libreria: il codice si chiede quando Amazon lo chiede (cosi' funziona anche l'SMS, che
    parte dopo la password), gli account senza verifica in due passaggi entrano senza codice e, se Amazon
    mostra altro (captcha, approvazione dall'app, password sbagliata), LoginStopped lo descrive.
    Usa metodi interni della libreria: la versione e' fissata in raspberry/install.sh.
    """
    lg, http = api.login, api._http_wrapper
    state = lg._session_state_data
    verifier = lg._create_code_verifier()
    soup, resp = await http.session_request(method="GET", url=lg._build_oauth_url(verifier, lg._build_client_id()))
    soup, resp = await submit(http, soup, resp.url, email=state.login_email, password=state.login_password)
    for _ in range(LOGIN_STEPS):
        code = resp.url.query.get("openid.oa2.authorization_code")
        if code:
            break
        otp = soup.find("input", id="auth-mfa-otpcode")
        if otp is None:
            raise LoginStopped(describe_page(soup, resp.url))
        soup, resp = await submit(http, soup, resp.url, form=otp.find_parent("form"), otpCode=ask(),
                                  mfaSubmit="Submit", rememberDevice="false")
    else:
        raise LoginStopped("Amazon continua a chiedere il codice: " + describe_page(soup, resp.url))
    login_data = await lg._register_device({"authorization_code": code, "code_verifier": verifier,
                                            "domain": state.domain})
    state.login_stored_data = login_data
    await lg._domain_refresh_auth_cookies()
    await lg._register_device_capabilities()
    await lg.obtain_account_customer_id()
    login_data["site"] = state.retail_site_url.human_repr().rstrip("/")
    return login_data


def ask_code() -> str:
    return input("Codice di verifica (SMS o app di autenticazione): ").strip().replace(" ", "")


def make_api(session, settings: Settings, login_data: dict | None = None, email: str = "", password: str = ""):
    from aioamazondevices.api import AmazonEchoApi
    from aioamazondevices.structures import AmazonSaveDataConfig
    return AmazonEchoApi(session, email, password, login_data=login_data,
                         save_data=AmazonSaveDataConfig(path=settings.state_dir))


async def fetch(api, renew: bool = True) -> dict:
    """Dispositivi dell'account con i valori dei sensori; con renew prima rinnova il token di accesso."""
    from aioamazondevices.exceptions import AmazonError, CannotAuthenticate
    if renew:
        try:
            await api.login.login_mode_stored_data()
        except CannotAuthenticate:
            raise
        except AmazonError as exc:
            log.warning("rinnovo dell'accesso non riuscito (%r): provo comunque a leggere i valori", exc)
    return await api.get_devices_data()


def print_readings(readings: list[Reading], settings: Settings) -> None:
    if not readings:
        print("Nessun sensore della qualita' dell'aria in questo account Amazon.")
        return
    now = time.time()
    for i, r in enumerate(readings, 1):
        when = datetime.fromtimestamp(r.sampled).strftime("%d/%m/%Y %H:%M:%S") if r.sampled else "?"
        print(f"{i}. {r.name} (serie {r.serial}, software {r.software or '?'}), misura delle {when}")
        print("   " + render(r, now, settings.stale_s).strip().replace("\n", "  "))


# --- comandi ------------------------------------------------------------------------------------------

async def cmd_accedi(settings: Settings) -> int:
    from aiohttp import ClientSession
    from aioamazondevices.exceptions import AmazonError, CannotAuthenticate, MissingCountryError
    email = input("Email dell'account Amazon: ").strip()
    password = getpass.getpass("Password di Amazon (mentre scrivi non si vede nulla): ")
    if not email or not password:
        print("Servono sia l'email sia la password.")
        return 1
    async with ClientSession() as session:
        api = make_api(session, settings, email=email, password=password)
        print("Accesso ad Amazon in corso...")
        try:
            login_data = await login_flow(api, ask_code)
        except LoginStopped as exc:
            print("Amazon non ha concluso l'accesso. Ecco cosa ha mostrato (copialo nella chat):")
            print(f"  {exc}")
            print("Se c'e' il campo della password, di solito vuol dire password sbagliata: riprova scrivendola "
                  "con calma. Se Amazon ti ha mandato un'email o una notifica sul telefono, dillo a Claude.")
            return 1
        except CannotAuthenticate as exc:
            print(f"Amazon ha rifiutato l'accesso ({exc}): riprova.")
            return 1
        except MissingCountryError:
            print("Nell'account Alexa manca il paese di residenza: impostalo nell'app Alexa e riprova.")
            return 1
        except AmazonError as exc:
            print(f"Accesso non riuscito: {exc!r}")
            return 1
        try:
            devices = await api.get_devices_data()
        except AmazonError as exc:
            print(f"Accesso riuscito, ma la lettura dei dispositivi non e' andata ({exc}).")
            devices = {}
    path = save_login(settings.state_dir, login_data)
    print(f"Accesso riuscito (account Amazon .{api.domain}). Dati di accesso salvati in {path}.")
    print_readings(readings_from_devices(devices, time.time()), settings)
    return 0


async def cmd_leggi(settings: Settings) -> int:
    from aiohttp import ClientSession
    from aioamazondevices.exceptions import AmazonError, CannotAuthenticate
    login_data = load_login(settings.state_dir)
    if login_data is None:
        print("Nessun accesso ad Amazon salvato: esegui prima 'amazon_loxone.py accedi'.")
        return 2
    try:
        async with ClientSession() as session:
            devices = await fetch(make_api(session, settings, login_data))
    except CannotAuthenticate as exc:
        print(f"Amazon rifiuta l'accesso salvato ({exc}): rifai 'amazon_loxone.py accedi'.")
        return 1
    except AmazonError as exc:
        print(f"Lettura da Amazon non riuscita: {exc!r}")
        return 1
    print_readings(readings_from_devices(devices, time.time()), settings)
    others = [d for d in devices.values() if not is_air_monitor(d)]
    if others:
        print("Altri dispositivi Amazon: " + ", ".join(
            f"{d.account_name} ({'in linea' if d.online else 'non in linea'})" for d in others))
    return 0


def cmd_esci(settings: Settings) -> int:
    for name in (LOGIN_FILE, "settings.json"):
        (settings.state_dir / name).unlink(missing_ok=True)
    print("Dati di accesso cancellati. Per revocare del tutto l'accesso elimina il dispositivo 'AioAmazonDevices' "
          "da Amazon > Gestisci contenuti e dispositivi > Dispositivi; poi: sudo systemctl stop amazon-loxone")
    return 0


class Bridge:
    """Ultimi valori letti da Amazon e pagina per il Miniserver."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.readings: list[Reading] = []
        self.refused: set[str] = set()

    def update(self, readings: list[Reading]) -> None:
        if not readings:
            log.warning("nessun sensore della qualita' dell'aria nell'account Amazon")
        for r in readings:
            log.info("aria %s", summary(r))
        self.readings = readings

    async def handle(self, request):
        from aiohttp import web
        if not allowed(request.remote, self.s.allow_from):
            if request.remote not in self.refused:
                self.refused.add(request.remote)
                log.warning("richiesta rifiutata da %s (non in ALLOW_FROM)", request.remote)
            return web.Response(status=403, text="vietato\n")
        n = request.match_info.get("n")
        reading = pick(self.readings, "" if n else self.s.device, int(n) if n else None)
        return web.Response(text=render(reading, time.time(), self.s.stale_s), content_type="text/plain")

    async def handle_json(self, request):
        from aiohttp import web
        if not allowed(request.remote, self.s.allow_from):
            return web.Response(status=403, text="vietato\n")
        now = time.time()
        return web.json_response([{
            "nome": r.name, "serie": r.serial, "raggiungibile": r.reachable, "valori": r.values,
            "online": render(r, now, self.s.stale_s).startswith("online=1"),
            "misura": datetime.fromtimestamp(r.sampled).isoformat() if r.sampled else None,
            "lettura": datetime.fromtimestamp(r.fetched).isoformat() if r.fetched else None,
        } for r in self.readings])


async def poll(api, bridge: Bridge, settings: Settings, stop: asyncio.Event) -> None:
    from aioamazondevices.exceptions import AmazonError, CannotAuthenticate
    auth_lost = False
    next_renew = 0.0
    while not stop.is_set():
        delay = settings.interval_s
        try:
            renew = time.monotonic() >= next_renew
            devices = await fetch(api, renew)
            if renew:
                next_renew = time.monotonic() + RENEW_S
            bridge.update(readings_from_devices(devices, time.time()))
            if auth_lost:
                log.info("accesso ad Amazon di nuovo valido")
                auth_lost = False
        except CannotAuthenticate as exc:
            auth_lost, delay = True, max(delay, AUTH_RETRY_S)
            log.error("Amazon rifiuta l'accesso salvato (%s): rifai 'amazon_loxone.py accedi'", exc)
        except (AmazonError, ValueError) as exc:
            log.warning("lettura da Amazon non riuscita: %r", exc)
        except Exception:  # la libreria cambia spesso: meglio un avviso che un servizio che riparte in loop
            log.exception("errore inatteso nella lettura da Amazon")
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


async def cmd_run(settings: Settings) -> int:
    from aiohttp import ClientSession, web
    login_data = load_login(settings.state_dir)
    if login_data is None:
        log.error("nessun accesso ad Amazon salvato in %s: esegui 'amazon_loxone.py accedi'", settings.state_dir)
        return 2
    bridge = Bridge(settings)
    app = web.Application()
    app.router.add_get("/aria", bridge.handle)
    app.router.add_get(r"/aria/{n:\d+}", bridge.handle)
    app.router.add_get("/aria.json", bridge.handle_json)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.port).start()
    log.info("pagina per Loxone su http://<raspberry>:%d/aria, valori da Amazon ogni %d s (ammessi: %s)",
             settings.port, settings.interval_s, ", ".join(sorted(settings.allow_from)) or "tutti")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        async with ClientSession() as session:
            await poll(make_api(session, settings, login_data), bridge, settings, stop)
    finally:
        await runner.cleanup()
    return 0


class QuietLibrary(logging.Filter):
    """Avvisi della libreria che tornano a ogni lettura senza conseguenze (es. account senza altoparlanti Echo)."""
    NOISE = ("Empty JSON data received", "Failed to refresh communications settings")

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith(self.NOISE)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("comando", choices=["accedi", "leggi", "run", "esci"])
    ap.add_argument("-v", "--verbose", action="store_true", help="piu' dettagli nel log del ponte")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    # i messaggi di debug della libreria possono contenere codici di accesso: restano spenti
    logging.getLogger("aioamazondevices").setLevel(logging.WARNING)
    logging.getLogger("aioamazondevices").addFilter(QuietLibrary())
    settings = Settings.from_env()
    if args.comando == "esci":
        return cmd_esci(settings)
    command = {"accedi": cmd_accedi, "leggi": cmd_leggi, "run": cmd_run}[args.comando]
    try:
        return asyncio.run(command(settings))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
