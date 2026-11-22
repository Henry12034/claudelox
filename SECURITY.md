# Sicurezza

ClaudeLox comanda una casa vera: una falla puo' riguardare antifurto, porte e dati personali.

## Come segnalare un problema

**Non aprire una segnalazione pubblica.** Usa la segnalazione privata di GitHub:
[Report a vulnerability](https://github.com/Henry12034/claudelox/security/advisories/new).

Scrivi cosa hai trovato, come riprodurlo e su quale versione. Non allegare dati della tua casa. Il progetto e'
mantenuto nel tempo libero: la risposta arriva appena possibile, e il problema viene reso pubblico solo dopo la
correzione. Si corregge sempre l'ultima versione del ramo `main`.

## Cosa conta come problema di sicurezza

- comandi ad antifurto, porte o cancelli eseguiti senza il consenso previsto;
- password, token o dati dell'impianto che finiscono in chiaro in output, log o file pubblicati;
- un caricamento che lascia la casa senza programma o salta i controlli;
- un'integrazione che accetta comandi da chi non dovrebbe.

Le falle del Miniserver o di Loxone Config vanno segnalate a Loxone.

## Se hai pubblicato per errore dati della tua casa

Cambia subito le password coinvolte e apri una segnalazione privata: una cancellazione da GitHub non basta, perche' i
vecchi commit e le copie possono restare raggiungibili.

## Quando valuti il codice di altri

Una Pull Request o un fork sono codice non fidato: non eseguirli su un computer con il tuo `.env`, il token o l'accesso
alla casa. I test delle Pull Request girano su GitHub, senza accesso ai tuoi dati.
