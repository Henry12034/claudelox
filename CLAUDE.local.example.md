# Impianto di casa: dati privati (escluso da git)

Copia questo file come `CLAUDE.local.md` (oppure lascia che lo crei Claude al primo avvio). Claude lo legge a
ogni sessione e lo aggiorna quando scopre qualcosa di stabile sull'impianto. Non pubblicarlo: e' in `.gitignore`.

## Miniserver

- Nome e progetto: ...
- IP in rete locale: 192.168.x.x. Modello, seriale, firmware: (vedi `./lox info`)
- Utente dedicato a Claude: `...` (maiuscole comprese). Diritti: ...
- Remote Connect: attivo / non attivo.

## Hardware e dispositivi esterni

- Extension, Tree, Air, intercom...
- Dispositivi via rete (Shelly, Raspberry, ...): IP previsti dal programma e a cosa servono.

## Logiche importanti

- Esempio: "Porta casa" vale 1 a porta CHIUSA; e' collegata ad antifurto, notifiche, luce esterna...
- Nomi che nell'app e nel programma sono diversi (es. blocco "Luci_sala" = controllo "Sala/cucina").
- Id delle scene luci verificati (es. 778 = "Off").

## Stato e problemi noti

- Data dell'ultimo controllo generale e cosa non funziona.

## Storico delle modifiche al programma

- Data: cosa e' stato cambiato, come e' stato caricato, esito della verifica.
