# Raspberry Pi: il ponte verso i dispositivi che Loxone non comanda da solo

Il Raspberry esegue le integrazioni della [libreria](../integrazioni/) che hanno bisogno di un computer sempre
acceso in casa (Bluetooth, analisi delle immagini, flussi di eventi). `install.sh` installa o aggiorna quelle
scelte, ognuna come servizio systemd che riparte da solo.

| Integrazione | Servizio | Configurazione |
|---|---|---|
| [govee-bluetooth](../integrazioni/govee-bluetooth/) | `loxone-govee` | `/etc/default/loxone-govee` |
| [hikvision-isapi](../integrazioni/hikvision-isapi/) | `hikvision-loxone` | `/etc/hikvision-loxone/config.toml` |

## Preparare il Raspberry

1. **Sistema**: Raspberry Pi OS (Bookworm o Trixie) scritto con Raspberry Pi Imager, impostando nome, utente
   `pi`, fuso orario, Wi-Fi e una **chiave SSH** (le password le scrive la persona in Imager).
2. **Alimentazione**: il Raspberry Pi 5 vuole l'alimentatore ufficiale da 27 W, o un USB-C PD da almeno
   5 V 3 A con cavo USB-C/USB-C. Con alimentazione scarsa si spegne sotto sforzo e puo' rovinare la scheda; in
   attesa dell'alimentatore giusto aiuta `arm_freq=1500` in `/boot/firmware/config.txt`. Controllo:
   `vcgencmd get_throttled` deve dire `0x0`.
3. **Rete**: meglio il cavo e un indirizzo fisso (quello che il programma Loxone usa per mandargli i comandi),
   riservato anche nel router.
4. **Installazione**, dalla cartella del repository copiata sul Raspberry:
   ```bash
   sudo raspberry/install.sh                      # tutte
   sudo raspberry/install.sh govee-bluetooth      # solo alcune
   ```
   Poi la configurazione di ciascuna, descritta nel suo README.

## Dove finiscono i file

- programmi: `/opt/claudelox-raspberry` (con `venv/` per il Bluetooth e `modelli/` per il riconoscimento facciale);
- configurazioni: `/etc/default/loxone-govee`, `/etc/hikvision-loxone/config.toml` (password: 0640, root e
  gruppo `pi`);
- dati: `/var/lib/hikvision-loxone` (impronte dei volti, ritagli dei volti riconosciuti, copie di impostazioni).

Log: `journalctl -u loxone-govee -f` e `journalctl -u hikvision-loxone -f`.
