# Govee Bluetooth: Loxone comanda le luci LED Govee

Stato: stabile. Gira su: Raspberry Pi (o qualunque Linux con Bluetooth LE). Servizio: `loxone-govee.service`.
Python: 3.10 o successivo (provato sul Raspberry con 3.13).

Le strisce LED Govee si comandano dall'app Govee via Bluetooth, ma Loxone non parla Bluetooth. Il Raspberry fa da
ponte: riceve da Loxone comandi di testo via UDP e li traduce nei pacchetti Bluetooth della striscia. Si possono
comandare piu' strisce insieme (ricevono gli stessi comandi).

## Dispositivi provati

| Modello | Firmware | Note |
|---|---|---|
| Govee H6126 (nome Bluetooth `GBK_H6126_xxxx`) | 2.06.1 | due strisce insieme; colore in modalita' a segmenti (0x15), luminosita' 0-100; scene classiche (Snowflake = 15, Sunrise = 0, Rainbow = 22) e animate del catalogo con cmdVersion 0 (Aurora 215, Fire 216, Forest 212: la striscia le conferma con `AA 05 04 <codice>`) |

Altri modelli Govee Bluetooth usano lo stesso schema di pacchetti con piccole varianti: si scelgono con
`GOVEE_COLOR_MODE` (02 classico, 0d, 15 a segmenti) e `GOVEE_BRIGHTNESS_MAX` (255 o 100); il comando `prova`
legge la risposta della striscia dopo ogni comando e serve a scoprire le varianti giuste.

## Come funziona

- **Collegamento**: Bluetooth LE con la libreria [bleak](https://github.com/hbldh/bleak) (licenza MIT, la
  installa `install.sh`), caratteristica di scrittura `00010203-0405-0607-0809-0a0b0c0d2b11`, risposte sulla
  `...2b10`. La striscia accetta **un solo collegamento**: chiudi l'app Govee del telefono.
- **Pacchetti** da 20 byte: primo byte `0x33` (comando) o `0xAA` (richiesta), poi comando e dati, zeri, ultimo
  byte = XOR dei 19 precedenti.

| Cosa | Pacchetto (senza zeri e XOR) |
|---|---|
| accendi / spegni | `33 01 01` / `33 01 00` |
| luminosita' | `33 04 <livello>` (0-100 sulla H6126, 0-255 su modelli piu' vecchi) |
| colore classico | `33 05 02 R G B` (variante `33 05 0D R G B`) |
| colore a segmenti (H6126) | `33 05 15 01 R G B 00 00 00 00 00 FF 7F` (tutti i segmenti) |
| scena | `33 05 04 <codice basso> <codice alto>` (Snowflake = 15; codici del modello nel catalogo) |
| dati di una scena animata | `A3 00 01 <totale> 02` + 14 byte, `A3 <n>` + 17 byte, ..., `A3 FF` + il resto; poi il codice |
| stato / tenere vivo il collegamento | `AA 01` (accensione), `AA 04` (luminosita'), `AA 05` (modo e colore), `AA 06` (firmware) |

- Senza traffico la striscia chiude il collegamento: il ponte manda `AA 01` ogni 2 secondi.
- I comandi che arrivano insieme (entro 0,15 s) si uniscono: un cursore che manda 20 valori ne invia uno; il
  colore nero o la luminosita' 0 spengono solo se nel gruppo non c'e' altro che accende (passando a una scena
  Loxone manda "colore nero" e subito dopo la scena: senza questa regola la luce si spegne e riaccende).
- Con piu' strisce il Bluetooth del Raspberry cerca e collega una luce alla volta; all'arresto il ponte chiude
  i collegamenti (se restano appesi la striscia si scollega poco dopo la ripartenza).
- **Colori pieni**: Loxone manda il colore gia' attenuato dalla luminosita' del selettore (viola al 40% =
  `color:58-0-102`). Con pochi livelli per canale i colori deboli verrebbero sbagliati: il ponte manda il colore
  pieno (`145-0-255`) e luminosita' = ultima `brightness:` di Loxone x intensita' del colore (100 se Loxone non ne
  ha mandata una). Le scene tornano a piena intensita'.
