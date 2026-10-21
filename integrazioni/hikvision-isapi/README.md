# Hikvision ISAPI: telecamere e registratori collegati a Loxone

Stato: eventi e regole stabili; riconoscimento facciale sperimentale. Gira su: Raspberry Pi. Servizio:
`hikvision-loxone.service`.

Il servizio ascolta gli eventi delle telecamere e dei registratori (NVR) Hikvision (attraversamento linea,
intrusione, movimento, volti...) e attiva ingressi virtuali di Loxone. In piu' ha comandi per leggere e
sistemare gli apparecchi (ora, firmware, luce notturna, archivio dei volti) e un **riconoscimento facciale che
gira sul Raspberry** con le foto della telecamera, per quando quello del registratore non basta.

## Dispositivi provati

| Modello | Firmware | Note |
|---|---|---|
| NVR AcuSense DS-7608NXI-I2/S | V4.62.200 | eventi di tutte le telecamere con il numero di canale; archivio volti e confronto (motore "face_recognition"); il suo rilevatore non trova volti visti dall'alto |
| Telecamera DS-2CD2347G2H-LI (ColorVu, luce ibrida) | V5.7.13 -> V5.7.23 | linea, intrusione, movimento; foto a piena risoluzione in 0,15-0,2 s; firmware aggiornato con il comando `firmware` |

## Come funziona

- **Eventi**: `GET /ISAPI/Event/notification/alertStream` (autenticazione digest) e' un flusso multipart di
  documenti `EventNotificationAlert` (e immagini JPEG). La telecamera ripete "active" finche' l'evento dura: il
  servizio agisce una volta per evento (pausa `cooldown`). Arrivano solo gli eventi con la "Notifica al centro di
  sorveglianza" attiva (il comando `info` dice quali). Un NVR manda gli eventi di tutte le sue telecamere: nelle
  regole si sceglie il `channel`.
- **Comandi a Loxone**: `GET http://<miniserver>/dev/sps/io/<ingresso>/SET(Sw;On;Pulse)` (basic auth). Nel
  programma Loxone l'ingresso e' un ingresso virtuale di testo collegato all'ingresso API di un pulsante.
- **Foto**: `GET /ISAPI/Streaming/channels/101/picture` restituisce un JPEG a piena risoluzione (meglio dei
  fotogrammi del video, che e' molto compresso).
- **Registrazioni** (per le prove): RTSP `rtsp://<nvr>:554/Streaming/tracks/<canale>01/?starttime=AAAAMMGGThhmmssZ`;
  sui registratori provati l'ora e' quella LOCALE, nonostante la Z.
- **Configurazione**: lettura e scrittura con GET e PUT dello stesso documento XML (il servizio cambia solo i
  campi necessari): `/ISAPI/System/time` e `/time/ntpServers/1`, `/ISAPI/Image/channels/1/supplementLight`,
  `/ISAPI/Smart/FaceDetect/<canale>`, `/ISAPI/Event/triggers/<evento>`, `/ISAPI/Intelligent/...` (archivi dei
  volti `FDLib`, confronto `channels/<canale>/faceContrast`, motori `analysisEngines/<id>`).
- **Firmware**: `PUT /ISAPI/System/updateFirmware` con il file `digicap.dav`, poi riavvio
  (`PUT /ISAPI/System/reboot`) e controllo della versione. Due insidie: la libreria requests usa il tempo
  massimo "di connessione" anche mentre invia il file (con 10 s il caricamento cade: serve un tempo unico lungo);
  il portale Hikvision Europe non manda il certificato intermedio, quindi lo scaricamento funziona da un
  sistema che lo recupera da solo (es. Windows) e non con curl.

### Riconoscimento facciale: registratore o Raspberry

- **Nel registratore** (comandi `volti`): serve il motore di analisi in modalita' `face_recognition` (si cambia
  senza riavvio), il **rilevamento dei volti** acceso sul canale (`FaceDetect`, altrimenti il confronto non parte
  mai), un archivio con foto frontali e il confronto acceso. Funziona con telecamere ad altezza viso; con una
  telecamera alta e grandangolare non trova volti nemmeno guardandola.
- **Sul Raspberry** (comandi `volti-locali`, regole `type = "volto"`): foto a piena risoluzione dalla
  telecamera, volti trovati con **YuNet** e confrontati con le impronte note con **SFace** (modelli dell'OpenCV
  Zoo, licenza Apache 2.0, scaricati da `install.sh` con controllo sha256). Trova volti anche piccoli (da ~40 px)
  e di sbieco: sulla telecamera provata, montata in alto, riconosce la persona che sale verso la telecamera
  (somiglianza 0,37-0,56 contro 0,04-0,21 della nuca scambiata per volto). Il cuore e' in `volti_locali.py`, che
  si prova senza telecamera.
