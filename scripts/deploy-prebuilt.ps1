$ErrorActionPreference = 'Stop'

$ComposeFile = if ($env:COMPOSE_FILE) { $env:COMPOSE_FILE } else { 'docker-compose.prebuilt.yml' }
$EnvFile = if ($env:ENV_FILE) { $env:ENV_FILE } else { '.env.server' }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  throw "Docker is required"
}

if (-not (Test-Path $ComposeFile)) {
  throw "Missing compose file: $ComposeFile"
}

if (-not (Test-Path $EnvFile)) {
  throw "Missing env file: $EnvFile. Create it from .env.server.example"
}

Write-Host "[deploy] Pulling images..."
docker compose --env-file $EnvFile -f $ComposeFile pull

Write-Host "[deploy] Starting services..."
docker compose --env-file $EnvFile -f $ComposeFile up -d --remove-orphans --pull never

Write-Host "[deploy] Service status:"
docker compose --env-file $EnvFile -f $ComposeFile ps

Write-Host "[deploy] Done."
Write-Host "Frontend: http://localhost:3000"
Write-Host "Backend health: http://localhost:5001/health"
