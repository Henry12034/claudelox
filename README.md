# ClaudeLox: la tua casa Loxone gestita da Claude Code

Con ClaudeLox chiedi a [Claude Code](https://claude.com/claude-code), in chat e con parole tue, di controllare e
programmare il tuo impianto **Loxone**: Claude legge lo stato della casa, invia comandi, ti spiega il programma
che di solito apri in Loxone Config e, se sei d'accordo, lo modifica, lo prova, lo carica sul Miniserver e
controlla che sia partito. Non devi ricordarti come funziona Config.

Tutto avviene **in rete locale**, con le API documentate da Loxone. Nessun servizio cloud in mezzo tra il
toolkit e il Miniserver.

> **Progetto non ufficiale**, non affiliato ne' approvato da Loxone Electronics GmbH, nato in una casa vera e
> provato su un solo impianto. Il caricamento del programma riavvia la logica di casa e usa un meccanismo ricavato
> dal comportamento del Miniserver, non documentato da Loxone: fa backup, verifica e ripristino automatico, ma
> **lo usi a tuo rischio**. Leggi "Sicurezza e privacy" e "Limiti noti" prima di usarlo su un impianto importante.

*English summary at the end.*

## Cosa sa fare

| | |
|---|---|
| **Capire** | controllo generale dell'impianto, stato di stanze e dispositivi, analisi del log di sistema, dispositivi di rete che non rispondono, spiegazione in parole semplici di una pagina o di un'automazione del programma |
| **Comandare** | luci, scene, interruttori, valori: ogni comando riporta lo stato prima e dopo; antifurto, porte e cancelli solo con il tuo consenso esplicito |
| **Programmare** | scarica il programma dal Miniserver, lo modifica **su una copia** (parametri, tempi, blocchi e collegamenti nuovi, automazioni da togliere, stanze e nomi nell'app), ti mostra un confronto leggibile |
| **Provare e caricare** | prova a secco senza toccare la casa, poi caricamento diretto: backup, controlli, upload, riavvio della logica (circa 10 secondi), verifica, **ripristino automatico** se qualcosa va storto |
| **Versioni e backup** | ogni versione scaricata e ogni copia modificata restano in `data/programmi/`: si possono confrontare tra loro e ricaricare per tornare indietro; legge anche i backup di Loxone Config sul PC |
| **Dispositivi esterni** | trova gli Shelly, assegna gli indirizzi previsti dal programma, li ricollega al Wi-Fi; [libreria aperta](integrazioni/) di collegamenti con Govee, Hikvision, Amazon |

## Cosa puoi chiedere a Claude

- "Fammi un controllo generale dell'impianto"
- "Com'e' la situazione in bagno? Temperatura, luci, presenza"
- "Spegni le luci della sala" / "Metti la scena Relax in camera"
- "Spiegami cosa fa la pagina Anticamera del programma"
- "Quando apro la porta di casa accendi le luci dell'ingresso" (Claude prepara la modifica su una copia, ti
  mostra le differenze e la carica solo dopo il tuo ok)
- "Togli l'automazione di prova che abbiamo fatto ieri"
- "Cosa e' cambiato nel programma rispetto alla versione della settimana scorsa?"
- "Mostrami cosa succede in casa per 2 minuti"

## Come funziona

- **Lettura e comandi**: WebSocket del Miniserver, con login cifrato (RSA + AES) e token. La password non
  viaggia mai in chiaro e non viene salvata (salvo per il caricamento diretto, vedi sotto).
- **Programma**: il toolkit scarica dal Miniserver lo stesso progetto che apre Loxone Config (XML), lo
  analizza e lo modifica sempre **su una copia**, mostrando un confronto leggibile.
- **Caricamento** (solo con il tuo consenso), in due modi:
  1. **diretto**, senza aprire Config: il programma va sul Miniserver via FTP insieme alla struttura dell'app e
     ai permessi aggiornati (controlli nuovi, eliminati, spostati o rinominati compaiono subito nell'app), la
     logica si riavvia (circa 10 secondi), poi il toolkit verifica che programma e app siano quelli giusti e, se
     qualcosa va storto, ripristina da solo la versione precedente;
  2. **con Loxone Config**, per i casi che il caricamento diretto non sa fare (stanze o categorie nuove,
     controlli composti come intercom e antifurto): Claude apre la copia in Config e tu premi "Salva nel
     Miniserver".
- **Regole per Claude** in [`CLAUDE.md`](CLAUDE.md): cosa puo' fare da solo, cosa deve chiederti, cosa non deve
  leggere. Claude Code le carica da solo quando apri la cartella.
- **Integrazioni** (libreria aperta, cartella [`integrazioni/`](integrazioni/)): collegamenti con dispositivi e
  protocolli che Loxone non comanda da solo, nati in case vere e provati dal vivo: luci LED **Govee** via
  Bluetooth, telecamere e registratori **Hikvision** (eventi, firmware, riconoscimento facciale), sensore della
  qualita' dell'aria **Amazon** (dal cloud Alexa), Shelly. Quelle che girano su un Raspberry Pi si installano con
  [`raspberry/install.sh`](raspberry/README.md).

## Requisiti

- Un **Miniserver Loxone** raggiungibile in rete locale. Provato su un Miniserver Gen 2 con firmware 17.2 e 17.3
  (vedi "Limiti noti" per cosa e' stato provato su quale versione).
- **Claude Code** (app desktop, terminale o estensione): serve un abbonamento Claude o una chiave API.
- **Python 3.10 o successivo** (provato con 3.13) e **git**.
- **Loxone Config** su Windows, solo per creare l'utente dedicato e per le poche modifiche dell'app che il
  caricamento diretto non sa fare. Il toolkit gira anche su Linux; su macOS dovrebbe, ma non e' ancora stato provato.
- Solo per il **caricamento diretto**: un utente amministratore e il **server FTP del Miniserver attivo**. Dal
  firmware 16.1 e' spento per impostazione predefinita
  ([documentazione Loxone](https://www.loxone.com/enen/kb/initial-setup-miniserver/)): si attiva in Loxone Config,
  nelle proprieta' del Miniserver, scegliendo "Enabled - TLS only" (solo cifrato). Senza FTP tutto il resto
  funziona e il programma si carica con Config.

## Installazione

Se non hai mai usato un terminale: installa Claude Code, apri questa cartella e scrivi *"aiutami a installare
ClaudeLox"*. Claude ti guida nei passi qui sotto (la password la digiti sempre tu, mai in chat).

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
      programma** serve un utente **amministratore** (e' la configurazione provata);
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
   Claude crea `CLAUDE.local.md` (privato) con i dati del tuo impianto e lo aggiorna nel tempo: e' la sua
   memoria della tua casa.

Per revocare l'accesso: `lox logout`, oppure disattiva l'utente in Loxone Config.

### Un buon modo di cominciare

1. Solo lettura: "fammi un controllo generale", "spiegami la pagina ..." (nessun rischio).
2. Un comando innocuo: "accendi e spegni la luce dello studio".
3. Una modifica piccola **in prova**: "porta a 6 minuti lo spegnimento automatico della luce esterna" e guarda il
   confronto che Claude ti mostra, senza caricare.
4. Il primo caricamento quando sei in casa, con l'antifurto disinserito, tenendo a portata di mano Loxone Config
   e l'ultimo backup.

## Comandi utili

```powershell
.\lox.cmd stato --stanza Bagno            # valori attuali di una stanza
.\lox.cmd comando "Bagno" changeTo/778    # scena "Off" (gli id delle scene: lox mostra "Bagno")
.\lox.cmd osserva --secondi 120           # cambiamenti in tempo reale
.\lox.cmd programma scarica               # scarica il programma attuale dal Miniserver
.\lox.cmd programma riepilogo             # cosa contiene il programma
.\lox.cmd programma confronta <nuovo> --file <vecchio>   # differenze tra due versioni
.\lox.cmd carica <copia.Loxone>           # prova a secco del caricamento (aggiungi --applica --conferma)
```

L'elenco completo e le regole che Claude segue sono in `CLAUDE.md`; `.\lox.cmd --help` mostra tutti i comandi.
Per tornare a una versione precedente: `lox carica data/programmi/<versione>.zip` (prima in prova). Una versione
salvata prima di un aggiornamento del firmware e' in un formato diverso: `carica` lo riconosce e in quel caso
indica Loxone Config, che la converte.

## Sicurezza e privacy

Cosa protegge la casa:

- **Consenso**: Claude esegue da solo solo le azioni che gli chiedi in modo esplicito. Per antifurto, intercom,
  porte, cancelli e finestre serve il tuo consenso per ogni singola azione, e il toolkit rifiuta il comando senza
  `--conferma`. Vale anche per i loro sotto-controlli (l'apriporta di un intercom), per i controlli che dal nome
  sembrano aprire o sbloccare qualcosa ("Apri cancello", "Serratura", "Sirena"...) e per quelli che elenchi tu in
  `LOXONE_PROTETTI` nel file `.env`.
- **Niente a sorpresa sul programma**: ogni modifica nasce su una copia; `carica` e' una prova a secco finche' non
  aggiungi `--applica --conferma`; prima di caricare fa il backup della versione attiva, mostra le differenze e si
  ferma se l'antifurto e' inserito, se la modifica tocca l'app in un modo che non sa riportare o se andrebbero
  perse modifiche fatte dall'app (atmosfere, orari, regole).
- **Verifica e ripristino**: dopo il riavvio controlla che il programma attivo sia quello caricato e che il log
  non segnali errori o il programma di emergenza; altrimenti ricarica da solo la versione precedente.

Cosa resta privato:

- Tutto il traffico del toolkit resta nella tua rete. Login cifrato come nelle app ufficiali; i file (programma,
  log) si scaricano via HTTP con un hash monouso del token. L'FTP usa TLS, ma senza verificare il certificato del
  Miniserver (in rete locale non e' valido per l'indirizzo IP). Comandi e stati viaggiano in HTTP nella rete di
  casa, come previsto da Loxone per i collegamenti locali.
- `.env`, `.state/`, `data/` e `CLAUDE.local.md` sono privati ed esclusi da git: i programmi contengono
  l'indirizzo di casa, gli utenti e le password di alcuni dispositivi. Il toolkit li omette quando mostra il
  programma. **Non pubblicarli** (nemmeno in una segnalazione di errore).
- Il caricamento diretto richiede la password dell'utente nel `.env` (in chiaro su disco): usa un utente
  dedicato e proteggi il PC. Il token salvato vale settimane: su un computer condiviso fai `lox logout`.
- `lox shelly wifi` manda la password del Wi-Fi allo Shelly nella sua rete di configurazione, aperta e senza
  cifratura, come fa l'app Shelly: usala solo mentre stai riconfigurando i tuoi dispositivi.
- Quello che Claude legge (stati, nomi di stanze, pezzi di programma) passa dal servizio di Anthropic come ogni
  conversazione con Claude Code: e' il motivo per cui il toolkit nasconde utenti, password e indirizzi.

Un assistente puo' sbagliare: leggi il confronto prima di dare l'ok, soprattutto se la modifica riguarda
sicurezza, riscaldamento o carichi importanti.

## Limiti noti

- **Provato su un solo impianto** (Miniserver Gen 2). Lettura, comandi, analisi, scaricamento e caricamento diretto
  del programma: provati con i firmware 17.2 e 17.3, compreso il ritorno a una versione precedente. Il ripristino
  automatico dopo un errore e' scattato davvero solo sul 17.2. Su altri modelli e firmware va provato con prudenza:
  comincia con un parametro di un blocco che non comanda nulla.
- Ogni caricamento riavvia la logica per qualche secondo: un controllo luci senza atmosfera attiva puo' ripartire
  mostrando nell'app la scena "Tanta luce", con la stessa luminosita'.
- Una cartella del toolkit per ogni Miniserver: i programmi scaricati in `data/` non distinguono gli impianti.
- Il caricamento diretto aggiorna la struttura dell'app (`LoxAPP3.json`) e i permessi (`permissions.bin`), che di
  solito genera Loxone Config, copiando controlli dello stesso tipo gia' presenti: stanze o categorie nuove,
  controlli composti diversi dalle luci (intercom, antifurto) e tipi di controllo che l'app non ha ancora restano
  a Config. Il comando `carica` riconosce questi casi e si ferma.
- Le modifiche fatte dall'app (atmosfere, orari, regole) restano sul Miniserver, fuori dal programma, finche'
  Config non le integra; caricare un programma via FTP le cancellerebbe, quindi `carica` in quel caso si ferma.
  I telefoni registrati per le notifiche push dopo l'ultimo salvataggio si registrano di nuovo quando riapri l'app.
- Dopo un aggiornamento del firmware il Miniserver rigenera da solo programma e struttura dell'app: i controlli
  nascosti dal toolkit ma ancora collegati a un controllo luci o a una regola possono ricomparire nell'app.
- Un aggiornamento del firmware puo' cambiare cio' su cui il toolkit si appoggia (e' successo con il 17.3):
  dopo ogni aggiornamento riparti da `lox ping`, `lox programma scarica` e da una prova a secco.

## Problemi frequenti

| Messaggio | Cosa fare |
|---|---|
| `ERRORE di connessione` | controlla `LOXONE_HOST` nel `.env` e che il computer sia nella stessa rete del Miniserver (`lox ping`) |
| `login rifiutato ... (codice 401)` | nome utente (maiuscole comprese) o password sbagliati; dopo troppi tentativi il Miniserver blocca gli accessi per qualche minuto |
| `nessun token valido salvato` | esegui `lox login` in un terminale |
| `l'utente non ha i permessi necessari` (403) | per programma e log serve un utente amministratore |
| `il server FTP del Miniserver non risponde` | attiva l'FTP (solo TLS) nelle proprieta' del Miniserver in Loxone Config, oppure carica con Config |
| `per le operazioni admin serve la password` | il caricamento diretto vuole `LOXONE_PASSWORD` nel `.env` |
| `nessun programma trovato sul Miniserver` | l'utente non e' amministratore, oppure il firmware e' cambiato: apri una segnalazione con la versione (`lox ping`) |
| il caricamento "va caricato con Loxone Config" | la modifica tocca l'app in un modo che il toolkit non sa riportare: `lox programma apri <file>` e "Salva nel Miniserver" |

## Integrazioni e contributi

La cartella [`integrazioni/`](integrazioni/) e' una libreria aperta: ogni integrazione ha codice, servizio,
esempio di configurazione, test senza dispositivo e un README con modelli e firmware provati e con quello che si
e' scoperto sul protocollo. Chi ha dispositivi simili puo' riusarle; chi ne collega di nuovi puo' aggiungerle (a
Claude basta chiedere *"aggiungi alla libreria l'integrazione che abbiamo fatto"*, le regole sono in
[`integrazioni/README.md`](integrazioni/README.md)).

Sono benvenuti:

- **prove su altri impianti**: modello di Miniserver, firmware, cosa funziona e cosa no (apri una segnalazione);
- **integrazioni nuove** e modelli in piu' per quelle esistenti;
- **scenari** riutilizzabili in [`scenari/`](scenari/) (script che preparano una modifica ricorrente);
- correzioni e traduzioni.

Prima di aprire una segnalazione o una richiesta di modifica controlla di non allegare dati della tua casa
(programmi, log, `CLAUDE.local.md`, indirizzi, nomi utente). I test si eseguono senza Miniserver:
`python -m unittest discover -s tests`. Se trovi un problema di sicurezza del toolkit, apri una segnalazione
**senza i dettagli** e chiedi un contatto privato.

## Struttura del progetto

```
lox, lox.cmd, lox.py    avvio del toolkit (Bash / PowerShell)
claudelox/              client Miniserver (client, crypto), struttura dell'app (structure, appstructure),
                        programma (loxcc, program, editor), caricamento (uploader), log, backup, Shelly,
                        telefono Android via adb (telefono: ricollega gli Shelly al Wi-Fi), CLI
scenari/                script di modifica riutilizzabili (porta che accende le luci, sensore dell'aria Amazon,
                        antifurto con assistente, scene Govee come atmosfere, riordino dell'app da un piano,
                        avviso quando un dispositivo di rete non risponde)
integrazioni/           libreria aperta di collegamenti con dispositivi esterni (Govee, Hikvision, Amazon...)
raspberry/              installazione sul Raspberry Pi delle integrazioni che girano li'
tests/                  test senza Miniserver:  python -m unittest discover -s tests
CLAUDE.md               istruzioni e regole per Claude (pubbliche)
CLAUDE.local.md         dati del tuo impianto (privato, creato al primo avvio)
```

## English summary

ClaudeLox lets [Claude Code](https://claude.com/claude-code) monitor, control and reprogram a **Loxone**
smart home on the local network, using Loxone's documented WebSocket/HTTP APIs (encrypted login, tokens).
It downloads the Miniserver program (the Loxone Config project XML), analyses and edits it on a copy, shows
a readable diff, and — only with your consent — uploads it either directly (FTP + logic restart, with
backup, verification and automatic rollback; the app structure `LoxAPP3.json` and `permissions.bin` are
updated too, so new or moved controls show up in the app) or through Loxone Config. Every downloaded version
and every edited copy is kept locally, so versions can be compared and restored. Safety rules for the agent
live in `CLAUDE.md` (alarm, doors, gates and windows need explicit consent for every action, enforced by the
CLI). The `integrazioni/` folder is an open library of bridges to devices Loxone cannot drive natively (Govee
Bluetooth LED strips, Hikvision cameras and recorders with local face recognition, the Amazon Smart Air Quality
Monitor via the Alexa cloud, Shelly), installed on a Raspberry Pi where needed; contributions and test reports
from other installations are welcome.

Tested on a single installation (Miniserver Gen 2, firmware 17.2 and 17.3, direct upload included). The
direct upload relies on undocumented Miniserver behaviour and needs the Miniserver FTP server, which
is disabled by default since firmware 16.1. The CLI and docs are in Italian. Unofficial project, not affiliated
with Loxone; use at your own risk.

## Licenza

[MIT](LICENSE)
