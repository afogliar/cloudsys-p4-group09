# Stockage et idempotence des batches

Ce bloc concerne uniquement `POST /v1/lowperf`. Il conserve le JSON dans MinIO
et son index dans PostgreSQL. Le schéma existant et l'authentification par
compteur sont utilisés avant toute écriture. Les métriques quotidiennes ne
changent pas. Aucun entraînement, registre MLflow ou roll-out n'est ajouté.

## Installation dans l'environnement de développement

Depuis la racine du dépôt, avec les services PostgreSQL et MinIO déjà configurés :

```powershell
docker compose --env-file cloud/.env -f cloud/compose.yaml up --build -d api postgres minio
docker compose --env-file cloud/.env -f cloud/compose.yaml exec -T api python -m app.database
docker compose --env-file cloud/.env -f cloud/compose.yaml exec -T api python -m app.storage
```

L'initialisation ajoute la table `lowperf_batches` sans effacer `daily_metrics`.
Il faut initialiser les tables et le bucket avant les premiers envois.
`POSTGRES_HOST` et `POSTGRES_PORT` sont facultatifs (défauts `postgres`, `5432`).

## Contrat HTTP

| Situation | HTTP | Réponse |
| --- | --- | --- |
| Batch finalisé pour la première fois, éventuellement après reprise | 201 | `status: created`, `stored: true`, `batch_id` |
| Réessai du même batch déjà finalisé | 200 | `status: already_stored`, `stored: true`, `batch_id` |
| Même identifiant, contenu différent | 409 | Message de conflit |
| Stockage PostgreSQL ou S3 indisponible | 503 | Message générique, `Retry-After: 5` |
| JSON invalide | 422 | Erreurs de validation existantes |
| Token absent/invalide ou mauvais compteur | 401/403 | Erreur d'accès existante |

L'edge doit créer `batch_id` et `sent_at` une fois à la détection, persister le
batch dans son outbox, puis réenvoyer son contenu sans modification. L'ordre des
clés JSON et les espaces ne comptent pas : le hash est calculé après validation
et sérialisation canonique. Les valeurs, notamment `sent_at`, comptent.
En cas de 503, de timeout ou de réponse perdue, l'edge conserve le batch et
réessaie avec temporisation. Un 409 doit être investigué, pas contourné en
générant automatiquement un nouvel identifiant.

## Écritures et reprise

1. Une transaction réserve durablement `batch_id`, le hash SHA-256 et la clé
   objet dans PostgreSQL avec l'état `pending`.
2. Une autre transaction verrouille cette ligne avec `SELECT ... FOR UPDATE`.
   Deux envois du même identifiant sont ainsi sérialisés, tandis que les autres
   identifiants peuvent avancer indépendamment.
3. Un hash différent entraîne un conflit. Un batch déjà `stored` n'est pas
   réécrit dans MinIO.
4. Pour un batch `pending`, l'API écrit le JSON à la clé déterministe :

```text
lowperf/category=cons/device=27afc1ed/day=2026-04-01/<batch_id>.json
```

5. L'API passe la ligne à `stored`, renseigne `stored_at`, valide la transaction,
   puis répond. Elle ne confirme jamais une réussite avant le commit.

Une panne avant l'upload laisse une réservation `pending`. Une panne après
l'upload mais avant le commit peut laisser un objet associé à une ligne
`pending`. Le prochain envoi identique réécrit la même clé et finalise la ligne.
Le hash durable empêche une reprise avec un contenu différent, même après panne.
La réservation ou l'attente du verrou est limitée à 5 secondes ; un dépassement
retourne 503 pour laisser l'edge réessayer.

PostgreSQL et MinIO n'ont pas de transaction distribuée. La reprise dépend de
l'outbox edge : il n'y a pas ici de job de réparation autonome. Une suppression
manuelle d'objet après finalisation n'est pas réparée par un réessai retournant
200 ; sauvegardes et contrôle d'intégrité restent des sujets d'exploitation.

**Le futur trainer doit sélectionner uniquement `status = 'stored'` et charger
les objets désignés par l'index**, pas parcourir indistinctement tout le bucket.
Les réservations `pending` ne comptent pas dans le déclencheur. `stored_at` est
une heure réelle de réception complète, pas l'horloge simulée du replay.

```sql
SELECT batch_id, device_id, category, day, status, stored_at, object_key
FROM lowperf_batches
ORDER BY received_at;
```

## Tests automatisés

La suite HTTP utilise des doubles de stockage pour vérifier les codes, les
contrôles avant écriture et les erreurs sans divulguer de diagnostics internes.
Elle ne prouve pas à elle seule la persistance.

```powershell
cd cloud/api
python -m pip install -r requirements-test.txt
python -m pytest -q tests/test_lowperf_http.py
```

La suite complète utilise de vrais PostgreSQL et MinIO dans un projet Compose
séparé, avec identifiants fictifs, sans `.env`, sans ports publiés et sans les
volumes du projet. Elle utilise l'image MinIO locale déjà construite.
**Lancer depuis la racine du dépôt avec le nom de projet indiqué :**

```powershell
docker compose -p group09-batch-tests -f cloud/compose.batch-tests.yaml up --build --abort-on-container-exit --exit-code-from batch-tests
```

Le processus `batch-tests` doit terminer avec le code 0. Les tests couvrent :

- premier envoi 201, réessai 200, une ligne et un objet de contenu exact ;
- identifiant réutilisé avec un contenu différent : 409 ;
- quatre envois concurrents identiques : une seule finalisation ;
- interruption avant et après l'écriture S3, puis reprise ;
- interruption avant le commit final, puis reprise ;
- indisponibilité de PostgreSQL et rejet de JSON invalide sans écriture.

Les fixtures nettoient exclusivement la base `p4_batch_tests` et le bucket
`p4-batch-tests`, avec un contrôle des noms avant nettoyage. Les données de test
sont en mémoire temporaire et disparaissent à l'arrêt des conteneurs concernés.
Pour retirer uniquement cette pile de test :

```powershell
docker compose -p group09-batch-tests -f cloud/compose.batch-tests.yaml down
```

Cette suite injecte des erreurs de panne dans le processus API ; elle ne remplace
pas les scénarios R1/R2 complets sur les cinq edges avec leur outbox réelle.

## Vérifications effectuées le 1 novembre 2026

- Syntaxe de tous les fichiers Python et contrôle `git diff --check` : réussis.
- Validation de `compose.batch-tests.yaml` avec `docker compose config --quiet` : réussie.
- Tests HTTP locaux : **10 réussis**. Exécution avec Python 3.12, FastAPI 0.115.12,
  Pydantic 2.11.7, Psycopg 3.2.9 et Boto3 1.38.46 dans un dossier temporaire,
  sans modification des dépendances installées du projet.
- MinIO réel accessible sur localhost : création d'un bucket temporaire, double
  initialisation, double écriture à la même clé, relecture exacte et un seul
  objet : réussis. Le bucket temporaire a été supprimé, sans modification de
  `p4-group09`.
- **8 tests d'intégration PostgreSQL/MinIO non exécutés dans cette session** :
  le moteur Docker refuse l'accès au canal nommé Windows. Ils ont été ignorés
  explicitement lors de l'exécution locale. La validation finale sur Python 3.11
  et les services réels reste à effectuer avec la commande Compose ci-dessus.

Ces résultats ne prouvent donc pas encore les garanties de concurrence et de
reprise sur PostgreSQL réel ; elles sont couvertes par la suite à exécuter.
