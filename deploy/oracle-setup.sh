#!/usr/bin/env bash
# Provision this app on a fresh Oracle Cloud Always Free VM (Ubuntu 22.04/24.04).
#
# Why a VM rather than a free PaaS: Open-Meteo meters its free tier per IP per day, and shared
# hosting shares that IP with strangers who may already have spent the quota. An Always Free
# instance has a public IPv4 of its own, so the quota is ours alone.
#
#   curl -fsSL https://raw.githubusercontent.com/SatyamSingh-Git/Weather-Advisory-Support-Bot/main/deploy/oracle-setup.sh | bash -s -- sk-or-v1-YOUR_KEY
#
set -euo pipefail

KEY="${1:?usage: oracle-setup.sh <OPENROUTER_API_KEY> [MODEL]}"
MODEL="${2:-deepseek/deepseek-v4-flash}"
REPO="https://github.com/SatyamSingh-Git/Weather-Advisory-Support-Bot.git"
APP_DIR=/opt/weather-advisory-bot

# nip.io resolves <dashed-ip>.nip.io to that IP, which lets Let's Encrypt issue a real certificate
# without owning a domain. A bare IP cannot get one.
PUBLIC_IP="$(curl -fsS --max-time 10 https://api.ipify.org)"
HOSTNAME="${PUBLIC_IP//./-}.nip.io"

echo "==> Public IP: $PUBLIC_IP    HTTPS host: $HOSTNAME"

sudo apt-get update -qq
sudo apt-get install -y -qq git python3-venv python3-pip debian-keyring debian-archive-keyring apt-transport-https curl

echo "==> Installing Caddy (terminates TLS, proxies to the app)"
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
sudo apt-get update -qq && sudo apt-get install -y -qq caddy

echo "==> Fetching the app"
sudo rm -rf "$APP_DIR"
sudo git clone --depth 1 -q "$REPO" "$APP_DIR"
sudo python3 -m venv "$APP_DIR/.venv"
sudo "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

printf 'OPENROUTER_API_KEY=%s\nOPENROUTER_MODEL=%s\n' "$KEY" "$MODEL" | sudo tee "$APP_DIR/.env" >/dev/null
sudo chmod 600 "$APP_DIR/.env"

echo "==> systemd service"
sudo tee /etc/systemd/system/weatherbot.service >/dev/null <<UNIT
[Unit]
Description=Weather Advisory Support Bot
After=network-online.target

[Service]
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/uvicorn app.server:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

echo "==> Caddy site (automatic HTTPS via Let's Encrypt)"
sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDY
$HOSTNAME {
    reverse_proxy 127.0.0.1:8000
}
CADDY

# Oracle images ship a default-DROP iptables policy; Caddy needs 80 and 443 through it.
sudo iptables -I INPUT 5 -p tcp --dport 80  -j ACCEPT 2>/dev/null || true
sudo iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT 2>/dev/null || true
sudo netfilter-persistent save 2>/dev/null || true

sudo systemctl daemon-reload
sudo systemctl enable --now weatherbot
sudo systemctl restart caddy

sleep 4
echo
echo "==> Local health check:"
curl -fsS http://127.0.0.1:8000/health || echo "  app not answering yet - check: journalctl -u weatherbot -n 40"
echo
echo "=================================================================="
echo "  Live at: https://$HOSTNAME"
echo "  Diagnostics: https://$HOSTNAME/api/diagnostics"
echo
echo "  Open ports 80 and 443 in the OCI console as well:"
echo "  Networking > VCN > Subnet > Security List > Add Ingress Rules"
echo "  Source 0.0.0.0/0, TCP, destination ports 80 and 443"
echo "=================================================================="
