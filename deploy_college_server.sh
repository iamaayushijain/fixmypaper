+#!/usr/bin/env bash
set -euo pipefail

# One-shot deployment script for Ubuntu/Debian college servers.
# It deploys BOTH services on one machine:
#   1) fixmypaper (main app: backend + frontend)
#   2) reference-quality API (REFRENCE-SECTION)

MAIN_REPO_URL="${MAIN_REPO_URL:-https://github.com/iamaayushijain/fixmypaper.git}"
REFERENCE_REPO_URL="${REFERENCE_REPO_URL:-https://github.com/06hardik/Research-Paper-Model.git}"

MAIN_DIR="${MAIN_DIR:-/opt/fixmypaper}"
REFERENCE_DIR="${REFERENCE_DIR:-/opt/reference-service}"

APP_USER="${APP_USER:-${SUDO_USER:-$USER}}"
DOMAIN="${DOMAIN:-_}"

BACKEND_PORT="${BACKEND_PORT:-7860}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
REFERENCE_PORT="${REFERENCE_PORT:-8000}"
UPLOAD_TIMEOUT_MS="${UPLOAD_TIMEOUT_MS:-900000}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
PARSER_URL="${PARSER_URL:-https://tmkc-100bar-extraction-engine.hf.space/api/processCitation}"

log() {
  printf "\n[deploy] %s\n" "$*"
}

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script with sudo/root."
  echo "Example: sudo bash deploy_college_local_full_stack.sh"
  exit 1
fi

if ! id "${APP_USER}" >/dev/null 2>&1; then
  echo "User '${APP_USER}' does not exist. Set APP_USER to a valid linux user."
  exit 1
fi

log "Installing system packages"
apt-get update
apt-get install -y \
  git \
  curl \
  nginx \
  ca-certificates \
  python3 \
  python3-venv \
  python3-pip \
  ghostscript \
  poppler-utils \
  libglib2.0-0 \
  libsm6 \
  libxext6 \
  libxrender1 \
  libgl1

if ! command -v node >/dev/null 2>&1; then
  log "Installing Node.js 20"
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
else
  NODE_MAJOR="$(node -v | sed -E 's/^v([0-9]+).*/\1/')"
  if [[ "${NODE_MAJOR}" -lt 20 ]]; then
    log "Upgrading Node.js to 20"
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
  fi
fi

log "Cloning or updating main repository"
if [[ -d "${MAIN_DIR}/.git" ]]; then
  git -C "${MAIN_DIR}" fetch --all --prune
  git -C "${MAIN_DIR}" checkout main
  git -C "${MAIN_DIR}" pull --ff-only
else
  rm -rf "${MAIN_DIR}"
  git clone "${MAIN_REPO_URL}" "${MAIN_DIR}"
fi

log "Cloning or updating reference repository"
if [[ -d "${REFERENCE_DIR}/.git" ]]; then
  git -C "${REFERENCE_DIR}" fetch --all --prune
  git -C "${REFERENCE_DIR}" checkout main
  git -C "${REFERENCE_DIR}" pull --ff-only
else
  rm -rf "${REFERENCE_DIR}"
  git clone "${REFERENCE_REPO_URL}" "${REFERENCE_DIR}"
fi

chown -R "${APP_USER}:${APP_USER}" "${MAIN_DIR}" "${REFERENCE_DIR}"

log "Setting up reference service (REFRENCE-SECTION)"
sudo -u "${APP_USER}" bash -lc "
  set -euo pipefail
  cd '${REFERENCE_DIR}/REFRENCE-SECTION'
  ${PYTHON_BIN} -m venv .venv
  source .venv/bin/activate
  pip install --upgrade pip
  pip install -r requirements.txt
  cat > .env <<EOF
API_PORT=${REFERENCE_PORT}
PARSER_URL=${PARSER_URL}
PYTHONUTF8=1
EOF
"

log "Setting up main backend"
sudo -u "${APP_USER}" bash -lc "
  set -euo pipefail
  cd '${MAIN_DIR}'
  ${PYTHON_BIN} -m venv .venv
  source .venv/bin/activate
  pip install --upgrade pip
  pip install -r requirements.txt
  mkdir -p uploads processed
