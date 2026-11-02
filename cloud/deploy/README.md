# Cloud VM deployment runbook — Group 09

Operates the **cloud stack** (API, PostgreSQL, MinIO, MLflow, trainer, Caddy)
on the single course cloud VM with Docker Compose. Nuvla/edge rollout is
covered separately by `deploy/nuvla/` and the Nuvla guide; this runbook does
not touch edge VMs.

- Compose files: `cloud/compose.yaml` (+ `cloud/compose.prod.yaml` overlay)
- Configuration: `cloud/.env` (template: `cloud/.env.example`)
- Backup/restore: `deploy/backup.sh`, `deploy/restore.sh`
- Provisioning (OpenStack VM only, run once, not from here): `deploy/provision_cloud.py`

## OPEN items (do not invent values)

| Item | Status | Note |
|---|---|---|
| Public DNS name of the VM | OPEN | `API_HOSTNAME` in `.env`. A `*.sslip.io` name (e.g. `86-119-46-71.sslip.io`) works without a DNS record. |
| TLS method | OPEN | Caddy obtains Let's Encrypt certificates automatically (ports 80/443 open in the security group). If the course mandates a different CA or a pre-provisioned cert, adapt the Caddyfile. |
| MLflow UI public reachability | PRIVATE | MLflow is bound to the cloud VM loopback interface. Use an SSH tunnel for the UI; it is not exposed through Caddy. |
| VM sizing | OPEN | `provision_cloud.py` uses `m1.large` (4 vCPU / 8 GB). Spec allows 2–4 vCPU / 8 GB; confirm quota. |
| `NUVLA_DEPLOYMENT_GROUP_ID` | OPEN | Owned by the Nuvla/edge workstream; trainer needs it at start. |
| Persistent-disk size | OPEN | Provision script defaults to 50 GB on `ceph-standard`; confirm quota. |

## 1. One-time VM setup

```sh
# 1. VM provisioned beforehand (out of scope here): Ubuntu 24.04, security
#    group 22/<admin-CIDR> + 80,443 public, persistent volume mounted.
# 2. Install Docker Engine + compose plugin:
curl -fsSL https://get.docker.com | sh
# 3. Persistent storage layout (volume mounted at /srv/p4):
sudo mkdir -p /srv/p4/postgres /srv/p4/minio /srv/p4/backups
# 4. Repositories side by side (bind defaults expect the sibling layout):
sudo mkdir -p /opt/p4 && cd /opt/p4
sudo git clone <group-repo-url> cloudsys-p4-group09          # branch cloud/deployment
sudo git clone <course-repo-url> cloudsys-p4-students-main   # read-only input, never modified
#    (or copy kit/ and data/ elsewhere and set P4_KIT_DIR / P4_DATA_DIR /
#     P4_COURSE_DIR in .env — all three support absolute paths)
# 5. Configuration:
cd cloudsys-p4-group09/cloud
cp .env.example .env
#    Fill in: DEVICE_TOKENS_JSON (all 10 meters), POSTGRES_PASSWORD,
#    MINIO_ROOT_PASSWORD, API_HOSTNAME, and NUVLA_* when rollout is enabled.
#    NOTE: the current cloud/.env predates S3_* — make sure S3_ENDPOINT_URL
#    and S3_BUCKET are present (values from .env.example).
```

Secrets live only in `cloud/.env` (mode 600 recommended: `chmod 600 .env`).
Never commit it; never print token values in logs or tickets.

## 2. First deploy

From the repository root (`/opt/p4/cloudsys-p4-group09`):

```sh
docker compose -f cloud/compose.yaml -f cloud/compose.prod.yaml up -d --build
```

What happens automatically (no manual init step):

