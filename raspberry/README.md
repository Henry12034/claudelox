# Raspberry Pi: il ponte verso i dispositivi che Loxone non comanda da solo

Il Raspberry esegue le integrazioni della [libreria](../integrazioni/) che hanno bisogno di un computer sempre
acceso in casa (Bluetooth, analisi delle immagini, flussi di eventi). `install.sh` installa o aggiorna quelle
scelte, ognuna come servizio systemd che riparte da solo.

| Integrazione | Servizio | Configurazione |
|---|---|---|
| [govee-bluetooth](../integrazioni/govee-bluetooth/) | `loxone-govee` | `/etc/default/loxone-govee` |
| [hikvision-isapi](../integrazioni/hikvision-isapi/) | `hikvision-loxone` | `/etc/hikvision-loxone/config.toml` |
| [amazon-alexa](../integrazioni/amazon-alexa/) | `amazon-loxone` | `/etc/default/amazon-loxone` |

## Su cosa e' stato provato

Le integrazioni sono state provate su un **Raspberry Pi 5** con **Raspberry Pi OS trixie** (Debian 13, Python
3.13, OpenCV 4.10). L'ultima versione di `install.sh` e' stata provata solo a secco: su un Raspberry appena
installato non e' ancora stata eseguita, quindi se si ferma apri una segnalazione con il messaggio di errore.

Su Raspberry Pi OS Bookworm (Debian 12, Python 3.11) ci si aspetta che funzionino il ponte Govee e gli eventi
delle telecamere Hikvision, che non usano niente di recente. Li' **non sono stati provati**, e potrebbero non
andare, il riconoscimento facciale sul Raspberry (vuole un OpenCV recente) e il ponte Amazon (la sua libreria
vuole un Python recente).

## Preparare il Raspberry

1. **Sistema**: Raspberry Pi OS trixie (quello provato; per Bookworm vedi sopra) scritto con Raspberry Pi
   Imager, impostando nome, utente (di solito `pi`), fuso orario, Wi-Fi e una **chiave SSH** (le password le
   scrive la persona in Imager).
2. **Alimentazione**: il Raspberry Pi 5 vuole l'alimentatore ufficiale da 27 W, o un USB-C PD da almeno
   5 V 3 A con cavo USB-C/USB-C. Con alimentazione scarsa si spegne sotto sforzo e puo' rovinare la scheda; in
   attesa dell'alimentatore giusto aiuta `arm_freq=1500` in `/boot/firmware/config.txt`. Controllo:
   `vcgencmd get_throttled` deve dire `0x0`.
3. **Rete**: meglio il cavo e un indirizzo fisso (quello che il programma Loxone usa per mandargli i comandi),
   riservato anche nel router.
4. **Installazione**, dalla cartella del repository copiata sul Raspberry:
   ```bash
   sudo bash raspberry/install.sh                      # tutte
   sudo bash raspberry/install.sh govee-bluetooth      # solo alcune
   ```
   Poi la configurazione di ciascuna, descritta nel suo README. Lo script si puo' rilanciare quando si vuole:
   aggiorna programmi e servizi e non tocca le configurazioni.

## Cosa fa install.sh

- **Utente dei servizi**: i servizi non girano come root ma come l'utente che ha lanciato `sudo` (senza `sudo`:
  `pi`). Per sceglierne un altro: `sudo SERVICE_USER=<utente> bash raspberry/install.sh`. Lo script scrive
  quell'utente nelle unita' systemd che installa e gli da' solo le cartelle dei dati; i comandi da lanciare a mano
  (ricerca delle luci, accesso ad Amazon...) vanno eseguiti come lui.
- **Pacchetti**: aggiorna l'elenco dei pacchetti (`apt-get update`) e installa quelli che mancano, senza
  aggiornare quelli gia' installati; le librerie Python che non sono nei pacchetti del sistema
  ([bleak](https://github.com/hbldh/bleak), licenza MIT;
  [aioamazondevices](https://github.com/chemelli74/aioamazondevices), licenza Apache 2.0) vanno in un ambiente
  Python a parte (`venv/`), installate senza i privilegi di root.
- **Ordine**: di ogni integrazione installa prima programma e servizio, poi le parti facoltative. Per le
  telecamere, OpenCV e i modelli del riconoscimento facciale (OpenCV Zoo: YuNet con licenza MIT, SFace con licenza
  Apache 2.0, accettati solo se l'sha256 coincide) arrivano per ultimi: se lo scaricamento non riesce lo script
  dice cosa e' mancato e come riprovare, e il ponte degli eventi funziona lo stesso.
- **Luci Govee**: finche' in `/etc/default/loxone-govee` manca `GOVEE_ADDRESS` il servizio non viene abilitato ne'
  avviato; lo script scrive i passi da fare.
- **Protezioni**: le tre unita' systemd girano senza privilegi e con `NoNewPrivileges`, `PrivateTmp`,
  `ProtectSystem=full`, `ProtectHome`, `ProtectKernelTunables`, `ProtectKernelModules`, `ProtectControlGroups`,
  `RestrictSUIDSGID`, `LockPersonality`.

## Dove finiscono i file

- programmi: `/opt/claudelox-raspberry` (con `venv/` per Govee e Amazon e `modelli/` per il riconoscimento
  facciale), **di root**: l'utente dei servizi li legge e li esegue, non li puo' cambiare;
- configurazioni: `/etc/default/loxone-govee`, `/etc/hikvision-loxone/config.toml` (password: 0640, root e
  gruppo dell'utente dei servizi; la procedura guidata ne tiene una copia in `config.toml.bak`),
  `/etc/default/amazon-loxone`;
- dati, le sole cartelle che appartengono all'utente dei servizi: `/var/lib/loxone-govee` (catalogo delle
  scene), `/var/lib/hikvision-loxone` (impronte dei volti noti, foto dei volti riconosciuti),
  `/var/lib/amazon-loxone` (dati di accesso ad Amazon, leggibili solo da lui).

Log: `journalctl -u loxone-govee -f`, `journalctl -u hikvision-loxone -f`, `journalctl -u amazon-loxone -f`.
