#!/usr/bin/env bash
# Installa (o aggiorna) i servizi ClaudeLox per Raspberry Pi OS:
#  - loxone-govee: Loxone -> luci Govee via Bluetooth (config: /etc/default/loxone-govee)
#  - hikvision-loxone: telecamere Hikvision -> Loxone (config: /etc/hikvision-loxone/config.toml)
# Uso, dalla cartella che contiene questi file:  sudo ./install.sh
set -euo pipefail
cd "$(dirname "$0")"
[ "$(id -u)" -eq 0 ] || { echo "esegui con sudo"; exit 1; }
DEST=/opt/claudelox-raspberry

apt-get install -y python3-venv python3-requests bluez >/dev/null
install -d -o pi -g pi "$DEST"
install -m 0755 -o pi -g pi loxone_govee.py hikvision_loxone.py "$DEST/"
[ -x "$DEST/venv/bin/python" ] || sudo -u pi python3 -m venv "$DEST/venv"
sudo -u pi "$DEST/venv/bin/pip" install --quiet --upgrade "bleak>=0.22,<2"
usermod -aG bluetooth pi

[ -f /etc/default/loxone-govee ] || install -m 0644 loxone-govee.env.example /etc/default/loxone-govee
install -d -m 0750 -g pi /etc/hikvision-loxone
install -m 0644 loxone-govee.service hikvision-loxone.service /etc/systemd/system/
systemctl daemon-reload

systemctl enable loxone-govee.service hikvision-loxone.service >/dev/null
systemctl restart loxone-govee.service
if [ -f /etc/hikvision-loxone/config.toml ]; then
  systemctl restart hikvision-loxone.service
else
  echo "Telecamere: sudo python3 $DEST/hikvision_loxone.py configura --miniserver <ip> --telecamere <ip1>,<ip2>"
fi
echo "loxone-govee: $(systemctl is-active loxone-govee)  hikvision-loxone: $(systemctl is-active hikvision-loxone || true)"
echo "Log: journalctl -u loxone-govee -f   /   journalctl -u hikvision-loxone -f"
