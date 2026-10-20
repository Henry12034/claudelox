# Servizi per il Raspberry Pi

Due piccoli servizi che collegano al Miniserver Loxone ciò che Loxone da solo non sa comandare.

| Servizio | Cosa fa | Configurazione |
|---|---|---|
| `loxone-govee` | riceve i comandi UDP di Loxone (porta 55000) e comanda via Bluetooth le luci LED **Govee** (accensione, luminosità, colore, scene) | `/etc/default/loxone-govee` |
| `hikvision-loxone` | ascolta gli eventi delle telecamere **Hikvision** (attraversamento linea, intrusione, movimento...) e attiva gli ingressi virtuali di Loxone | `/etc/hikvision-loxone/config.toml` |

## Installazione

Su un Raspberry con Raspberry Pi OS (Bookworm o Trixie), copiata questa cartella:

```bash
sudo ./install.sh
```

Poi:

1. **Luci Govee**: trova l'indirizzo della luce e scrivilo in `/etc/default/loxone-govee` (`GOVEE_ADDRESS`):
   ```bash
   /opt/claudelox-raspberry/venv/bin/python /opt/claudelox-raspberry/loxone_govee.py scan
   ```
   Prova: `python3 /opt/claudelox-raspberry/loxone_govee.py send "color:255-0-0"`.
   In Loxone serve un'uscita virtuale `/dev/udp/<ip-raspberry>/55000` con i comandi `on`, `off`,
   `brightness:<v>` (`brightness:off` spegne), `color:<v1>-<v2>-<v3>` o il nome di una scena (`Movie`,
   `Romantic`, `Snowflake`...). I comandi che arrivano insieme si uniscono: il colore nero o la luminosita' 0
   spengono solo se nel gruppo non c'e' altro che accende.
2. **Telecamere**: la procedura guidata chiede utenti e password (di Loxone e di ogni telecamera), li prova
   e salva `/etc/hikvision-loxone/config.toml` leggibile solo dal servizio, mantenendo le regole esistenti:
   ```bash
   sudo python3 /opt/claudelox-raspberry/hikvision_loxone.py configura --miniserver <ip> --telecamere <ip1>,<ip2>
   ```
   Senza regole il servizio registra soltanto gli eventi. Per scegliere le regole guarda quali eventi arrivano,
   poi aggiungile al file (esempio: `hikvision-loxone.example.toml`) e riavvia il servizio:
   ```bash
   python3 /opt/claudelox-raspberry/hikvision_loxone.py eventi Esterna 120
   sudo systemctl restart hikvision-loxone
   ```

Log: `journalctl -u loxone-govee -f` e `journalctl -u hikvision-loxone -f`.

## Note

- Le luci Govee accettano un solo collegamento Bluetooth: chiudi l'app Govee del telefono.
- Il Raspberry Pi 5 vuole un alimentatore adeguato (ufficiale da 27 W, o USB-C PD da almeno 5 V 3 A con
  cavo USB-C/USB-C): con alimentazione scarsa si spegne sotto sforzo e può rovinare la scheda.
