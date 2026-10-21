# Integrazioni: libreria aperta di collegamenti

Ogni cartella collega Loxone (tramite ClaudeLox) a un dispositivo o a un protocollo che il Miniserver non sa
usare da solo: luci Bluetooth, telecamere, riconoscimento facciale... Sono nate in case vere, scritte con Claude,
provate dal vivo e documentate in modo che altri le possano riusare. **Chi usa ClaudeLox puo' aggiungere le
proprie**: piu' case, piu' dispositivi, una libreria sempre piu' grande.

| Integrazione | Dispositivi provati | Protocollo | Cosa fa | Gira su | Stato |
|---|---|---|---|---|---|
| [govee-bluetooth](govee-bluetooth/) | Govee H6126 (firmware 2.06.1) | Bluetooth LE (GATT) | Loxone comanda le strisce LED: accensione, luminosita', colore, scene; piu' strisce insieme | Raspberry Pi | stabile |
| [hikvision-isapi](hikvision-isapi/) | registratore DS-7608NXI-I2/S (V4.62), telecamera DS-2CD2347G2H-LI (V5.7.13-V5.7.23) | HTTP ISAPI (digest), RTSP | eventi delle telecamere -> ingressi virtuali di Loxone; ora, firmware, luce notturna, archivio volti; **riconoscimento facciale sul Raspberry** (OpenCV) | Raspberry Pi | eventi: stabile; volti: sperimentale |
| Shelly (nel toolkit) | Shelly Gen1 e Gen2 | HTTP locale | cerca gli Shelly, assegna l'IP fisso previsto dal programma, li fa lampeggiare: `lox shelly` ([codice](../claudelox/shelly.py)) | PC | stabile |

Per installare quelle che girano sul Raspberry: [`raspberry/`](../raspberry/).

## Aggiungere un'integrazione

Basta chiedere a Claude, a lavoro finito: *"aggiungi alla libreria l'integrazione che abbiamo fatto"*. Le
regole, che Claude segue (vedi `CLAUDE.md`) e che valgono anche per chi scrive a mano:

1. **Una cartella** `integrazioni/<marca-protocollo>/` (minuscole, trattini), con il README copiato da
   [`_modello/`](_modello/README.md) e compilato: dispositivi provati con il firmware, come funziona il protocollo
   (quello che si e' scoperto: indirizzi, pacchetti, stranezze), come si collega a Loxone, comandi, limiti.
2. **Niente dati di casa**: mai IP, MAC, numeri di serie, nomi di persone o stanze, password o chiavi. Negli
   esempi usa `10.0.0.x`, `AA:BB:CC:DD:EE:FF`, `Mario`. I dati veri stanno nei file di configurazione sul
   dispositivo e in `CLAUDE.local.md`, che non si pubblica.
3. **Configurazione fuori dal codice**: un file in `/etc/...` o variabili d'ambiente, con un esempio
   (`*.example`). Le password le scrive la persona, con una procedura guidata, e non passano dalla chat.
4. **Test senza il dispositivo** in `tests/test_<nome>.py`: pacchetti, risposte, decisioni. Devono passare con
   `python -m unittest discover -s tests`.
5. **Sicurezza**: se l'integrazione puo' aprire porte, disinserire allarmi o comandare cancelli, deve avere una
   modalita' di prova che scrive solo nel log (`dry_run`), conferme ripetute, una pausa tra due comandi, e va
   attivata solo dopo le prove e con il consenso esplicito di chi abita la casa.
6. **Servizio**: se gira sul Raspberry, un file `.service` di systemd nella cartella e una funzione in
   `raspberry/install.sh`.
7. **Licenza**: il codice e' MIT come il resto del progetto; modelli o librerie di terzi vanno scaricati
   dall'origine ufficiale (con controllo sha256) e citati con la loro licenza.

Le integrazioni della tabella sono il punto di partenza: copiane lo stile (un file Python per il ponte, uno per
la logica che si puo' provare senza dispositivo, un README che spiega tutto in italiano).
