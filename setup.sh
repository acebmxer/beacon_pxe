#!/usr/bin/env bash
# ============================================================================
# Beacon — bootstrap
# Generates .env (admin password, secret key, server IP), then starts the stack.
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'

rand() { LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "${1:-32}"; }

if [[ -f .env ]]; then
  echo -e "${YELLOW}.env already exists — leaving it untouched.${NC}"
else
  echo "Creating .env from .env.example ..."
  cp .env.example .env

  # Auto-generate admin password if left blank.
  GEN_PW=""
  if grep -qE '^ADMIN_PASSWORD=\s*$' .env; then
    GEN_PW="$(rand 20)"
    sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${GEN_PW}|" .env
  fi

  # Session secret.
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(rand 48)|" .env

  # Bake the project directory into .env so self-updates pass the correct
  # --project-directory to docker compose regardless of where it runs from.
  sed -i "s|^PROJECT_DIR=.*|PROJECT_DIR=$(pwd)|" .env

  # Best-effort server IP detection.
  IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}' || true)"
  [[ -n "${IP:-}" ]] && sed -i "s|^SERVER_IP=.*|SERVER_IP=${IP}|" .env

  echo -e "${GREEN}.env created.${NC}"
  if [[ -n "$GEN_PW" ]]; then
    echo -e "${BOLD}=============================================================${NC}"
    echo -e "${BOLD} Auto-generated admin credentials (save these now):${NC}"
    echo -e "   username: ${GREEN}$(grep '^ADMIN_USER=' .env | cut -d= -f2)${NC}"
    echo -e "   password: ${GREEN}${GEN_PW}${NC}"
    echo -e "${BOLD}=============================================================${NC}"
  fi
fi

mkdir -p data data/images

echo "Pulling images and starting containers ..."
docker compose pull
docker compose up -d

echo
echo -e "${GREEN}Done.${NC} Web UI:  http://$(grep '^SERVER_IP=' .env | cut -d= -f2 || echo localhost):$(grep '^WEB_PORT=' .env | cut -d= -f2 || echo 8080)"
echo "If you did not set a password, retrieve it with:  docker compose logs web | grep -i 'admin password'"
