#!/usr/bin/env bash
# Prepara un Raspberry Pi come ponte tra Loxone e i dispositivi della libreria integrazioni/.
# Installa (o aggiorna) le integrazioni indicate; senza argomenti tutte quelle che girano sul Raspberry:
#   govee-bluetooth   luci LED Govee via Bluetooth            (config: /etc/default/loxone-govee)
#   hikvision-isapi   telecamere e registratori Hikvision,     (config: /etc/hikvision-loxone/config.toml)
#                     anche riconoscimento facciale sul Raspberry
#   amazon-alexa      sensore della qualita' dell'aria Amazon  (config: /etc/default/amazon-loxone)
# Uso, dalla cartella del repository:
#   sudo bash raspberry/install.sh [govee-bluetooth] [hikvision-isapi] [amazon-alexa]
# I servizi girano come l'utente che lancia sudo (senza sudo: "pi"); per sceglierne un altro:
#   sudo SERVICE_USER=<utente> bash raspberry/install.sh ...
# Si puo' rilanciare quando si vuole: aggiorna programmi e servizi, non tocca le configurazioni.
set -euo pipefail
cd "$(dirname "$0")/../integrazioni"
[ "$(id -u)" -eq 0 ] || { echo "esegui con sudo: sudo bash raspberry/install.sh"; exit 1; }
DEST=/opt/claudelox-raspberry
[ $# -gt 0 ] || set -- govee-bluetooth hikvision-isapi amazon-alexa

# utente dei servizi: mai root. I programmi restano di root; a lui appartengono solo le cartelle dei dati
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-pi}}"
case "$SERVICE_USER" in
  "" | *[!A-Za-z0-9_.-]*) echo "SERVICE_USER non valido: '$SERVICE_USER'"; exit 1 ;;
esac
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "l'utente '$SERVICE_USER' non esiste: indica quello giusto con  sudo SERVICE_USER=<utente> bash raspberry/install.sh"
  exit 1
fi
if [ "$(id -u "$SERVICE_USER")" -eq 0 ]; then
  echo "i servizi non devono girare come root: indica un utente normale con  sudo SERVICE_USER=<utente> bash raspberry/install.sh"
  exit 1
fi
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
echo "Utente dei servizi: $SERVICE_USER (gruppo $SERVICE_GROUP)"
# i comandi da lanciare a mano vanno eseguiti come utente dei servizi: se non e' chi ha lanciato sudo, lo si scrive
AS_USER=""
[ "${SUDO_USER:-}" = "$SERVICE_USER" ] || AS_USER="sudo -u $SERVICE_USER "

# programmi, ambiente Python e modelli: di root, in sola lettura per l'utente dei servizi (anche dove una
# versione precedente di questo script li aveva lasciati all'utente)
install -d -m 0755 -o root -g root "$DEST"
chown -hR root:root "$DEST"

as_user() {  # comando...: lo esegue come utente dei servizi, da "/" (potrebbe non poter entrare nel repository)
  (cd / && sudo -u "$SERVICE_USER" "$@")
}

APT_UPDATED=0
apt_install() {  # pacchetti...: aggiorna l'elenco dei pacchetti (una volta sola), poi installa quelli che mancano
  if [ "$APT_UPDATED" -eq 0 ]; then
    apt-get update >/dev/null || echo "ATTENZIONE: 'apt-get update' non riuscito: provo comunque a installare"
    APT_UPDATED=1
  fi
  # --no-upgrade: i pacchetti gia' installati restano alla loro versione (lo script non aggiorna il sistema)
  apt-get install -y --no-upgrade "$@" >/dev/null
}

state_dir() {  # cartella [permessi]: dati di un servizio, i soli file che appartengono al suo utente
  install -d -m "${2:-0755}" -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$1"
  chown -hR "$SERVICE_USER:$SERVICE_GROUP" "$1"
}

install_unit() {  # file.service: lo installa scrivendo l'utente dei servizi al posto di "pi"
  local tmp
  tmp="$(mktemp)"
  sed "s/^User=.*/User=$SERVICE_USER/" "$1" > "$tmp"
  install -m 0644 -o root -g root "$tmp" "/etc/systemd/system/$(basename "$1")"
  rm -f "$tmp"
  systemctl daemon-reload
}