1. `postgres` becomes healthy; `minio` starts.
2. `db-init` (one-shot) creates all index tables.
3. `models-init` (one-shot) creates the bucket, imports kit models as `v0001` for `cons`/`pros`.
4. `data-init` (one-shot) ingests the provided data into `base/` (idempotency depends on `app.data_init` — see Risks).
5. `api` → healthy; `mlflow` → healthy; `trainer` starts its polling loop.
6. `caddy` starts once the API is healthy and obtains a certificate for
   `API_HOSTNAME` (Let's Encrypt HTTP-01, needs port 80).

Check:

```sh
docker compose -f cloud/compose.yaml -f cloud/compose.prod.yaml ps
docker compose -f cloud/compose.yaml -f cloud/compose.prod.yaml logs --tail 20 db-init models-init data-init
```

## 3. Verify health

```sh
# Liveness through the public endpoint (token = one value from
# DEVICE_TOKENS_JSON in cloud/.env; NEVER paste the real value anywhere):
curl -sS -H "Authorization: Bearer <device-token ****masked****>" \
     "https://${API_HOSTNAME}/v1/health"
# expected: {"status":"ok"}
```

Smoke checklist:

```sh
cd cloudsys-p4-group09
DC="docker compose -f cloud/compose.yaml -f cloud/compose.prod.yaml --env-file cloud/.env"

# PostgreSQL tables (expect daily_metrics, lowperf_batches, model_versions,
# model_releases; training_runs appears once the trainer has initialised):
$DC exec postgres psql -U p4 -d p4 -c '\dt'

# MinIO keys (expect models/cons/v0001/model.txt, models/pros/v0001/model.txt,
# and base/... once data-init has run):
$DC exec api python -c "from app.storage import create_s3_client,get_bucket_name; c=create_s3_client(); [print(o['Key']) for o in c.list_objects_v2(Bucket=get_bucket_name()).get('Contents',[])]"

# MLflow UI is private; use an SSH tunnel to localhost:5000 when needed:
# ssh -L 5000:127.0.0.1:5000 <cloud-vm>
# Then open http://127.0.0.1:5000/

# Trainer: JSON lines, one object per poll (idle/completed), no tracebacks:
$DC logs --tail 20 trainer

# API: no stack traces on startup, health 200 above:
$DC logs --tail 20 api
```

Exposure check (only Caddy public; everything else loopback or internal):

```sh
$DC ps --format '{{.Name}} {{.Publishers}}'
# expected: caddy 0.0.0.0:80,0.0.0.0:443; api/minio/mlflow 127.0.0.1:*; others none
```

## 4. Update

```sh
git pull
docker compose -f cloud/compose.yaml -f cloud/compose.prod.yaml up -d --build
```

One-shot inits re-run only if their configuration changed; they are idempotent
(`CREATE TABLE IF NOT EXISTS`, hash-checked immutable v0001 models). Data
surives: PostgreSQL and MinIO are on `/srv/p4` binds, trainer state on the
named volume `trainer_state`.

## 5. Backup and restore

Daily backup (PostgreSQL + optional MinIO mirror) with retention:

```sh
/opt/p4/cloudsys-p4-group09/cloud/deploy/backup.sh                 # pg_dump only
MINIO_BACKUP=1 /opt/p4/cloudsys-p4-group09/cloud/deploy/backup.sh  # + mc mirror
```

Schedule with the systemd timer example in the header of `backup.sh`
(`systemctl enable --now p4-backup.timer`). Restore (overwrites the DB,
5-second abort window):

```sh
/opt/p4/cloudsys-p4-group09/cloud/deploy/restore.sh /srv/p4/backups/<STAMP>
```

**Test the restore** after the first real deploy (required by the course):
run a backup, then restore it, and re-run the §3 smoke checks. MinIO restore
command is documented in `restore.sh`.

## 6. Deployment manifest (fill in after first deploy)

Record (names only, never secret values): image tags/digests (`docker compose
... images --digests`), Compose file versions, VM size/flavor, API base URL
(`https://<API_HOSTNAME>`), storage endpoint/bucket/prefix
(`http://minio:9000`, `S3_BUCKET`, `base/`, `lowperf/`, `models/`),
Nuvla app/deployment-group IDs, initial model versions (`cons`/`pros` v0001),
and secret NAMES (`DEVICE_TOKENS_JSON`, `POSTGRES_PASSWORD`,
`MINIO_ROOT_PASSWORD`, `NUVLA_API_KEY`, `NUVLA_API_SECRET`).

## 7. Troubleshooting

- `caddy` restarts with a TLS error → check DNS points at the VM and ports 80/443 are open in the security group; certificates cache in `caddy_data`.
- `models-init`/`data-init` exit non-zero → kit/data bind paths wrong: check `P4_KIT_DIR`, `P4_DATA_DIR` (`bind.create_host_path: false` fails fast on a missing source).
- `data-init` fails with "no module named app.data_init" → the API-side module has not landed yet; coordinate with the API workstream.
- API 503 on model routes → `models-init` did not complete; check its logs.
- Trainer logs `initialization_error` → usually PostgreSQL/MinIO not ready or `NUVLA_*` missing; check `.env`.
