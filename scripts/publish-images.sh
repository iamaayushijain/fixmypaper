#!/usr/bin/env bash
set -euo pipefail

# Builds and pushes prebuilt images for backend and frontend.
# Run this on CI or a trusted build machine, not on the college server.

REGISTRY="${REGISTRY:-ghcr.io}"
IMAGE_NAMESPACE="${IMAGE_NAMESPACE:-06hardik}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)}"
PLATFORM="${PLATFORM:-linux/amd64}"

BACKEND_INTERNAL_URL="${BACKEND_INTERNAL_URL:-http://backend:7860}"
UPLOAD_PROXY_TIMEOUT_MS="${UPLOAD_PROXY_TIMEOUT_MS:-900000}"

BACKEND_IMAGE="${REGISTRY}/${IMAGE_NAMESPACE}/fixmypaper-backend:${IMAGE_TAG}"
FRONTEND_IMAGE="${REGISTRY}/${IMAGE_NAMESPACE}/fixmypaper-frontend:${IMAGE_TAG}"

printf "\n[publish] Using tag: %s\n" "${IMAGE_TAG}"
printf "[publish] Backend image: %s\n" "${BACKEND_IMAGE}"
printf "[publish] Frontend image: %s\n" "${FRONTEND_IMAGE}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required"
  exit 1
fi

if ! docker buildx version >/dev/null 2>&1; then
  echo "Docker Buildx is required"
  exit 1
fi

printf "\n[publish] Building backend image...\n"
docker buildx build \
  --platform "${PLATFORM}" \
  -f Dockerfile.backend \
  -t "${BACKEND_IMAGE}" \
  --push \
  .

printf "\n[publish] Building frontend image...\n"
docker buildx build \
  --platform "${PLATFORM}" \
  -f frontend/Dockerfile \
  --build-arg BACKEND_INTERNAL_URL="${BACKEND_INTERNAL_URL}" \
  --build-arg UPLOAD_PROXY_TIMEOUT_MS="${UPLOAD_PROXY_TIMEOUT_MS}" \
  -t "${FRONTEND_IMAGE}" \
  --push \
  ./frontend

cat <<EOF

[publish] Done.

Set these values on the server .env.server file:
BACKEND_IMAGE=${REGISTRY}/${IMAGE_NAMESPACE}/fixmypaper-backend
FRONTEND_IMAGE=${REGISTRY}/${IMAGE_NAMESPACE}/fixmypaper-frontend
IMAGE_TAG=${IMAGE_TAG}
EOF
