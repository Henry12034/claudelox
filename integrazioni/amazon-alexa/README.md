# Amazon Alexa (cloud): sensore della qualita' dell'aria in Loxone

Stato: sperimentale. Gira su: Raspberry Pi. Servizio: `amazon-loxone.service`.

L'**Amazon Smart Air Quality Monitor** non ha nessun accesso locale: misura polveri sottili, composti organici
volatili, monossido di carbonio, umidita' e temperatura e manda tutto solo al cloud di Amazon (lo mostra l'app
Alexa). Il ponte sul Raspberry legge i valori da li' ogni 5 minuti e li offre in rete locale come una pagina di
testo, che il Miniserver legge con un ingresso virtuale HTTP. Servono il sensore gia' configurato nell'app
Alexa, un Raspberry (o un altro Linux sempre acceso) e l'accesso all'account Amazon.

## Dispositivi provati

| Modello | Firmware | Note |
|---|---|---|
| Amazon Smart Air Quality Monitor (account amazon.it) | software 200000026 | tutti i valori: indice, PM2.5, PM10, VOC, CO, umidita', temperatura |

## Come funziona

- **Nessuna interfaccia locale**: in rete il sensore non apre nessuna porta TCP (provate 1-10000 e le porte IoT
  comuni). I valori si leggono solo dal cloud di Amazon.
- **Libreria**: [aioamazondevices](https://github.com/chemelli74/aioamazondevices) (Apache 2.0), la stessa
  dell'integrazione ufficiale "Alexa Devices" di Home Assistant, con la versione fissata in `raspberry/install.sh`
  (Amazon cambia spesso le sue interfacce: si aggiorna dopo una prova). Il sensore e' il tipo `AEZME1X38KDRA`;
  i valori arrivano come sensori della libreria: `Air Quality` (indice Amazon, piu' alto = aria migliore),
  `PM25` e `PM10` (microgrammi/m3), `VOC` (indice), `CO` (ppm), `Humidity` (%), `temperature` (gradi, con
  la scala), `reachability` (`OK` = sensore in linea).
- **Accesso**: `amazon_loxone.py accedi` chiede email e password e, se Amazon lo chiede, il codice di verifica.
  La libreria vuole il codice *prima* di iniziare (va bene con un'app di autenticazione) e accetta solo account
  con verifica in due passaggi; il ponte rifa' gli stessi passi della libreria ma chiede il codice quando
  Amazon lo chiede (cosi' funziona anche l'SMS) e accetta gli account senza verifica in due passaggi. Se Amazon
  mostra altro (captcha, approvazione dall'app, password sbagliata) il ponte descrive la pagina senza dati
  riservati. Amazon registra un dispositivo "AioAmazonDevices" nell'account; il ponte salva i suoi codici di
  accesso, **non la password**.
- **Letture**: ogni 5 minuti (come Home Assistant); il token di Amazon si rinnova ogni 50 minuti.

## Collegamento con Loxone

Un **ingresso virtuale HTTP** con indirizzo `http://<raspberry>:8095/aria` e ciclo di lettura di 60 s. La pagina:

```
online=1
qualita=95
pm25=0.0
pm10=0.0
voc=1.0
co=0.0
umidita=51
temperatura=17.5
eta=145
```

Un comando dell'ingresso virtuale per ogni valore, con il riconoscimento `\iqualita=\i\v`, `\ipm25=\i\v` e cosi'
via (`\i...\i` = salta dopo il testo, `\v` = numero). `online` vale 0 se il sensore non e' raggiungibile o se i
valori hanno piu' di 3 letture; `eta` sono i secondi dall'ultima misura. Un valore che Amazon non fornisce manca
dalla pagina e Loxone tiene l'ultimo. Con piu' sensori nell'account: `/aria/1`, `/aria/2`... (in ordine di nome)
oppure `DEVICE` nella configurazione; `/aria.json` li mostra tutti.

Lo script `scenari/aria_amazon.py` crea l'ingresso e i comandi su una copia del programma (unita' "µg/m³",
"ppm", "%", gradi, statistica, stanza e categoria scelte) copiandoli da un ingresso HTTP gia' presente, per
esempio quello del meteo; poi `lox carica` lo porta sul Miniserver, struttura dell'app compresa.

## Installazione e configurazione

```bash
sudo raspberry/install.sh amazon-alexa
```

Configurazione in `/etc/default/amazon-loxone` (esempio in `amazon-loxone.env.example`): `HTTP_PORT` (8095),
`ALLOW_FROM` (IP ammessi: il Miniserver e 127.0.0.1), `INTERVAL_S` (300), `DEVICE`. L'accesso ad Amazon lo
fa la persona, in un terminale del Raspberry (o con un file sul Desktop che apre `ssh -t`):

```bash
/opt/claudelox-raspberry/venv/bin/python /opt/claudelox-raspberry/amazon_loxone.py accedi && sudo systemctl restart amazon-loxone
```

## Comandi

```bash
amazon_loxone.py accedi      # accesso ad Amazon (una volta sola), poi mostra i sensori trovati
amazon_loxone.py leggi       # legge i valori da Amazon e li stampa
amazon_loxone.py run         # il ponte (servizio systemd)
amazon_loxone.py esci        # cancella i dati di accesso salvati
curl http://127.0.0.1:8095/aria     # la pagina che legge il Miniserver
journalctl -u amazon-loxone -f      # una riga con i valori a ogni lettura
```

## Sicurezza e privacy

- Il ponte **legge soltanto**: non comanda nulla, ne' su Amazon ne' in casa.
- I dati di accesso ad Amazon (token, cookie) stanno in `/var/lib/amazon-loxone/accesso.json`, leggibile solo
  dall'utente del servizio: valgono come un'app Alexa collegata all'account. Per revocarli: `esci` e poi elimina
  il dispositivo "AioAmazonDevices" da Amazon > Gestisci contenuti e dispositivi.
- I messaggi di debug della libreria (che possono contenere codici di accesso) restano spenti.
- La pagina per il Miniserver risponde solo agli IP in `ALLOW_FROM`.
- Il monossido di carbonio di questo sensore non sostituisce un rilevatore certificato.

## Test

`tests/test_amazon_loxone.py`: valori dai sensori della libreria (errori, gradi Fahrenheit), pagina per Loxone
(online, eta), scelta del sensore, IP ammessi, salvataggio dei dati di accesso, accesso passo per passo con
pagine di Amazon finte (codice chiesto dopo la password e ripetibile, account senza verifica, pagina sconosciuta
descritta senza dati riservati).

## Limiti noti

- Dipende da internet e da Amazon: interfaccia non ufficiale, puo' smettere di funzionare finche' la libreria
  non si aggiorna.
- I valori arrivano con qualche minuto di ritardo (lettura ogni 5 minuti, misura del sensore gia' vecchia di 1-3).
- Gli account con approvazione dall'app o con captcha all'accesso non sono gestiti: si riprova piu' tardi.
