#!/usr/bin/env bash
# Prepara un Raspberry Pi come ponte tra Loxone e i dispositivi della libreria integrazioni/.
# Installa (o aggiorna) le integrazioni indicate; senza argomenti tutte quelle che girano sul Raspberry:
#   govee-bluetooth   luci LED Govee via Bluetooth            (config: /etc/default/loxone-govee)
#   hikvision-isapi   telecamere e registratori Hikvision,     (config: /etc/hikvision-loxone/config.toml)
#                     anche riconoscimento facciale sul Raspberry
# Uso, dalla cartella del repository:  sudo raspberry/install.sh [govee-bluetooth] [hikvision-isapi]
set -euo pipefail
cd "$(dirname "$0")/../integrazioni"
[ "$(id -u)" -eq 0 ] || { echo "esegui con sudo"; exit 1; }
DEST=/opt/claudelox-raspberry
[ $# -gt 0 ] || set -- govee-bluetooth hikvision-isapi
install -d -o pi -g pi "$DEST"

install_govee() {
  apt-get install -y python3-venv bluez >/dev/null
  install -m 0755 -o pi -g pi govee-bluetooth/loxone_govee.py "$DEST/"
  [ -x "$DEST/venv/bin/python" ] || sudo -u pi python3 -m venv "$DEST/venv"
  sudo -u pi "$DEST/venv/bin/pip" install --quiet --upgrade "bleak>=0.22,<2"
  usermod -aG bluetooth pi
  [ -f /etc/default/loxone-govee ] || install -m 0644 govee-bluetooth/loxone-govee.env.example /etc/default/loxone-govee
  install -m 0644 govee-bluetooth/loxone-govee.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable loxone-govee.service >/dev/null
  systemctl restart loxone-govee.service
  echo "loxone-govee: $(systemctl is-active loxone-govee)   log: journalctl -u loxone-govee -f"
}

# modelli del riconoscimento facciale (OpenCV Zoo, licenza Apache 2.0), accettati solo se l'sha256 coincide
fetch_model() {  # nome cartella sha256
  local f="$DEST/modelli/$1"
  if [ -f "$f" ] && echo "$3  $f" | sha256sum -c --quiet 2>/dev/null; then return; fi
  sudo -u pi curl -fsSL --retry 3 -o "$f.tmp" "https://github.com/opencv/opencv_zoo/raw/main/models/$2/$1"
  if echo "$3  $f.tmp" | sha256sum -c --quiet; then mv "$f.tmp" "$f"; else rm -f "$f.tmp"; echo "modello $1 non verificato"; exit 1; fi
}

install_hikvision() {
  apt-get install -y python3-requests python3-opencv python3-numpy >/dev/null
  install -m 0755 -o pi -g pi hikvision-isapi/hikvision_loxone.py "$DEST/"
  install -m 0644 -o pi -g pi hikvision-isapi/volti_locali.py "$DEST/"
  install -d -o pi -g pi "$DEST/modelli"
  fetch_model face_detection_yunet_2023mar.onnx face_detection_yunet \
    8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
  fetch_model face_recognition_sface_2021dec.onnx face_recognition_sface \
    0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79
  install -d -m 0750 -g pi /etc/hikvision-loxone
  install -m 0644 hikvision-isapi/hikvision-loxone.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable hikvision-loxone.service >/dev/null
  if [ -f /etc/hikvision-loxone/config.toml ]; then
    systemctl restart hikvision-loxone.service
    echo "hikvision-loxone: $(systemctl is-active hikvision-loxone)   log: journalctl -u hikvision-loxone -f"
  else
    echo "Telecamere: sudo python3 $DEST/hikvision_loxone.py configura --miniserver <ip> --telecamere <ip1>,<ip2>"
  fi
}

for nome in "$@"; do
  case "$nome" in
    govee-bluetooth) install_govee ;;
    hikvision-isapi) install_hikvision ;;
    *) echo "integrazione sconosciuta: $nome (vedi integrazioni/README.md)"; exit 1 ;;
  esac
done
