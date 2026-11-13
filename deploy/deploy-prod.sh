#!/usr/bin/env bash
# Production twin of deploy-dev.sh: runs ON the Contabo box from /opt/sribees-supply-prod.
# image and copied this bundle. Usage: deploy-dev.sh <image-tag>
# Order: database up -> migrate -> api -> smoke -> web + backup. A failed
# migration or smoke test stops here with the previous api still serving.
set -Eeuo pipefail
cd "$(dirname "$0")"

IMAGE="$1"
# Kept beside .env so a hand-run `docker compose ... --env-file .env
# --env-file release.env` (logs, exec, restart) names the deployed image.
echo "SUPPLY_IMAGE=$IMAGE" > release.env
COMPOSE=(docker compose -f docker-compose.prod.yml --env-file .env --env-file release.env)

mkdir -p web
# The web container needs something to serve before the first frontend deploy.
if [ ! -e web/current ]; then
  mkdir -p web/releases/empty
  echo '<!doctype html><title>Sribees Supply</title><p>The staff web app is not deployed yet.' > web/releases/empty/index.html
  ln -sfn releases/empty web/current
fi

"${COMPOSE[@]}" up -d --wait postgres
"${COMPOSE[@]}" run --rm --no-deps api alembic upgrade head

"${COMPOSE[@]}" up -d --no-deps api
for i in $(seq 1 30); do
  status="$(docker inspect -f '{{.State.Health.Status}}' "$("${COMPOSE[@]}" ps -q api)" 2>/dev/null || true)"
  [ "$status" = healthy ] && break
  sleep 2
done
if [ "$status" != healthy ]; then
  echo "api did not become healthy (last: ${status:-unknown})"
  "${COMPOSE[@]}" logs --tail 80 api
  exit 1
fi

mkdir -p backups && chmod 700 backups
"${COMPOSE[@]}" up -d web backup worker
# web holds the api address from its own start; a restart picks up a new api.
"${COMPOSE[@]}" restart web >/dev/null
"${COMPOSE[@]}" ps

# Keep the five newest images; older ones only take disk.
docker image ls sribees-supply --format '{{.Tag}} {{.CreatedAt}}' \
  | sort -k2 -r | awk 'NR>5 {print $1}' \
  | xargs -r -I{} docker image rm "sribees-supply:{}" >/dev/null 2>&1 || true
echo "deployed $IMAGE"
