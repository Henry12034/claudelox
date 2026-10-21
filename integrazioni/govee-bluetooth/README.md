# Govee Bluetooth: Loxone comanda le luci LED Govee

Stato: stabile. Gira su: Raspberry Pi (o qualunque Linux con Bluetooth LE). Servizio: `loxone-govee.service`.

Le strisce LED Govee si comandano dall'app Govee via Bluetooth, ma Loxone non parla Bluetooth. Il Raspberry fa da
ponte: riceve da Loxone comandi di testo via UDP e li traduce nei pacchetti Bluetooth della striscia. Si possono
comandare piu' strisce insieme (ricevono gli stessi comandi).

## Dispositivi provati

| Modello | Firmware | Note |
|---|---|---|
| Govee H6126 (nome Bluetooth `GBK_H6126_xxxx`) | 2.06.1 | due strisce insieme; colore in modalita' a segmenti (0x15), luminosita' 0-100, scena Snowflake = 15 |

Altri modelli Govee Bluetooth usano lo stesso schema di pacchetti con piccole varianti: si scelgono con
`GOVEE_COLOR_MODE` (02 classico, 0d, 15 a segmenti) e `GOVEE_BRIGHTNESS_MAX` (255 o 100); il comando `prova`
legge la risposta della striscia dopo ogni comando e serve a scoprire le varianti giuste.

## Come funziona

- **Collegamento**: Bluetooth LE, caratteristica di scrittura `00010203-0405-0607-0809-0a0b0c0d2b11`, risposte
  sulla `...2b10`. La striscia accetta **un solo collegamento**: chiudi l'app Govee del telefono.
- **Pacchetti** da 20 byte: primo byte `0x33` (comando) o `0xAA` (richiesta), poi comando e dati, zeri, ultimo
  byte = XOR dei 19 precedenti.

| Cosa | Pacchetto (senza zeri e XOR) |
|---|---|
| accendi / spegni | `33 01 01` / `33 01 00` |
| luminosita' | `33 04 <livello>` (0-100 sulla H6126, 0-255 su modelli piu' vecchi) |
| colore classico | `33 05 02 R G B` (variante `33 05 0D R G B`) |
| colore a segmenti (H6126) | `33 05 15 01 R G B 00 00 00 00 00 FF 7F` (tutti i segmenti) |
| scena | `33 05 04 <codice>` (Snowflake = 15; altri codici in `DEFAULT_SCENES`, da verificare per modello) |
| stato / tenere vivo il collegamento | `AA 01` (accensione), `AA 04` (luminosita'), `AA 05` (modo e colore), `AA 06` (firmware) |

- Senza traffico la striscia chiude il collegamento: il ponte manda `AA 01` ogni 2 secondi.
- I comandi che arrivano insieme (entro 0,15 s) si uniscono: un cursore che manda 20 valori ne invia uno; il
  colore nero o la luminosita' 0 spengono solo se nel gruppo non c'e' altro che accende (passando a una scena
  Loxone manda "colore nero" e subito dopo la scena: senza questa regola la luce si spegne e riaccende).
- Con piu' strisce il Bluetooth del Raspberry cerca e collega una luce alla volta; all'arresto il ponte chiude
  i collegamenti (se restano appesi la striscia si scollega poco dopo la ripartenza).

## Collegamento con Loxone

Un'uscita virtuale con indirizzo `/dev/udp/<ip-raspberry>/55000` e comandi di uscita virtuale con questi testi:

| Testo | Effetto |
|---|---|
| `on`, `accendi led` / `off`, `spegni led` | accende / spegne |
| `brightness:<0-100>`, `brightness:off` | luminosita' in percento (0 o `off` spengono) |
| `color:<R>-<G>-<B>` | colore 0-255 (0-0-0 spegne) |
| `<nome scena>` | scena Govee, es. `Snowflake`, `Movie`, `Romantic` |
| `test` | rosso, verde, blu: prova del collegamento |

Esempio provato con un controllo luci (LightControllerV2): l'uscita AQ1 passa da un blocco di stato che scrive il
valore se e' tra 1 e 100, altrimenti `off`, al comando analogico `brightness:<v>`; l'uscita RGB passa da un
LightsceneRGB e tre formule `INT(I1*255/10)` a un blocco di stato `color:<v1>-<v2>-<v3>`; le scene con un
blocco "uguale" sull'uscita Scene e un impulso verso il comando con il nome della scena. Piu' comandi nello
stesso pacchetto UDP si separano con `;` o a capo. `ALLOW_FROM` limita i mittenti (di solito il solo Miniserver).

## Installazione e configurazione

```bash
sudo raspberry/install.sh govee-bluetooth           # dal repository, sul Raspberry
python3 /opt/claudelox-raspberry/loxone_govee.py scan   # trova le luci (con il servizio fermo o le altre luci)
sudo nano /etc/default/loxone-govee                 # GOVEE_ADDRESS=AA:BB:CC:DD:EE:01,AA:BB:CC:DD:EE:02
sudo systemctl restart loxone-govee
```

Esempio di configurazione: [`loxone-govee.env.example`](loxone-govee.env.example).

## Comandi

```bash
loxone_govee.py run                    # il ponte (lo avvia il servizio)
loxone_govee.py scan                   # luci Govee vicine
loxone_govee.py send "color:255-0-0"   # manda un comando al ponte in esecuzione (come farebbe Loxone)
loxone_govee.py query                  # legge stato e firmware della luce (collegamento diretto)
loxone_govee.py prova "brightness:30"  # manda comandi alla luce e ne legge l'effetto (per scoprire le varianti)
```

Log: `journalctl -u loxone-govee -f`.

## Sicurezza e privacy

Comanda solo luci. Accetta comandi UDP solo dagli IP in `ALLOW_FROM`; non salva niente.

## Test

`tests/test_govee.py`: pacchetti e XOR, varianti di colore, traduzione dei testi di Loxone, unione dei comandi
arrivati insieme (sequenze reali di Loxone), configurazione.

## Limiti noti

- Una striscia, un collegamento: con l'app Govee aperta il ponte non entra (e viceversa).
- I codici delle scene cambiano tra i modelli: verificali con `prova` e correggili con `GOVEE_SCENES`.
- Portata del Bluetooth: qualche metro, dipende dai muri.