- **Scene animate**: le strisce RGBIC hanno decine di scene (Aurora, Fire, Forest...) che vogliono i dati
  dell'animazione in piu' pacchetti `A3` prima del codice. Nomi, codici e dati sono pubblici nella libreria delle
  scene di Govee (la stessa dell'app, senza account): `catalogo <modello>` la scarica UNA volta, tiene per ogni scena
  la versione per i firmware vecchi (cmdVersion 0, si cambia con `--cmd-version`) e salva un catalogo compatto in
  `/var/lib/loxone-govee/scene.json`. Da li' in poi il ponte lavora solo in locale. Il catalogo si scrive tutto
  o niente (file temporaneo, poi scambio); se manca o e' rovinato il ponte lo dice nel log e continua con le sole
  scene classiche. Il formato dei pacchetti e' quello di altri progetti open source (stesso risultato, byte per
  byte, di govee_ble_lights).

## Collegamento con Loxone

Un'uscita virtuale con indirizzo `/dev/udp/<ip-raspberry>/55000` e comandi di uscita virtuale con questi testi:

| Testo | Effetto |
|---|---|
| `on`, `accendi led` / `off`, `spegni led` | accende / spegne |
| `brightness:<0-100>`, `brightness:off` | luminosita' in percento (0 o `off` spengono) |
| `color:<R>-<G>-<B>` | colore 0-255 (0-0-0 spegne) |
| `<nome scena>`, `scena:<nome scena>` | scena Govee, es. `Snowflake`, `Movie`, `Aurora`, `scena:Starry Sky` (maiuscole e spazi non contano) |
| `0` | "nessuna scena": lo manda il comando `<v>` delle scene quando si lascia un'atmosfera-scena (ignorato) |
| `test` | rosso, verde, blu: prova del collegamento |

Esempio provato con un controllo luci (LightControllerV2): l'uscita AQ1 passa da un blocco di stato che scrive il
valore se e' tra 1 e 100, altrimenti `off`, al comando analogico `brightness:<v>`; l'uscita RGB passa da un
LightsceneRGB e tre formule `INT(I1*255/10)` a un blocco di stato `color:<v1>-<v2>-<v3>`. Le scene sono atmosfere
del controllo luci (circuito dimmer acceso, circuito colore nero): un blocco di stato traduce il numero
dell'atmosfera attiva (uscita Scene) nel nome della scena e un comando `<v>` testuale lo manda al ponte. Lo script
[`scenari/luci_scene_govee.py`](../../scenari/luci_scene_govee.py) aggiunge atmosfere e collegamenti a una copia del
programma. Piu' comandi nello stesso pacchetto UDP si separano con `;` o a capo; un pacchetto piu' lungo di 256
byte viene ignorato. `ALLOW_FROM` limita i mittenti (di solito il Miniserver e `127.0.0.1`): vedi "Sicurezza e
privacy".

## Installazione e configurazione

Il programma gira con il Python dell'ambiente preparato da `install.sh` (li' c'e' la libreria bleak): i comandi
vanno lanciati cosi', dall'utente dei servizi (di solito quello che ha lanciato `sudo`).

```bash
sudo bash raspberry/install.sh govee-bluetooth      # dal repository, sul Raspberry
P=/opt/claudelox-raspberry/venv/bin/python; G=/opt/claudelox-raspberry/loxone_govee.py
$P $G scan                                          # trova le luci: indirizzo e nome (GBK_H6126_xxxx)
sudo nano /etc/default/loxone-govee                 # GOVEE_ADDRESS=AA:BB:CC:DD:EE:01,AA:BB:CC:DD:EE:02
                                                    # ALLOW_FROM=<IP del Miniserver>,127.0.0.1
$P $G catalogo H6126                                # una volta: le scene del modello (va su internet)
sudo systemctl enable loxone-govee && sudo systemctl restart loxone-govee
```

Finche' `GOVEE_ADDRESS` e' vuoto `install.sh` non avvia il servizio (e il ponte, se lanciato, esce con il codice
78 senza essere riavviato): si abilita con l'ultimo comando, dopo aver scritto l'indirizzo. Se `scan` dice che il
Bluetooth nega l'accesso, esci e rientra nella sessione: `install.sh` ha appena messo l'utente nel gruppo
`bluetooth`. Esempio di configurazione: [`loxone-govee.env.example`](loxone-govee.env.example).

## Comandi