"

log "Setting up main frontend"
sudo -u "${APP_USER}" bash -lc "
  set -euo pipefail
  cd '${MAIN_DIR}/frontend'
  cat > .env <<EOF
BACKEND_INTERNAL_URL=\"http://127.0.0.1:${BACKEND_PORT}\"
UPLOAD_PROXY_TIMEOUT_MS=\"${UPLOAD_TIMEOUT_MS}\"
EOF
  npm ci
  npm run build
"

log "Creating systemd service: reference-api"
cat > /etc/systemd/system/reference-api.service <<EOF
[Unit]
Description=Reference Section Quality API
After=network.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${REFERENCE_DIR}/REFRENCE-SECTION
EnvironmentFile=-${REFERENCE_DIR}/REFRENCE-SECTION/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=${REFERENCE_DIR}/REFRENCE-SECTION/.venv/bin/uvicorn api:app --host 127.0.0.1 --port ${REFERENCE_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

log "Creating systemd service: fixmypaper-backend"
cat > /etc/systemd/system/fixmypaper-backend.service <<EOF
[Unit]
Description=FixMyPaper Backend
After=network.target reference-api.service
Requires=reference-api.service

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${MAIN_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=REFERENCE_API_URL=http://127.0.0.1:${REFERENCE_PORT}/analyze
ExecStart=${MAIN_DIR}/.venv/bin/gunicorn app:app --bind 127.0.0.1:${BACKEND_PORT} --workers 1 --timeout 1800
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

log "Creating systemd service: fixmypaper-frontend"
cat > /etc/systemd/system/fixmypaper-frontend.service <<EOF
[Unit]
Description=FixMyPaper Frontend
After=network.target fixmypaper-backend.service
Requires=fixmypaper-backend.service

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${MAIN_DIR}/frontend
Environment=NODE_ENV=production
Environment=BACKEND_INTERNAL_URL=http://127.0.0.1:${BACKEND_PORT}
Environment=UPLOAD_PROXY_TIMEOUT_MS=${UPLOAD_TIMEOUT_MS}
ExecStart=/usr/bin/env node node_modules/next/dist/bin/next start -p ${FRONTEND_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

log "Configuring Nginx"
cat > /etc/nginx/sites-available/fixmypaper <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    client_max_body_size 60M;

    location / {
        proxy_pass http://127.0.0.1:${FRONTEND_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 1800;
        proxy_send_timeout 1800;
    }
}
EOF

ln -sf /etc/nginx/sites-available/fixmypaper /etc/nginx/sites-enabled/fixmypaper
rm -f /etc/nginx/sites-enabled/default
nginx -t

log "Reloading services"
systemctl daemon-reload
systemctl enable --now reference-api
systemctl enable --now fixmypaper-backend
systemctl enable --now fixmypaper-frontend
systemctl enable --now nginx
systemctl restart reference-api
systemctl restart fixmypaper-backend
systemctl restart fixmypaper-frontend
systemctl restart nginx

if command -v ufw >/dev/null 2>&1; then
  log "Configuring firewall (ufw)"
  ufw allow OpenSSH || true
  ufw allow 'Nginx Full' || true
  ufw --force enable || true
fi

log "Running health checks"
curl -fsS "http://127.0.0.1:${REFERENCE_PORT}/health" >/dev/null
curl -fsS "http://127.0.0.1:${BACKEND_PORT}/health" >/dev/null
curl -fsSI "http://127.0.0.1:${FRONTEND_PORT}" >/dev/null

PUBLIC_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

cat <<EOF

Deployment complete.

Reference API health:
  http://127.0.0.1:${REFERENCE_PORT}/health

Main backend health:
  http://127.0.0.1:${BACKEND_PORT}/health

Frontend local:
  http://127.0.0.1:${FRONTEND_PORT}

Frontend public (if firewall/network allow):
  http://${PUBLIC_IP:-YOUR_SERVER_IP}

Useful logs:
  sudo journalctl -u reference-api -f
  sudo journalctl -u fixmypaper-backend -f
  sudo journalctl -u fixmypaper-frontend -f
  sudo tail -f /var/log/nginx/error.log

EOF
