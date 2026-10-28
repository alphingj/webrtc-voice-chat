#!/usr/bin/env bash
# Idempotent installer for the Raspberry Pi. Safe to run again after an update.
#   sudo APP_DIR=/home/gj/voice ./setup.sh
# Running as the owner with sudo also works.
set -euo pipefail

APP_DIR="${APP_DIR:-/home/gj/voice}"
OWNER="${VOICE_USER:-gj}"
SERVICE_NAME="voice"
BIND_IP="${BIND_IP:-}"

say() { printf '\n==> %s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ -d "$APP_DIR" ]] || fail "directory not found: $APP_DIR"
cd "$APP_DIR"
[[ -f server.py ]] || fail "server.py not found in $APP_DIR"
[[ -f config.json ]] || fail "config.json not found in $APP_DIR"

if [[ $EUID -eq 0 ]]; then
  SUDO=()
  RUNAS=()
  if [[ "$OWNER" != "root" ]]; then RUNAS=(runuser -u "$OWNER" --); fi
else
  command -v sudo >/dev/null || fail "run this as root or install sudo"
  SUDO=(sudo)
  RUNAS=()
fi

if [[ -z "$BIND_IP" ]]; then
  BIND_IP="$(python3 -c 'import json;print(json.load(open("config.json"))["bind_ip"])')"
fi
VOICE_PORT="$(python3 -c 'import json;print(json.load(open("config.json"))["voice_port"])')"
ADMIN_PORT="$(python3 -c 'import json;print(json.load(open("config.json"))["admin_port"])')"

say "Installing packages (python3-aiohttp, openssl, sqlite3)"
"${SUDO[@]}" apt-get update -qq
DEBIAN_FRONTEND=noninteractive "${SUDO[@]}" apt-get install -y --no-install-recommends \
  python3-aiohttp openssl sqlite3 ca-certificates

say "Preparing directories"
mkdir -p "$APP_DIR/certs" "$APP_DIR/static/app/icons" "$APP_DIR/static/admin"
if [[ $EUID -eq 0 ]]; then
  chown -R "$OWNER:$OWNER" "$APP_DIR"
fi
chmod 700 "$APP_DIR/certs"

say "TLS certificate for $BIND_IP"
CERT="$APP_DIR/certs/server.crt"
KEY="$APP_DIR/certs/server.key"
if [[ -s "$CERT" && -s "$KEY" ]]; then
  echo "Certificate already exists, keeping it."
  if ! openssl x509 -in "$CERT" -noout -text | grep -q "IP Address:${BIND_IP}"; then
    echo "WARNING: $CERT has no subjectAltName for $BIND_IP."
    echo "         Regenerate it with the command in README.md (section 'Certificates')."
  fi
else
  # Self-signed, CA:true so Android can also install it as a user CA.
  openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout "$KEY" -out "$CERT" \
    -subj "/CN=${BIND_IP}" \
    -addext "subjectAltName=IP:${BIND_IP}" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign" \
    -addext "extendedKeyUsage=serverAuth"
  chmod 600 "$KEY"
  if [[ $EUID -eq 0 ]]; then chown "$OWNER:$OWNER" "$CERT" "$KEY"; fi
  echo "Generated $CERT (10 years, SAN IP:${BIND_IP})."
fi

say "Initialising the database"
"${RUNAS[@]}" python3 manage.py --config "$APP_DIR/config.json" init-db

say "Installing systemd unit"
TMP_UNIT="$(mktemp)"
sed -e "s|/home/gj/voice|${APP_DIR}|g" -e "s|^User=gj$|User=${OWNER}|" -e "s|^Group=gj$|Group=${OWNER}|" \
  "$APP_DIR/voice.service" > "$TMP_UNIT"
"${SUDO[@]}" install -m 0644 "$TMP_UNIT" "/etc/systemd/system/${SERVICE_NAME}.service"
rm -f "$TMP_UNIT"
"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl enable "${SERVICE_NAME}.service" >/dev/null

say "Starting the service"
"${SUDO[@]}" systemctl restart "${SERVICE_NAME}.service"
sleep 3
if "${SUDO[@]}" systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  echo "Service is running."
else
  echo "Service failed to start. Recent log lines:"
  "${SUDO[@]}" journalctl -u "${SERVICE_NAME}.service" -n 30 --no-pager || true
  exit 1
fi

cat <<EOF

==> Done.

  Voice app:    https://${BIND_IP}:${VOICE_PORT}
  Admin panel:  https://${BIND_IP}:${ADMIN_PORT}

  First administrator (prompts for a password):
      cd ${APP_DIR} && python3 manage.py create-admin <username>

  Service:      sudo systemctl status ${SERVICE_NAME}
  Live logs:    sudo journalctl -u ${SERVICE_NAME} -f

The server listens only on ${BIND_IP}. Open the URLs from a device on your tailnet
and accept the certificate warning once (see README.md).
EOF
