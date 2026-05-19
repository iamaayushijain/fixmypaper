# Ubuntu Final Pull And Run (Prebuilt Images)

This runbook is for Ubuntu server only.
Use it when images are already published to GHCR and the server should only pull and run.

## What You Need

1. Repository files on server:
	1. docker-compose.prebuilt.yml
	2. .env.server.example
	3. scripts/deploy-prebuilt.sh
2. Published image tag (example: v2026.04.09)
3. GitHub token with read:packages scope for GHCR pull

## One-Time Ubuntu Setup

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

# Re-login once so docker group applies, then continue.
```

## Configure Runtime Env On Server

```bash
cd /opt/fixmypaper
cp .env.server.example .env.server
nano .env.server
```

Set these values in .env.server:

```env
BACKEND_IMAGE=ghcr.io/06hardik/fixmypaper-backend
FRONTEND_IMAGE=ghcr.io/06hardik/fixmypaper-frontend
IMAGE_TAG=v2026.04.09
REFERENCE_API_URL=https://reference-api.onrender.com/analyze
BACKEND_INTERNAL_URL=http://backend:7860
UPLOAD_PROXY_TIMEOUT_MS=900000
```

## Final Pull And Run (Ubuntu)

```bash
cd /opt/fixmypaper

# GHCR login for pulling images
docker login ghcr.io -u 06hardik

# Pull exact images from registry
docker compose --env-file .env.server -f docker-compose.prebuilt.yml pull

# Start using only local pulled images (avoids extra registry lookup)
docker compose --env-file .env.server -f docker-compose.prebuilt.yml up -d --remove-orphans --pull never

# Check status
docker compose --env-file .env.server -f docker-compose.prebuilt.yml ps
```

## Health Checks

```bash
curl http://localhost:5001/health
curl -I http://localhost:3000
```

## Update To New Version

```bash
cd /opt/fixmypaper
sed -i 's/^IMAGE_TAG=.*/IMAGE_TAG=v2026.04.10/' .env.server
docker compose --env-file .env.server -f docker-compose.prebuilt.yml pull
docker compose --env-file .env.server -f docker-compose.prebuilt.yml up -d --remove-orphans --pull never
docker compose --env-file .env.server -f docker-compose.prebuilt.yml ps
```

## Rollback

```bash
cd /opt/fixmypaper
sed -i 's/^IMAGE_TAG=.*/IMAGE_TAG=v2026.04.09/' .env.server
docker compose --env-file .env.server -f docker-compose.prebuilt.yml pull
docker compose --env-file .env.server -f docker-compose.prebuilt.yml up -d --remove-orphans --pull never
docker compose --env-file .env.server -f docker-compose.prebuilt.yml ps
```

## Optional Wrapper Script

If you prefer the project script:

```bash
cd /opt/fixmypaper
chmod +x scripts/deploy-prebuilt.sh
./scripts/deploy-prebuilt.sh
```
