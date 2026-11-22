# Hikvision ISAPI: telecamere e registratori collegati a Loxone

Stato: eventi e regole stabili; riconoscimento facciale sperimentale. Gira su: Raspberry Pi. Servizio:
`hikvision-loxone.service`. Python: 3.11 o successivo (la configurazione si legge con `tomllib`); il
riconoscimento facciale sul Raspberry vuole anche un OpenCV recente (provato con 4.10 e Python 3.13).

Il servizio ascolta gli eventi delle telecamere e dei registratori (NVR) Hikvision (attraversamento linea,
intrusione, movimento, volti...) e attiva ingressi virtuali di Loxone. In piu' ha comandi per leggere e
sistemare gli apparecchi (ora, firmware, archivio dei volti) e un **riconoscimento facciale che
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
- **Comandi a Loxone**: `GET http://<miniserver>/dev/sps/io/<ingresso>/SET(Sw;On;Pulse)` con autenticazione
  HTTP Basic: utente e password viaggiano **in chiaro** nella rete di casa a ogni comando (vedi "Collegamento con
  Loxone"). Nel programma Loxone l'ingresso e' un ingresso virtuale di testo collegato all'ingresso API di un
  pulsante.
- **Foto**: `GET /ISAPI/Streaming/channels/101/picture` restituisce un JPEG a piena risoluzione (meglio dei
  fotogrammi del video, che e' molto compresso).
- **Registrazioni** (per le prove): RTSP `rtsp://<nvr>:554/Streaming/tracks/<canale>01/?starttime=AAAAMMGGThhmmssZ`;
  sui registratori provati l'ora e' quella LOCALE, nonostante la Z.
- **Configurazione**: lettura e scrittura con GET e PUT dello stesso documento XML (i comandi cambiano solo i
  campi necessari): `/ISAPI/System/time` e `/time/ntpServers/1`, `/ISAPI/Smart/FaceDetect/<canale>`,
  `/ISAPI/Event/triggers/<evento>`, `/ISAPI/Intelligent/...` (archivi dei volti `FDLib`, confronto
  `channels/<canale>/faceContrast`, motori `analysisEngines/<id>`). La luce della telecamera sta in
  `/ISAPI/Image/channels/1/supplementLight`: `leggi` la mostra, ma nessun comando la cambia (si imposta dalla
  pagina web della telecamera).
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
  telecamera, volti trovati con **YuNet** e confrontati con le impronte note con **SFace** (modelli
  dell'[OpenCV Zoo](https://github.com/opencv/opencv_zoo): YuNet con licenza MIT, SFace con licenza Apache 2.0;
  `install.sh` li scarica dall'origine e li accetta solo se l'sha256 coincide). Trova volti anche piccoli (da
  ~40 px) e di sbieco: sulla telecamera provata, montata in alto, riconosce la persona che sale verso la
  telecamera (somiglianza 0,37-0,56 contro 0,04-0,21 della nuca scambiata per volto). Il cuore e' in
  `volti_locali.py`, che si prova senza telecamera.
- **Tempi**: l'avviso di intrusione arriva dopo qualche secondo di permanenza nella zona; chi passa svelto e'
  gia' uscito. Con `continuous = true` il Raspberry analizza le foto di continuo (2-3 al secondo, circa un core
  del Raspberry Pi 5) invece di aspettare l'avviso.
- **Di notte**: con la luce della telecamera spenta (`supplementLightMode` = close) restano solo i lampioni e le
  immagini sono scure e mosse. Le modalita' sono `irLight`, `colorVuWhiteLight` e `eventIntelligence`
  (infrarossi, piu' luce bianca quando la telecamera vede una persona: immagine a colori proprio quando serve).

## Collegamento con Loxone

Per ogni segnale un ingresso virtuale di testo (es. `movimento_ingresso`) collegato all'ingresso API di un
pulsante; il comando predefinito `SET(Sw;On;Pulse)` lo preme per un impulso.

Il servizio manda ogni comando con il nome e la password di un utente Loxone, in chiaro nella rete di casa (HTTP
Basic): chi puo' ascoltare la rete puo' leggerli. Per questo serve un **utente dedicato al Raspberry, con i
permessi minimi** (quelli che bastano per quegli ingressi virtuali): **mai un amministratore** e mai l'utente di
una persona.

## Installazione e configurazione

```bash
sudo bash raspberry/install.sh hikvision-isapi
sudo python3 /opt/claudelox-raspberry/hikvision_loxone.py configura --miniserver <ip> --telecamere <ip1>,<ip2>
```

La procedura guidata chiede utenti e password (le scrive la persona, non passano dalla chat), li prova e salva
`/etc/hikvision-loxone/config.toml`, leggibile solo da root e dal gruppo dell'utente del servizio. Si puo'
rilanciare quando si vuole: una telecamera gia' configurata che viene saltata (password vuota), che non risponde
o che non supera la verifica **resta com'era**, con utente, password e regole di prima; del file di prima resta
una copia in `config.toml.bak`, con gli stessi permessi. Per togliere una telecamera si cancella il suo blocco
`[[cameras]]` dal file. Il gruppo del file e' quello che ha gia', o quello della cartella `/etc/hikvision-loxone`
preparata da `install.sh` (si puo' imporre con la variabile `HIKVISION_LOXONE_GROUP`). Esempio di regole:
[`hikvision-loxone.example.toml`](hikvision-loxone.example.toml).

Chi aggiorna da una versione precedente: una regola con una chiave sbagliata, o una regola dei volti senza
`min_similarity`, ora ferma il servizio con una riga nel log che dice cosa correggere (prima veniva accettata in
silenzio); una regola dei volti senza `dry_run` ora resta in prova.

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
# una regola e' una tabella TOML in linea, tutta su UNA riga (le tabelle in linea non possono andare a capo):
sudo python3 $H regole Scale --aggiungi '{type="volto", continuous=true, person="Mario", min_similarity=45, confirmations=2, window=8, inputs=["mario_entrata"], cooldown=120}'
sudo python3 $H regole Scale --prova 2 no                      # dopo le prove: la regola 2 (vedi l'elenco) comanda Loxone
```

Log: `journalctl -u hikvision-loxone -f`.

## Sicurezza e privacy

- Le regole del riconoscimento facciale (quelle con `person` e quelle `type = "volto"`) possono disinserire un
  antifurto o aprire una porta: **partono sempre in prova**, anche se `dry_run` non c'e' scritto (scrivono solo
  "PROVA, avrei attivato..."). Comandano Loxone solo con `dry_run = false`, cioe' dopo `regole ... --prova N no`:
  da fare solo dopo le prove e con il consenso di chi abita la casa. Vogliono `min_similarity` scritto e
  maggiore di 0 (per le regole "volto" almeno 36.3, la soglia di SFace per "stessa persona": per un antifurto
  meglio piu' alta) e, per le regole "volto", `person`; servono anche `confirmations` (es. 2 foto) e una pausa
  (`cooldown`). Le altre regole (linea, intrusione, movimento...) sono attive appena scritte, come sempre.
- Una chiave sbagliata in una regola (es. `min_similarty`, `dry-run`) e' un errore che dice quale chiave e'
  sbagliata e quali sono quelle valide: non viene ignorata in silenzio.
- Una telecamera da sola non distingue un volto vero da una foto stampata: tieni sempre un ritardo d'ingresso e
  il codice, e se puoi una notifica a ogni disinserimento.
- Si salvano solo le **impronte** dei volti noti (128 numeri per foto, in
  `/var/lib/hikvision-loxone/volti-noti.npz`) e, delle persone **riconosciute**, il ritaglio del volto
  (riconoscimento sul Raspberry) oppure documento e foto dell'evento (riconoscimento del registratore): gli
  ultimi 60 file in `/var/lib/hikvision-loxone/volti`. Delle persone sconosciute non si salva niente.
- Solo per diagnostica (es. per capire perche' il registratore non riconosce nessuno), `save_unknown_faces = true`
  in cima al file di configurazione fa tenere documento e foto di **tutti** gli eventi dei volti, persone
  sconosciute comprese; il servizio lo scrive nel log a ogni avvio. Va tolto finite le prove.
- Le password stanno nel file di configurazione e nella sua copia `config.toml.bak` (0640, root e gruppo
  dell'utente del servizio). La password dell'utente Loxone viaggia in chiaro nella rete di casa: utente dedicato
  con i permessi minimi, mai un amministratore (vedi "Collegamento con Loxone").
- `leggi` non stampa i valori dei campi il cui nome contiene `password`, `passwd`, `pwd`, `key`, `secret`, `token`,
  `community`, `psk`, `pin` o `verificationCode`, e rifiuta i percorsi con utenti e dati di accesso.
- La soglia della somiglianza si sceglie con i dati: ogni 10 minuti il log riporta la somiglianza piu' alta dei
  volti non accettati.

## Test

`tests/test_hikvision.py` (configurazione, chiavi sconosciute, regole dei volti in prova, salvataggio e rilettura,
eventi XML con uno o piu' candidati, flusso multipart, regole e canali, cosa si salva degli eventi dei volti,
procedura guidata, valori nascosti da `leggi`, scritture ISAPI, firmware, archivio volti) e
`tests/test_volti_locali.py` (raffiche di foto, analisi continua, regole "volto"), senza telecamere e senza
OpenCV. Con Python 3.10 i due file si saltano da soli (serve `tomllib`).

## Limiti noti

- Il riconoscimento dipende dall'inquadratura: con una telecamera alta il volto si vede bene solo salendo verso
  di lei o guardandola.
- SFace e' leggero e veloce ma non il piu' preciso: se servisse, il confronto si puo' passare ad ArcFace.
- Analisi continua: circa un core del Raspberry Pi 5 e 7 Mbit/s di foto dalla telecamera.
- Le regole "volto" ignorano `channel` e prendono sempre la foto dal canale 101 dell'apparecchio su cui sono
  scritte: vanno messe sulla telecamera, non sul registratore.
- Con piu' regole "volto" a raffica (senza `continuous`) sulla stessa telecamera parte solo la prima: la raffica
  di foto e' una alla volta.
- I comandi a Loxone sono HTTP con autenticazione Basic: la password dell'utente Loxone passa in chiaro nella
  rete di casa. Deve essere un utente dedicato con i permessi minimi, mai un amministratore.
- Un evento del registratore con piu' candidati scritti in un modo che non lascia capire a chi appartiene la
  somiglianza resta senza persona: nessuna regola scatta (si vede con `save_unknown_faces`).
- Provato su Raspberry Pi 5 con Raspberry Pi OS trixie (Python 3.13, OpenCV 4.10). Su Bookworm (Python 3.11) gli
  eventi dovrebbero funzionare; il riconoscimento facciale sul Raspberry li' non e' stato provato.
