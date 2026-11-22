# Come contribuire a ClaudeLox

Grazie! ClaudeLox e' un progetto gratuito, nato in una casa vera e mantenuto nel tempo libero.
*Contributions in English are welcome too.*

## Come funziona

1. Fai una **copia** del progetto sul tuo account GitHub (pulsante **Fork**).
2. Prepara la modifica su un ramo della tua copia.
3. Apri una **Pull Request**: e' la proposta di integrare la tua modifica nel progetto.
4. GitHub esegue i test in automatico e il manutentore legge la proposta: puo' chiedere correzioni, integrarla o
   rifiutarla.

Nessuno puo' cambiare direttamente il progetto: entra solo cio' che il manutentore approva.

**Non sei un programmatore?** Apri la tua copia con Claude Code e chiedi: *"prepara una Pull Request per ClaudeLox
con questa modifica, seguendo CONTRIBUTING.md"*. Oppure apri semplicemente una
[segnalazione](https://github.com/Henry12034/claudelox/issues/new/choose): anche dire cosa funziona sul tuo impianto
e' un contributo.

## Cosa serve di piu'

- **Prove su altri impianti**: modello di Miniserver, firmware, cosa funziona e cosa no.
- **Integrazioni nuove** per dispositivi che Loxone non comanda da solo, o modelli in piu' per quelle esistenti: le
  regole sono in [`integrazioni/README.md`](integrazioni/README.md), il modello in
  [`integrazioni/_modello/`](integrazioni/_modello/README.md).
- **Scenari** in [`scenari/`](scenari/): script che preparano una modifica ricorrente del programma.
- **Correzioni** al codice e alle guide, traduzioni.

## Regole che valgono sempre

1. **Niente dati di casa.** Mai programmi (`.Loxone`, `.LoxCC`, `sps_*.zip`), `LoxAPP3.json`, `permissions.bin`, log,
   `.env`, `CLAUDE.local.md`, indirizzi, coordinate, numeri di serie, MAC, nomi di persone. Negli esempi usa `10.0.0.x`,
   `AA:BB:CC:DD:EE:FF`, `Mario`. Attenzione ai dati copiati nei test: anche un UUID o una latitudine vengono da casa tua.
2. **Niente segreti.** Le password le scrive la persona, in un terminale o in una procedura guidata: non passano dalla
   chat, dalla riga di comando o dal codice.
3. **Prudenza con cio' che apre.** Tutto cio' che puo' aprire, disinserire o sbloccare parte in prova (`dry_run`), con
   conferme e pause, e si attiva solo con il consenso esplicito di chi abita la casa.
4. **Il programma si tocca solo su una copia**, con confronto e prova a secco prima di caricare.
5. **Solo interfacce accessibili** (API documentate, FTP del Miniserver, file di progetto): niente reverse engineering
   di Loxone Config.
6. **Test senza dispositivo** per ogni comportamento nuovo, in `tests/`.

## Prima di aprire la Pull Request

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt     # su Windows: .venv\Scripts\python.exe
.venv/bin/python -m unittest discover -s tests
```

Spiega cosa cambia e su quale impianto o dispositivo l'hai provato. Una modifica piccola e chiara si integra prima di
una grande.

## Problemi di sicurezza

Non aprire una segnalazione pubblica: segui [`SECURITY.md`](SECURITY.md).

## Licenza

Contribuendo accetti che il tuo contributo sia distribuito con la [licenza MIT](LICENSE) del progetto. Chi partecipa
segue il [codice di condotta](CODE_OF_CONDUCT.md).
