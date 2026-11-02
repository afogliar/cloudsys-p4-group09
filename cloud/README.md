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
| Trainer | Exécution du kit et publication conditionnelle | Aucun port ; profil Compose `training` |

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

## Premier démarrage

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

Le Compose fourni expose les ports sur `127.0.0.1`. Le reverse proxy HTTPS sur
SWITCH et le roll-out Nuvla sont à configurer séparément. Un modèle publié côté
Cloud n'est pas nécessairement actif sur les Edge.

## Documentation détaillée

- [Stockage des batches, validation et idempotence](BATCH_STORAGE.md)
- [Import initial, endpoints et téléchargement des modèles](MODEL_ACCESS.md)
- [Déclenchement, entraînement et tests du worker](TRAINING.md)
- [Suivi MLflow, registre et publication](MLFLOW.md)

Ces documents décrivent aussi les étapes historiques de développement ; les
mentions « hors de ce bloc » concernent l'étape documentée.
