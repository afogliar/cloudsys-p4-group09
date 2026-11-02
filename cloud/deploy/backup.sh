#!/bin/sh
# Daily logical backup of the Project 4 cloud stack.
#
# Copies to $BACKUP_DIR (default /srv/p4/backups, on the persistent disk):
#   <STAMP>/postgres.sql.gz   main index DB (batches, metrics, model versions)
#   <STAMP>/mlflow.sql.gz     MLflow backend DB (when it exists)
#   <STAMP>/minio/            optional object mirror (MINIO_BACKUP=1)
#
# Usage on the VM (root or a user in the docker group):
#   /opt/p4/cloudsys-p4-group09/cloud/deploy/backup.sh
#
# systemd timer example (no cron dependency):
#   /etc/systemd/system/p4-backup.service
#     [Unit]
#     Description=P4 cloud stack backup
#     [Service]
#     Type=oneshot
#     ExecStart=/opt/p4/cloudsys-p4-group09/cloud/deploy/backup.sh
#   /etc/systemd/system/p4-backup.timer
#     [Unit]
#     Description=Daily P4 backup
#     [Timer]
#     OnCalendar=*-*-* 03:00:00
#     Persistent=true
#     [Install]
#     WantedBy=timers.target
#   systemctl enable --now p4-backup.timer
#
# Secrets are read from cloud/.env only; they are never echoed and never
# hardcoded. The optional MinIO mirror passes credentials to the mc container
# through the MC_HOST_* environment variable of `docker run` (briefly visible
# in the host `ps` output of a single-admin VM; acceptable for this project).
set -eu

CLOUD_DIR="${CLOUD_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
ENV_FILE="${ENV_FILE:-$CLOUD_DIR/.env}"
BACKUP_DIR="${BACKUP_DIR:-/srv/p4/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
MINIO_BACKUP="${MINIO_BACKUP:-0}"
MLFLOW_DATABASE="${MLFLOW_DATABASE:-p4_mlflow}"

[ -f "$ENV_FILE" ] || { echo "backup: missing $ENV_FILE" >&2; exit 1; }

# shellcheck disable=SC1090
. "$ENV_FILE"

# Paths are word-split on purpose; CLOUD_DIR must not contain whitespace
# (true for the documented /opt/p4 layout).
COMPOSE_FILES="-f $CLOUD_DIR/compose.yaml"
[ -f "$CLOUD_DIR/compose.prod.yaml" ] && \
    COMPOSE_FILES="$COMPOSE_FILES -f $CLOUD_DIR/compose.prod.yaml"

compose() {
    # shellcheck disable=SC2086
    docker compose --env-file "$ENV_FILE" $COMPOSE_FILES "$@"
}

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$BACKUP_DIR/$STAMP"
mkdir -p "$TARGET"

PG_ID="$(compose ps -q postgres)"
[ -n "$PG_ID" ] || { echo "backup: postgres container is not running" >&2; exit 1; }

echo "backup: dumping PostgreSQL ($POSTGRES_DB) to $TARGET/postgres.sql.gz"
# --clean/--if-exists make the dump restorable over a populated database.
docker exec "$PG_ID" sh -c \
    'pg_dump --no-owner --clean --if-exists -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    | gzip > "$TARGET/postgres.sql.gz"

if docker exec "$PG_ID" sh -c 'psql -U "$POSTGRES_USER" -lqt' \
        | cut -d '|' -f1 | grep -qw "$MLFLOW_DATABASE"; then
    echo "backup: dumping MLflow DB ($MLFLOW_DATABASE) to $TARGET/mlflow.sql.gz"
    docker exec "$PG_ID" sh -c \
        'pg_dump --no-owner --clean --if-exists -U "$POSTGRES_USER" -d "$MLFLOW_DATABASE"' \
        | gzip > "$TARGET/mlflow.sql.gz"
fi

if [ "$MINIO_BACKUP" = "1" ]; then
    # mc is not shipped in the stack images; run the official client image on
    # the compose network and mirror into the backup directory.
    : "${S3_BUCKET:?S3_BUCKET manquant}"
    : "${MINIO_ROOT_USER:?MINIO_ROOT_USER manquant}"
    : "${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD manquant}"
    NET="$(docker inspect -f \
        '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' \
        "$(compose ps -q minio)")"
    NET="${NET%% *}"
    mkdir -p "$TARGET/minio"
    echo "backup: mirroring MinIO bucket $S3_BUCKET to $TARGET/minio/"
    docker run --rm --network "$NET" -v "$TARGET/minio:/target" \
        -e MC_HOST_p4="http://${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}@minio:9000" \
        minio/mc:latest mirror "p4/${S3_BUCKET}" /target/
fi

# Retention: keep the last RETENTION_DAYS days of timestamped backup dirs.
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '20*' \
    -mtime +"$RETENTION_DAYS" -exec rm -rf {} +

echo "backup: complete -> $TARGET"
