# ClaudeLox: la tua casa Loxone gestita da Claude Code

Con ClaudeLox chiedi a [Claude Code](https://claude.com/claude-code), in chat, di controllare e programmare il
tuo impianto **Loxone**: Claude legge lo stato della casa, invia comandi, analizza il programma che di solito
apri in Loxone Config e, se sei d'accordo, lo modifica e lo carica sul Miniserver. Non devi ricordarti come
funziona Config.

Tutto avviene **in rete locale**, con le API ufficiali documentate da Loxone. Nessun servizio cloud.

> **Progetto non ufficiale**, non affiliato ne' approvato da Loxone Electronics GmbH. Il caricamento del
> programma riavvia la logica di casa e usa un meccanismo ricavato dal comportamento del Miniserver, non
> documentato da Loxone: fa backup, verifica e ripristino automatico, ma lo usi a tuo rischio.

*English summary at the end.*

## Cosa puoi chiedere a Claude

- "Fammi un controllo generale dell'impianto"
- "Com'e' la situazione in bagno? Temperatura, luci, presenza"
- "Spegni le luci della sala" / "Metti la scena Relax in camera"
- "Spiegami cosa fa la pagina Anticamera del programma"
- "Quando apro la porta di casa accendi le luci dell'ingresso" (Claude prepara la modifica su una copia, ti
  mostra le differenze e la carica solo dopo il tuo ok)
- "Togli l'automazione di prova che abbiamo fatto ieri"
- "Mostrami cosa succede in casa per 2 minuti"

## Come funziona

- **Lettura e comandi**: WebSocket ufficiale del Miniserver, con login cifrato (RSA + AES) e token. La
  password non viaggia mai in chiaro e non viene salvata (salvo per il caricamento diretto, vedi sotto).
- **Programma**: il toolkit scarica dal Miniserver lo stesso progetto che apre Loxone Config (XML), lo
  analizza e lo modifica sempre **su una copia**, mostrando un confronto leggibile.
- **Caricamento** (solo con il tuo consenso), in due modi:
  1. **diretto**, senza aprire Config: il programma va sul Miniserver via FTP, la logica si riavvia (circa
     10 secondi), poi il toolkit verifica che il programma attivo sia quello giusto e, se qualcosa va storto,
     ripristina da solo la versione precedente;
  2. **con Loxone Config**: Claude apre la copia in Config e (con il "computer use" di Claude) preme
     "Salva nel Miniserver", oppure lo fai tu. Serve per le modifiche che cambiano l'app (nuovi controlli,
     nomi, stanze), che il caricamento diretto rifiuta.
- **Regole di sicurezza** per Claude in `CLAUDE.md`: antifurto, porte, cancelli e finestre richiedono il tuo
  consenso esplicito per ogni singola azione; nessun caricamento ad antifurto inserito.
- **Raspberry Pi (facoltativo)**: in `raspberry/` ci sono due piccoli servizi per cio' che Loxone non comanda
  da solo: luci LED **Govee** via Bluetooth (il Miniserver manda comandi UDP) e telecamere **Hikvision** (gli
  eventi attivano ingressi virtuali di Loxone). Vedi [`raspberry/README.md`](raspberry/README.md).

## Requisiti

- Un Miniserver Loxone raggiungibile in rete locale (testato: Miniserver Gen 2, firmware 17.2).
- Python 3.10 o successivo (testato con 3.13) e Claude Code.
- Loxone Config su Windows, solo per creare l'utente dedicato e per le modifiche che cambiano l'app.
  Lettura, comandi, analisi e caricamento diretto funzionano anche da macOS e Linux.

## Installazione

1. **Scarica il progetto**

   ```bash
   git clone https://github.com/Henry12034/claudelox.git
   cd claudelox
   ```