venv_pip() {  # argomenti di "pip install": ambiente Python comune a Govee e Amazon
  # pip scarica e installa come utente dei servizi, non come root; finito, l'ambiente torna di root, leggibile
  # da tutti (qualunque sia la umask di chi installa)
  local rc=0
  apt_install python3-venv
  [ -d "$DEST/venv" ] || install -d -m 0755 "$DEST/venv"
  chown -hR "$SERVICE_USER:$SERVICE_GROUP" "$DEST/venv"
  { [ -x "$DEST/venv/bin/python" ] || as_user python3 -m venv "$DEST/venv"; } \
    && as_user "$DEST/venv/bin/pip" install --quiet "$@" || rc=$?
  chown -hR root:root "$DEST/venv"
  chmod -R a+rX "$DEST/venv"
  [ "$rc" -eq 0 ] || echo "pacchetti Python non installati (pip install $*): guarda l'errore di pip qui sopra (rete, o versione di Python troppo vecchia per la libreria) e rilancia lo script"
  return "$rc"
}

govee_address_set() {  # vero se in /etc/default/loxone-govee c'e' un GOVEE_ADDRESS non vuoto
  [ -n "$(sed -n 's/^[[:space:]]*GOVEE_ADDRESS=//p' /etc/default/loxone-govee | tail -n 1 | tr -d "\"' \t\r")" ]
}

install_govee() {
  apt_install bluez
  install -m 0755 -o root -g root govee-bluetooth/loxone_govee.py "$DEST/"
  state_dir /var/lib/loxone-govee     # catalogo delle scene ("loxone_govee.py catalogo <modello>")
  venv_pip --upgrade "bleak>=0.22,<2"
  usermod -aG bluetooth "$SERVICE_USER"
  [ -f /etc/default/loxone-govee ] || install -m 0644 govee-bluetooth/loxone-govee.env.example /etc/default/loxone-govee
  install_unit govee-bluetooth/loxone-govee.service
  if govee_address_set; then
    systemctl enable loxone-govee.service >/dev/null
    systemctl restart loxone-govee.service
    echo "loxone-govee: $(systemctl is-active loxone-govee)   log: journalctl -u loxone-govee -f"
  else   # senza indirizzo il ponte non ha niente da comandare: non lo si avvia, si dice cosa manca
    echo "Luci Govee: manca l'indirizzo Bluetooth della luce, quindi il servizio non viene avviato. Prossimi passi:"
    echo "  1) ${AS_USER}$DEST/venv/bin/python $DEST/loxone_govee.py scan"
    echo "     (se il Bluetooth nega l'accesso esci e rientra: l'utente e' appena entrato nel gruppo bluetooth)"
    echo "  2) sudo nano /etc/default/loxone-govee      (GOVEE_ADDRESS=AA:BB:CC:DD:EE:FF, ALLOW_FROM=<IP del Miniserver>,127.0.0.1)"
    echo "  3) sudo systemctl enable loxone-govee && sudo systemctl restart loxone-govee"
  fi
}

# modelli del riconoscimento facciale (OpenCV Zoo: YuNet con licenza MIT, SFace con licenza Apache 2.0),
# accettati solo se l'sha256 coincide; li scarica l'utente dei servizi, non root
fetch_model() {  # nome cartella sha256: 0 se alla fine il modello c'e' ed e' quello verificato
  local f="$DEST/modelli/$1" tmp
  if [ -f "$f" ] && echo "$3  $f" | sha256sum -c --quiet >/dev/null 2>&1; then return 0; fi
  tmp="$(as_user mktemp)" || { echo "modello $1: file temporaneo non creato"; return 1; }
  if ! as_user curl -fsSL --retry 3 --connect-timeout 20 --speed-limit 1024 --speed-time 60 -o "$tmp" \
      "https://github.com/opencv/opencv_zoo/raw/main/models/$2/$1"; then
    rm -f "$tmp"
    echo "modello $1: scaricamento non riuscito"
    return 1
  fi
  install -m 0644 -o root -g root "$tmp" "$f.tmp"
  rm -f "$tmp"
  if echo "$3  $f.tmp" | sha256sum -c --quiet >/dev/null 2>&1; then
    mv "$f.tmp" "$f"
  else
    rm -f "$f.tmp"
    echo "modello $1: il file scaricato non e' quello verificato (sha256 diverso), scartato"
    return 1
  fi
}

