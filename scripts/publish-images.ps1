$ErrorActionPreference = 'Stop'

$Registry = if ($env:REGISTRY) { $env:REGISTRY } else { 'ghcr.io' }
$ImageNamespace = if ($env:IMAGE_NAMESPACE) { $env:IMAGE_NAMESPACE } else { '06hardik' }
$ImageTag = if ($env:IMAGE_TAG) { $env:IMAGE_TAG } else { (git rev-parse --short HEAD).Trim() }
$Platform = if ($env:PLATFORM) { $env:PLATFORM } else { 'linux/amd64' }

$BackendInternalUrl = if ($env:BACKEND_INTERNAL_URL) { $env:BACKEND_INTERNAL_URL } else { 'http://backend:7860' }
$UploadProxyTimeoutMs = if ($env:UPLOAD_PROXY_TIMEOUT_MS) { $env:UPLOAD_PROXY_TIMEOUT_MS } else { '900000' }

$BackendImage = "$Registry/$ImageNamespace/fixmypaper-backend:$ImageTag"
$FrontendImage = "$Registry/$ImageNamespace/fixmypaper-frontend:$ImageTag"

Write-Host "[publish] Using tag: $ImageTag"
Write-Host "[publish] Backend image: $BackendImage"
Write-Host "[publish] Frontend image: $FrontendImage"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  throw "Docker is required"
}

docker buildx version | Out-Null

Write-Host "[publish] Building backend image..."
docker buildx build --platform $Platform -f Dockerfile.backend -t $BackendImage --push .

Write-Host "[publish] Building frontend image..."
docker buildx build --platform $Platform -f frontend/Dockerfile --build-arg BACKEND_INTERNAL_URL=$BackendInternalUrl --build-arg UPLOAD_PROXY_TIMEOUT_MS=$UploadProxyTimeoutMs -t $FrontendImage --push ./frontend

Write-Host "[publish] Done."
Write-Host "Set on server:"
Write-Host "BACKEND_IMAGE=$Registry/$ImageNamespace/fixmypaper-backend"
Write-Host "FRONTEND_IMAGE=$Registry/$ImageNamespace/fixmypaper-frontend"
Write-Host "IMAGE_TAG=$ImageTag"
