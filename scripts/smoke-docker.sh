#!/usr/bin/env bash
set -euo pipefail

project_name="zenstream-orchestrator-smoke-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
port="${ORCHESTRATOR_SMOKE_PORT:-19088}"
run_root="$(mktemp -d "${TMPDIR:-/tmp}/zenstream-orchestrator-smoke.XXXXXX")"

cleanup() {
  result=$?
  trap - EXIT
  if (( result != 0 )); then
    docker compose --project-name "$project_name" logs --no-color orchestrator 2>/dev/null \
      | sed -E 's/Password: .*/Password: [redacted]/' >&2 || true
  fi
  cleanup_image="$(docker compose --project-name "$project_name" images --quiet orchestrator 2>/dev/null | head -n 1 || true)"
  docker compose --project-name "$project_name" down --remove-orphans >/dev/null 2>&1 || true
  if [[ -n "$cleanup_image" ]]; then
    docker run --rm --user 0:0 --volume "$run_root:/smoke" --entrypoint python "$cleanup_image" \
      -c 'import pathlib, shutil
root=pathlib.Path("/smoke")
for entry in root.iterdir():
    if entry.is_dir() and not entry.is_symlink():
        shutil.rmtree(entry)
    else:
        entry.unlink()' >/dev/null 2>&1 || true
  fi
  if ! rm -rf -- "$run_root"; then
    echo "Could not remove the Orchestrator smoke directory." >&2
    if (( result == 0 )); then
      result=1
    fi
  fi
  exit "$result"
}
trap cleanup EXIT

mkdir -p "$run_root/library" "$run_root/metadata"
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(64))')"
export LIBRARY_PATH="$run_root/library"
export METADATA_PATH="$run_root/metadata"
export ORCHESTRATOR_PORT="$port"

docker compose --project-name "$project_name" up --build --detach orchestrator

url="http://127.0.0.1:${port}/health/ready"
ready=false
deadline=$((SECONDS + 180))
while (( SECONDS < deadline )); do
  if response="$(curl --silent --connect-timeout 2 --max-time 4 --output /dev/null --write-out '%{http_code}' "$url" 2>/dev/null)" \
    && [[ "$response" == "200" ]]; then
    container_id="$(docker compose --project-name "$project_name" ps --quiet orchestrator)"
    health_status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$container_id")"
    if [[ "$health_status" == "healthy" ]]; then
      ready=true
      break
    fi
  fi

  container_state="$(docker compose --project-name "$project_name" ps --format '{{.State}}' orchestrator 2>/dev/null || true)"
  case "${container_state,,}" in
    exited*|dead*)
      echo "Orchestrator production container stopped before becoming ready." >&2
      exit 1
      ;;
  esac
  sleep 2
done

if [[ "$ready" != true ]]; then
  echo "Orchestrator production container did not become ready within 180 seconds." >&2
  exit 1
fi

docker compose --project-name "$project_name" exec -T orchestrator python -c \
  "import sqlite3; connection=sqlite3.connect('/app/sqlite/orchestrator.db'); rows=connection.execute('SELECT version_num FROM alembic_version').fetchall(); assert len(rows) == 1 and rows[0][0], rows; print('SQLite initialized at Alembic revision', rows[0][0])"
echo "Orchestrator production container passed the SQLite readiness smoke."