- **Tempi**: l'avviso di intrusione arriva dopo qualche secondo di permanenza nella zona; chi passa svelto e'
  gia' uscito. Con `continuous = true` il Raspberry analizza le foto di continuo (2-3 al secondo, circa un core
  del Raspberry Pi 5) invece di aspettare l'avviso.
- **Di notte**: con la luce della telecamera spenta (`supplementLightMode` = close) restano solo i lampioni e le
  immagini sono scure e mosse. Le modalita' sono `irLight`, `colorVuWhiteLight` e `eventIntelligence`
  (infrarossi, piu' luce bianca quando la telecamera vede una persona: immagine a colori proprio quando serve).

## Collegamento con Loxone

Per ogni segnale un ingresso virtuale di testo (es. `movimento_ingresso`) collegato all'ingresso API di un
pulsante; il comando predefinito `SET(Sw;On;Pulse)` lo preme per un impulso. Il servizio usa un utente Loxone
con accesso a quegli ingressi (meglio uno dedicato al Raspberry, con pochi permessi).

## Installazione e configurazione

```bash
sudo raspberry/install.sh hikvision-isapi
sudo python3 /opt/claudelox-raspberry/hikvision_loxone.py configura --miniserver <ip> --telecamere <ip1>,<ip2>
```

La procedura guidata chiede utenti e password (le scrive la persona, non passano dalla chat), li prova e salva
`/etc/hikvision-loxone/config.toml` leggibile solo dal servizio. Esempio di regole:
[`hikvision-loxone.example.toml`](hikvision-loxone.example.toml).

## Comandi

```bash
H=/opt/claudelox-raspberry/hikvision_loxone.py
python3 $H info                          # modelli, canali, rilevazioni attive, eventi inviati
python3 $H leggi Scale /ISAPI/Smart/LineDetection/1   # una configurazione, password nascoste
python3 $H eventi Scale 120              # eventi che arrivano, per 120 secondi
sudo python3 $H regole Scale --aggiungi '{type="linedetection", region=1, target="human", inputs=["movimento_ingresso"]}'
python3 $H regole Scale                  # elenco numerato; --togli N, --prova N si|no
python3 $H ora Scale                     # ora automatica (NTP) e fuso dell'Europa centrale
python3 $H firmware Scale digicap.dav --conferma
python3 $H volti motore|aggiungi|elenco|rilevamento|confronto|risultati NVR ...   # riconoscimento nel registratore
python3 $H volti-locali impara Mario fronte.jpg lato.jpg       # impronte dalle foto (le foto non si salvano)
python3 $H volti-locali impara-da Scale Mario                  # impronte con le foto della telecamera
python3 $H volti-locali prova Scale                            # chi vede adesso e con che somiglianza
sudo python3 $H regole Scale --aggiungi '{type="volto", continuous=true, person="Mario", min_similarity=45,
    confirmations=2, window=8, inputs=["mario_entrata"], cooldown=120, dry_run=true}'
```

Log: `journalctl -u hikvision-loxone -f`.

## Sicurezza e privacy

- Le regole che possono disinserire un antifurto o aprire una porta partono in **prova** (`dry_run`: scrivono
  solo "PROVA, avrei attivato...") e si attivano con `regole ... --prova N no` solo dopo le prove e con il
  consenso di chi abita la casa; servono `confirmations` (es. 2 foto) e una pausa (`cooldown`).
- Una telecamera da sola non distingue un volto vero da una foto stampata: tieni sempre un ritardo d'ingresso e
  il codice, e se puoi una notifica a ogni disinserimento.
- Si salvano solo le **impronte** dei volti (128 numeri per foto, in `/var/lib/hikvision-loxone/volti-noti.npz`)
  e i ritagli dei volti riconosciuti (ultimi 60 file in `/var/lib/hikvision-loxone/volti`); delle persone
  sconosciute nessuna immagine. Le password stanno nel file di configurazione (0640, root e gruppo del servizio).
- La soglia della somiglianza si sceglie con i dati: ogni 10 minuti il log riporta la somiglianza piu' alta dei
  volti non accettati.

## Test

`tests/test_hikvision.py` (configurazione, eventi XML, flusso multipart, regole e canali, procedura guidata,
scritture ISAPI, firmware, archivio volti) e `tests/test_volti_locali.py` (raffiche di foto, analisi continua,
regole "volto"), senza telecamere e senza OpenCV.

## Limiti noti

- Il riconoscimento dipende dall'inquadratura: con una telecamera alta il volto si vede bene solo salendo verso
  di lei o guardandola.
- SFace e' leggero e veloce ma non il piu' preciso: se servisse, il confronto si puo' passare ad ArcFace.
- Analisi continua: circa un core del Raspberry Pi 5 e 7 Mbit/s di foto dalla telecamera.
