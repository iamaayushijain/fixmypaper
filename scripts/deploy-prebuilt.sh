#!/usr/bin/env bash
set -euo pipefail

# Pulls and runs prebuilt images on the deployment server.

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prebuilt.yml}"
ENV_FILE="${ENV_FILE:-.env.server}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required"
  exit 1
fi

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "Missing compose file: ${COMPOSE_FILE}"
  exit 1
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing env file: ${ENV_FILE}"
  echo "Create it from .env.server.example"
  exit 1
fi

printf "\n[deploy] Pulling images...\n"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" pull

printf "\n[deploy] Starting services...\n"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --remove-orphans --pull never

printf "\n[deploy] Service status:\n"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps

cat <<EOF

[deploy] Done.
Frontend: http://localhost:3000
Backend health: http://localhost:5001/health
EOF
