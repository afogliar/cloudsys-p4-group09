#!/bin/sh
# Restore the Project 4 cloud stack from a backup directory made by backup.sh.
#
# Usage (on the VM):
#   /opt/p4/cloudsys-p4-group09/cloud/deploy/restore.sh /srv/p4/backups/<STAMP>
#
# The dumps contain DROP+CREATE statements (pg_dump --clean --if-exists), so a
# restore OVERWRITES the current database content. The API and trainer are
# stopped during the restore and restarted afterwards. A 5-second abort window
# protects against running it by accident.
#
# MinIO objects are NOT touched automatically. To restore the object store
# from a mirror (see backup.sh, MINIO_BACKUP=1):
#   docker run --rm --network cloud_default \
#     -e MC_HOST_p4="http://<MINIO_ROOT_USER>:<MINIO_ROOT_PASSWORD>@minio:9000" \
#     -v /srv/p4/backups/<STAMP>/minio:/source \
#     minio/mc:latest mirror --overwrite /source "p4/<S3_BUCKET>"
#
# Secrets are read from cloud/.env only; they are never echoed or hardcoded.
set -eu

CLOUD_DIR="${CLOUD_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
ENV_FILE="${ENV_FILE:-$CLOUD_DIR/.env}"
MLFLOW_DATABASE="${MLFLOW_DATABASE:-p4_mlflow}"

[ "$#" -eq 1 ] || { echo "usage: $0 <backup-dir>" >&2; exit 2; }
SRC="$1"
[ -f "$ENV_FILE" ] || { echo "restore: missing $ENV_FILE" >&2; exit 1; }
[ -f "$SRC/postgres.sql.gz" ] || \
    { echo "restore: no postgres.sql.gz in $SRC" >&2; exit 1; }

# shellcheck disable=SC1090
. "$ENV_FILE"

COMPOSE_FILES="-f $CLOUD_DIR/compose.yaml"
[ -f "$CLOUD_DIR/compose.prod.yaml" ] && \
    COMPOSE_FILES="$COMPOSE_FILES -f $CLOUD_DIR/compose.prod.yaml"

compose() {
    # shellcheck disable=SC2086
    docker compose --env-file "$ENV_FILE" $COMPOSE_FILES "$@"
}

PG_ID="$(compose ps -q postgres)"
[ -n "$PG_ID" ] || { echo "restore: postgres container is not running" >&2; exit 1; }

echo "restore: will OVERWRITE PostgreSQL ($POSTGRES_DB) with $SRC/postgres.sql.gz"
printf 'restore: continuing in 5s - press Ctrl-C to abort\n'
sleep 5

echo "restore: stopping API and trainer (writers of $POSTGRES_DB)"
compose stop api trainer

echo "restore: loading $POSTGRES_DB"
gunzip -c "$SRC/postgres.sql.gz" | docker exec -i "$PG_ID" sh -c \
    'psql --set ON_ERROR_STOP=1 --quiet -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    >/dev/null

if [ -f "$SRC/mlflow.sql.gz" ]; then
    echo "restore: stopping mlflow and loading $MLFLOW_DATABASE"
    compose stop mlflow
    gunzip -c "$SRC/mlflow.sql.gz" | \
        docker exec -i -e MLFLOW_DB_NAME="$MLFLOW_DATABASE" "$PG_ID" sh -c \
            'psql --set ON_ERROR_STOP=1 --quiet -U "$POSTGRES_USER" -d "$MLFLOW_DB_NAME"' \
        >/dev/null
fi

echo "restore: restarting API, trainer and (if stopped) mlflow"
compose start api trainer
if [ -f "$SRC/mlflow.sql.gz" ]; then
    compose start mlflow
fi

echo "restore: complete. Verify:"
echo "  curl -sS -H 'Authorization: Bearer <device-token>' \"https://\${API_HOSTNAME}/v1/health\""
echo "  docker compose $COMPOSE_FILES logs --tail 20 api trainer"
