# Cloud - CloudSys, groupe 09

Backend Cloud du projet : réception des métriques et journées dégradées,
stockage persistant, réentraînement LightGBM, suivi MLflow et accès aux modèles
publiés. Responsable : Alan Fogliaresi (étudiant 2).

## Services

| Service | Rôle | Accès local |
|---|---|---|
| FastAPI | Authentification, ingestion et téléchargement des modèles | http://localhost:8000 ; Swagger : http://localhost:8000/docs |
| PostgreSQL | Métriques, index des batches, entraînements et versions | Réseau Docker uniquement |
| MinIO | Batches, modèles et artefacts | Console : http://localhost:9001 ; API S3 : http://localhost:9000 |
| MLflow | Suivi des exécutions et registre des modèles | http://localhost:5000 |
| Trainer | Suit les journées replayées et entraîne des candidats lorsque les règles de déclenchement sont satisfaites | Aucun port public |

## Prérequis

Docker avec conteneurs Linux et Docker Compose compatible avec les contextes de
build additionnels. Les ressources du cours doivent être à côté du dépôt :

```text
Project 4/
  cloudsys-p4-group09/cloud/
  cloudsys-p4-students-main/kit/
  cloudsys-p4-students-main/data/
```

Le trainer copie le kit et les données dans son image. Adapter les chemins du
Compose si cette disposition diffère. Toutes les commandes suivantes partent de
la **racine du dépôt de groupe**.

## Configuration

Créer la configuration une seule fois, sans écraser un fichier existant :

```powershell
Copy-Item cloud/.env.example cloud/.env
```

Remplacer les valeurs fictives : tokens dans `DEVICE_TOKENS_JSON`, identifiants
PostgreSQL et MinIO, puis vérifier `S3_ENDPOINT_URL` et `S3_BUCKET`.
Les Edge et le Cloud doivent utiliser les mêmes associations compteur/token.
L'exemple contient deux compteurs ; compléter les associations selon les besoins.
**Ne pas ajouter le fichier `.env` à Git.**

## Démarrage local de développement

Démarrer le socle et attendre que les services soient prêts. Le premier build
MinIO peut prendre plusieurs minutes. MLflow crée sa base PostgreSQL séparée.

```powershell
docker compose --env-file cloud/.env -f cloud/compose.yaml up --build -d postgres minio api mlflow
docker compose --env-file cloud/.env -f cloud/compose.yaml ps
```

Initialiser les tables, le bucket et les modèles initiaux :

```powershell
docker compose --env-file cloud/.env -f cloud/compose.yaml exec -T api python -m app.database
docker compose --env-file cloud/.env -f cloud/compose.yaml exec -T api python -m app.storage
docker compose --env-file cloud/.env -f cloud/compose.yaml run --rm --no-deps --build models-init
```

Activer ensuite le réentraînement automatique :

```powershell
docker compose --env-file cloud/.env -f cloud/compose.yaml up --build -d trainer
docker compose --env-file cloud/.env -f cloud/compose.yaml logs --tail 20 trainer
```

## Exploitation

Après modification du `.env`, recréer les services concernés : un simple
`restart` ne recharge pas les variables d'environnement.
Les volumes `postgres_data`, `minio_data` et `trainer_state` conservent les données.
Éviter `down -v` pour les préserver ; Git ne remplace pas leur sauvegarde.

Le Compose local expose les ports d'administration sur `127.0.0.1`. Pour le
déploiement sur la VM SWITCH, utiliser l'overlay de production décrit ci-dessous.
Le déploiement Nuvla des Edge est documenté dans [`edge/README.md`](../edge/README.md).

## Déploiement en production sur SWITCH Engine

Le déploiement de production utilise une VM Ubuntu avec Docker Compose, le dépôt
du projet, le dépôt de cours en lecture seule et un volume persistant monté sous
`/srv/p4`. Le guide [cloud/deploy/README.md](deploy/README.md) couvre le
provisionnement, les sauvegardes et la restauration. La configuration réelle
reste dans `cloud/.env`; ne jamais la copier dans Git ou dans une vidéo.

Depuis la racine de `cloudsys-p4-group09` sur la VM :

```sh
chmod 600 cloud/.env
docker compose --env-file cloud/.env \
  -f cloud/compose.yaml -f cloud/compose.prod.yaml \
  up -d --build
docker compose --env-file cloud/.env \
  -f cloud/compose.yaml -f cloud/compose.prod.yaml ps
```