```bash
P=/opt/claudelox-raspberry/venv/bin/python; G=/opt/claudelox-raspberry/loxone_govee.py
$P $G scan                        # luci Govee vicine (non usa GOVEE_ADDRESS)
$P $G catalogo H6126              # scarica il catalogo delle scene del modello (libreria pubblica Govee)
$P $G scene                       # elenca le scene del catalogo (nome, codice, classica o animata)
$P $G send "color:255-0-0"        # manda un comando al ponte in esecuzione, come farebbe Loxone

# query e prova parlano direttamente con la luce: leggono GOVEE_ADDRESS e le varianti del modello
# dall'ambiente e vogliono il servizio fermo (la striscia accetta un solo collegamento)
set -a; . /etc/default/loxone-govee; set +a       # carica la configurazione in questo terminale
sudo systemctl stop loxone-govee
$P $G query                       # stato e firmware della luce
$P $G prova "brightness:30"       # manda comandi alla luce e ne legge l'effetto (per scoprire le varianti)
$P $G prova "scena:Aurora"        # "modo e colore: aa 05 04 d7 ..." = scena 215 attiva
sudo systemctl start loxone-govee
```

- Il servizio prende la configurazione da `/etc/default/loxone-govee`; un comando lanciato a mano no: la legge
  dall'ambiente del terminale. Senza `set -a; . /etc/default/loxone-govee; set +a`, `query` e `prova` si fermano
  con "GOVEE_ADDRESS e' vuoto"; caricando tutto il file `prova` usa anche le varianti giuste del modello
  (`GOVEE_COLOR_MODE`, `GOVEE_BRIGHTNESS_MAX`).
- `send` manda un pacchetto UDP a `127.0.0.1` (porta `UDP_PORT`): il ponte lo esegue solo se `127.0.0.1` e' tra
  gli IP di `ALLOW_FROM` (o se `ALLOW_FROM` e' vuoto), altrimenti scrive nel log "comando da 127.0.0.1 ignorato".
- `run` e' il ponte: lo avvia il servizio.

Log: `journalctl -u loxone-govee -f`.

## Sicurezza e privacy

- Comanda solo luci. I comandi arrivano via UDP **senza autenticazione**: con `ALLOW_FROM` vuoto (e' il
  predefinito) **chiunque sia collegato alla rete di casa puo' accendere, spegnere e colorare le luci**; il ponte
  lo scrive nel log a ogni avvio. Per limitarlo: `ALLOW_FROM=<IP del Miniserver>,127.0.0.1` in
  `/etc/default/loxone-govee`. E' un filtro sull'indirizzo del mittente, che in una rete locale si puo'
  falsificare: ferma gli errori e i curiosi, non un attacco deciso.
- Un pacchetto piu' lungo di 256 byte viene ignorato senza essere letto (un avviso nel log, senza il contenuto).
- Salva solo il catalogo delle scene (dati pubblici di Govee); `catalogo` e' l'unico comando che va su internet,
  e solo quando lo si lancia.
- Il servizio gira senza privilegi, con le protezioni di systemd dell'unita' (`NoNewPrivileges`, `ProtectSystem`,
  `ProtectHome`...); i programmi in `/opt/claudelox-raspberry` sono di root.

## Test

`tests/test_govee.py`: pacchetti e XOR, varianti di colore, traduzione dei testi di Loxone (colore letto in tempo
lineare, con le stesse risposte di prima), pacchetti troppo lunghi, mittenti ammessi, unione dei comandi arrivati
insieme (sequenze reali di Loxone), configurazione, indirizzo mancante (codice 78), pacchetti A3 delle scene
animate, catalogo (anche rovinato o scritto a meta'), colori pieni con luminosita' ridotta.

## Limiti noti

- Una striscia, un collegamento: con l'app Govee aperta il ponte non entra (e viceversa).
- Due comandi dello stesso tipo arrivati quasi insieme, il secondo dei quali spegne: `brightness:4` e subito
  `brightness:off`, oppure un colore e subito `color:0-0-0`. Se il primo non e' ancora stato mandato alla striscia
  (ci vogliono circa 0,3 s; di piu' se la striscia non e' collegata) il ponte scarta quello che spegne e la
  striscia resta accesa con il valore di prima: non lo distingue dal "nero" che Loxone manda passando a una scena.
  Un `off` spegne sempre (nell'impianto provato l'atmosfera "Off" del controllo luci manda `brightness:off` e
  `off`).
- I codici delle scene cambiano tra i modelli: verificali con `prova` e correggili con `GOVEE_SCENES`.
- `prova` conferma che la striscia ha accettato il codice della scena, non che l'animazione sia quella giusta:
  per le scene animate guarda la striscia.
- Dopo un riavvio del ponte la luminosita' di Loxone non e' nota finche' Loxone non ne manda una: nel frattempo un
  colore cambiato dal selettore usa 100 come base.
- Portata del Bluetooth: qualche metro, dipende dai muri.