install_hikvision() {
  local f faces=1
  apt_install python3-requests
  install -m 0755 -o root -g root hikvision-isapi/hikvision_loxone.py "$DEST/"
  install -m 0644 -o root -g root hikvision-isapi/volti_locali.py "$DEST/"
  # configurazione (contiene password): di root, leggibile dal gruppo dell'utente dei servizi. La procedura
  # guidata e il comando "regole" scrivono il file con il gruppo che trovano qui
  install -d -m 0750 -o root -g "$SERVICE_GROUP" /etc/hikvision-loxone
  for f in /etc/hikvision-loxone/config.toml /etc/hikvision-loxone/config.toml.bak; do
    if [ -f "$f" ]; then chown "root:$SERVICE_GROUP" "$f"; chmod 0640 "$f"; fi
  done
  state_dir /var/lib/hikvision-loxone 0750    # impronte dei volti noti, foto dei volti riconosciuti
  install_unit hikvision-isapi/hikvision-loxone.service
  systemctl enable hikvision-loxone.service >/dev/null
  if [ -f /etc/hikvision-loxone/config.toml ]; then
    systemctl restart hikvision-loxone.service
    echo "hikvision-loxone: $(systemctl is-active hikvision-loxone)   log: journalctl -u hikvision-loxone -f"
  else
    echo "Telecamere: sudo python3 $DEST/hikvision_loxone.py configura --miniserver <ip> --telecamere <ip1>,<ip2>"
  fi
  # facoltativo, per ultimo: riconoscimento facciale sul Raspberry (OpenCV e modelli). Se qui qualcosa non
  # riesce lo script prosegue: il ponte degli eventi e' gia' installato e avviato
  apt_install python3-opencv python3-numpy || { faces=0; echo "OpenCV (python3-opencv, python3-numpy): installazione non riuscita"; }
  install -d -m 0755 -o root -g root "$DEST/modelli"
  fetch_model face_detection_yunet_2023mar.onnx face_detection_yunet \
    8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4 || faces=0
  fetch_model face_recognition_sface_2021dec.onnx face_recognition_sface \
    0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79 || faces=0
  if [ "$faces" -eq 0 ]; then
    echo "ATTENZIONE: il riconoscimento facciale sul Raspberry (regole \"volto\") non e' pronto: qui sopra c'e' cosa non e' riuscito."
    echo "  Gli eventi delle telecamere funzionano lo stesso. Per riprovare: sudo bash raspberry/install.sh hikvision-isapi"
  fi
}

# versione della libreria provata: Amazon cambia spesso le sue interfacce, si aggiorna dopo una prova
AIOAMAZONDEVICES=16.9.0

install_amazon() {
  install -m 0755 -o root -g root amazon-alexa/amazon_loxone.py "$DEST/"
  venv_pip "aioamazondevices==$AIOAMAZONDEVICES"
  [ -f /etc/default/amazon-loxone ] || install -m 0644 amazon-alexa/amazon-loxone.env.example /etc/default/amazon-loxone
  state_dir /var/lib/amazon-loxone 0700    # dati di accesso ad Amazon
  install_unit amazon-alexa/amazon-loxone.service
  systemctl enable amazon-loxone.service >/dev/null
  if [ -f /var/lib/amazon-loxone/accesso.json ]; then
    systemctl restart amazon-loxone.service
    echo "amazon-loxone: $(systemctl is-active amazon-loxone)   log: journalctl -u amazon-loxone -f"
  else
    echo "Sensore aria Amazon: ${AS_USER}$DEST/venv/bin/python $DEST/amazon_loxone.py accedi && sudo systemctl restart amazon-loxone"
  fi
}

for nome in "$@"; do
  case "$nome" in
    govee-bluetooth) install_govee ;;
    hikvision-isapi) install_hikvision ;;
    amazon-alexa) install_amazon ;;
    *) echo "integrazione sconosciuta: $nome (vedi integrazioni/README.md)"; exit 1 ;;
  esac
done