L'overlay démarre Caddy sur les ports publics 80/443, conserve PostgreSQL et
MinIO sous `/srv/p4`, et laisse l'API, MinIO et MLflow liés à l'interface locale
de la VM. Le pare-feu SWITCH doit autoriser 80/443 et SSH depuis le réseau
d'administration. Caddy ne publie que les routes `/v1/*` de l'API, à l'adresse
`https://<API_HOSTNAME>` configurée dans `.env`.

### Caddy : HTTPS et reverse proxy

Le service Caddy est le seul conteneur publié sur Internet. Il termine le TLS
HTTPS et relaie les requêtes `https://<API_HOSTNAME>/v1/*` vers le service
FastAPI `api:8000` sur le réseau Docker. Toute autre route renvoie `404`; les
interfaces Swagger, MLflow, MinIO et PostgreSQL ne sont pas publiées par Caddy.
MLflow et MinIO restent accessibles via le tunnel SSH ci-dessous.

Pour que Caddy obtienne et renouvelle automatiquement son certificat Let's
Encrypt, `API_HOSTNAME` doit résoudre vers l'adresse publique de la VM et le
pare-feu doit laisser entrer le trafic TCP sur les ports 80 et 443. Les données
de certificat et la configuration persistante de Caddy sont conservées dans les
volumes `caddy_data` et `caddy_config`.

Pour diagnostiquer un problème HTTPS ou appliquer une modification du
`Caddyfile`, consulter les logs puis redémarrer uniquement Caddy :

```sh
docker compose --env-file cloud/.env \
  -f cloud/compose.yaml -f cloud/compose.prod.yaml logs --tail 50 caddy
docker compose --env-file cloud/.env \
  -f cloud/compose.yaml -f cloud/compose.prod.yaml restart caddy
```

Le fichier de routage est [`cloud/Caddyfile`](Caddyfile). Après son changement,
vérifier que l'API répond toujours sur `https://<API_HOSTNAME>/v1/health`.

Vérifier l'état et l'API avec un jeton de compteur valide, sans afficher le
jeton :

```sh
docker compose --env-file cloud/.env \
  -f cloud/compose.yaml -f cloud/compose.prod.yaml ps
curl -sS -H "Authorization: Bearer <DEVICE_TOKEN>" \
  "https://<API_HOSTNAME>/v1/health"
```

La réponse attendue est `{"status":"ok"}`. Les routes des modèles sont
`/v1/models/cons/latest`, `/v1/models/pros/latest` et leurs routes versionnées.
Les jetons sont dans `DEVICE_TOKENS_JSON`; chaque jeton correspond à un compteur.

Pour accéder aux consoles privées depuis le poste d'administration, ouvrir un
tunnel SSH vers la VM (remplacer l'adresse si la VM change) :

```sh
ssh -i ~/.ssh/p4_group09_cloud -N \
  -L 5000:127.0.0.1:5000 \
  -L 9000:127.0.0.1:9000 \
  -L 9001:127.0.0.1:9001 \
  ubuntu@86.119.46.71
```

Puis ouvrir MLflow sur `http://localhost:5000` ou la console MinIO sur
`http://localhost:9001`. Utiliser `MINIO_ROOT_USER` et `MINIO_ROOT_PASSWORD`
depuis `cloud/.env`; l'API S3 locale au tunnel est `http://127.0.0.1:9000`.

Après une modification de `.env` ou des images, relancer `up -d` avec les mêmes
fichiers Compose afin de recréer les services concernés. Les données résident
sur `/srv/p4` et dans `trainer_state`; ne pas exécuter `docker compose down -v`
sur le déploiement de production. Pour arrêter les services sans supprimer les
données, utiliser `docker compose ... down` sans `-v`.

## Documentation détaillée

- [Stockage des batches, validation et idempotence](BATCH_STORAGE.md)
- [Import initial, endpoints et téléchargement des modèles](MODEL_ACCESS.md)
- [Déclenchement, entraînement et tests du worker](TRAINING.md)
- [Suivi MLflow, registre et publication](MLFLOW.md)

Ces documents décrivent aussi les étapes historiques de développement ; les
mentions « hors de ce bloc » concernent l'étape documentée.