2. **Crea l'ambiente Python**

   Windows (PowerShell):
   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
   macOS / Linux:
   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   ```

3. **Crea un utente dedicato a Claude** in Loxone Config, cosi' puoi disattivarlo quando vuoi:
   1. apri Loxone Config e collegati al Miniserver;
   2. apri la gestione utenti ("Utenti e permessi" nell'albero a sinistra, oppure "Gestione utenti" in alto);
   3. aggiungi un utente (es. `Claude`) con una password nuova e robusta. Il nome e' sensibile alle maiuscole;
   4. diritti: per leggere e comandare basta l'accesso ai controlli della casa; per **scaricare e caricare il
      programma** serve un utente **amministratore** (e' la configurazione testata);
   5. salva nel Miniserver.

4. **Configura il collegamento**: copia `.env.example` come `.env` e imposta almeno `LOXONE_HOST`
   (indirizzo IP del Miniserver) e `LOXONE_USER`. Lascia vuota la password, salvo che tu voglia il caricamento
   diretto del programma (vedi i commenti nel file).

5. **Fai il login una volta**, in un terminale nella cartella del progetto (la password la digiti tu e non
   viene salvata: resta solo un token valido per settimane, che si rinnova da solo):

   ```powershell
   .\lox.cmd login
   ```
   (su macOS/Linux o Git Bash: `./lox login`)

6. **Verifica**:

   ```powershell
   .\lox.cmd ping --ws     # il Miniserver risponde? scambio chiavi cifrato ok?
   .\lox.cmd info          # modello, firmware, permessi del token
   .\lox.cmd salute        # controllo generale: log, dispositivi esterni, stato di tutti i controlli
   ```

7. **Apri Claude Code nella cartella** e chiedi: "Fammi un controllo generale dell'impianto". Al primo avvio
   Claude crea `CLAUDE.local.md` (privato) con i dati del tuo impianto e lo aggiorna nel tempo.

Per revocare l'accesso: `lox logout`, oppure disattiva l'utente in Loxone Config.

## Comandi utili

```powershell
.\lox.cmd stato --stanza Bagno            # valori attuali di una stanza
.\lox.cmd comando "Bagno" changeTo/778    # scena "Off" (gli id delle scene: lox mostra "Bagno")
.\lox.cmd osserva --secondi 120           # cambiamenti in tempo reale
.\lox.cmd programma scarica               # scarica il programma attuale dal Miniserver
.\lox.cmd programma riepilogo             # cosa contiene il programma
.\lox.cmd carica <copia.Loxone>           # prova a secco del caricamento (aggiungi --applica --conferma)
```

L'elenco completo e le regole che Claude segue sono in `CLAUDE.md`; `.\lox.cmd --help` mostra tutti i comandi.

## Sicurezza e privacy

- Tutto il traffico resta nella tua rete. Login cifrato come nelle app ufficiali; i file (programma, log) si
  scaricano via HTTP con un hash monouso del token. L'FTP usa TLS, ma senza verificare il certificato del
  Miniserver (in rete locale non e' valido per l'indirizzo IP).
- `.env`, `.state/`, `data/` e `CLAUDE.local.md` sono privati ed esclusi da git: i programmi contengono
  l'indirizzo di casa, gli utenti e le password di alcuni dispositivi. Il toolkit li omette quando mostra il
  programma. **Non pubblicarli.**
- Il caricamento diretto richiede la password dell'utente nel `.env` (in chiaro su disco): usa un utente
  dedicato e proteggi il PC.

## Limiti noti

- Il caricamento diretto non aggiorna la struttura dell'app (`LoxAPP3.json`, che genera Loxone Config):
  vale solo per modifiche alla logica (parametri, collegamenti, blocchi non visibili nell'app). Il comando
  `carica` riconosce le altre modifiche e chiede di usare Config.
- Le modifiche fatte dall'app (timer, scene, Autopilot) restano sul Miniserver finche' Config non le integra
  nel programma; caricare un programma via FTP le cancellerebbe, quindi `carica` in quel caso si ferma.
- Testato su un solo impianto (Gen 2, firmware 17.2). Altri modelli e firmware dovrebbero funzionare per
  lettura e comandi; il caricamento diretto va provato con prudenza.

## Struttura del progetto

```
lox, lox.cmd, lox.py    avvio del toolkit (Bash / PowerShell)
claudelox/              client Miniserver (client, crypto), struttura dell'app (structure),
                        programma (loxcc, program, editor), caricamento (uploader), log, backup, Shelly, CLI
scenari/                script di modifica riutilizzabili (esempio: porta che accende le luci)
raspberry/              servizi per un Raspberry Pi: luci Govee via Bluetooth, telecamere Hikvision
tests/                  test senza Miniserver:  python -m unittest discover -s tests
CLAUDE.md               istruzioni e regole per Claude (pubbliche)
CLAUDE.local.md         dati del tuo impianto (privato, creato al primo avvio)
```

## English summary

ClaudeLox lets [Claude Code](https://claude.com/claude-code) monitor, control and reprogram a **Loxone**
smart home on the local network, using Loxone's documented WebSocket/HTTP APIs (encrypted login, tokens).
It downloads the Miniserver program (the Loxone Config project XML), analyses and edits it on a copy, shows
a readable diff, and — only with your consent — uploads it either directly (FTP + logic restart, with
backup, verification and automatic rollback) or through Loxone Config. Safety rules for the agent live in
`CLAUDE.md` (alarm, doors, gates and windows need explicit consent for every action). Optional Raspberry Pi
services in `raspberry/` bridge Govee Bluetooth LED strips and Hikvision camera events to the Miniserver.
The CLI and docs are in Italian. Unofficial project, not affiliated with Loxone; use at your own risk.

## Licenza

[MIT](LICENSE)
