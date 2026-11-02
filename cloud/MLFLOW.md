# Suivi MLflow et publication des candidats

MLflow 3.16.1 est un service distinct de l'API et du trainer. Ses métadonnées
sont dans la base PostgreSQL dédiée `p4_mlflow` sur le même serveur local ; ses
artefacts sont dans `s3://p4-group09/mlflow`, servis par proxy via MLflow.
Le client `mlflow-skinny` du trainer est fixé à la même version. Les contraintes
ML du kit sont toujours appliquées et les 30 vecteurs restent vérifiés au build.

## Démarrage local

```powershell
docker compose --env-file cloud/.env -f cloud/compose.yaml up --build -d mlflow trainer
```

Interface : http://localhost:5000. Le port est publié uniquement sur 127.0.0.1.
La liste d'hôtes autorisés est explicite. Il n'y a pas d'authentification propre
à MLflow dans cet environnement local : avant un accès partagé sur la VM,
l'étudiant 3 devra appliquer les contrôles d'accès du déploiement. L'API edge
conserve son authentification par token et n'expose pas les identifiants MinIO.

Le serveur construit son URI PostgreSQL à partir des variables existantes, sans
la placer dans ses arguments de lancement. Il crée sa base si nécessaire, puis
MLflow initialise ses tables. Le compte PostgreSQL local fourni par l'image
officielle a les droits nécessaires ; un compte limité sur une autre VM exige
une création préalable de la base. Aucune variable secrète n'est ajoutée au Git.

## Ce que l'on trouve dans MLflow

Deux expériences et deux noms de modèles enregistrés : `p4-cons`, `p4-pros`.
Les versions initiales du kit sont aussi enregistrées, avec l'origine
`provided_kit` et sans inventer de métriques d'entraînement.

Pour chaque run terminé :

- catégorie, version de projet, modèle de base, seed et nombre d'arbres ;
- métriques A2 et held-out du candidat et de la référence, durée ;
- fichiers `model/model.txt`, `metrics.json` et `manifest.json` avec la lignée ;
- décision `accepted` ou `rejected`, raison du kit et état de publication.

Les échecs techniques/interrompus sont des runs MLflow `FAILED`, avec un
`failure.json`, sans publication d'un modèle. Un candidat rejeté est un run
`FINISHED` et une version enregistrée avec `decision=rejected` : son entraînement
a réussi techniquement. Il reste consultable dans MLflow mais l'API de modèles
ne permet pas son téléchargement.

Le registre contient les fichiers LightGBM bruts attendus par le kit ; il ne
crée pas une nouvelle interface d'inférence MLflow PyFunc. L'edge continue de
télécharger `model.txt` par votre API et d'appeler le kit fourni.

## Numérotation

Le numéro du projet (`candidate_version`, puis `model_versions.version`) reste
la référence des paramètres Nuvla et des URLs API. MLflow numérote ses versions
indépendamment. Les échecs d'entraînement peuvent créer des trous dans la
numérotation du projet. Les colonnes `mlflow_run_id`, `mlflow_model_version`
et le tag `project_version` fournissent la correspondance : ne jamais supposer
que les deux numéros sont identiques.

## Publication et reprise

La table `training_runs` reçoit des colonnes supplémentaires, sans effacement :
`mlflow_run_id`, `mlflow_model_version`, `publication_status`,
`publication_error_type` et `publication_finished_at`. `model_versions` reçoit
les références MLflow et la lignée JSON.

Sous le même verrou par catégorie que l'entraînement :

1. Le worker reprend les publications en attente avant de sélectionner le
   modèle de référence de l'entraînement suivant.
2. Il télécharge les artefacts et vérifie hash, catégorie, manifeste, lignée et
   décision. Il vérifie aussi que le booléen de release correspond à la règle
   du kit appliquée aux métriques archivées, sans modifier cette règle.
3. Il retrouve ou crée le run MLflow grâce au tag `training_run_id`, enregistre
   les métriques/artefacts et retrouve ou crée sa version de registre.
4. Pour un rejet, il conserve `model_versions.status=rejected` et ne change
   pas `model_releases`.
5. Pour une acceptation, une transaction insère la version `released` et
   avance le pointeur `model_releases`. Elle refuse un candidat dont le modèle
   de base a été remplacé entre-temps ; elle ne rétrograde pas le pointeur.
6. Il met à jour l'alias MLflow `released`, puis marque la publication terminée.

L'API `/latest` utilise PostgreSQL, pas l'alias MLflow. Une panne entre les
étapes 5 et 6 peut donc rendre le modèle disponible dans l'API avant que l'alias
soit mis à jour. Le réessai retrouve le même run et la même version et termine
la synchronisation. Il ne relance pas l'entraînement et n'écrase pas le modèle.
Les chemins d'artefacts sont réutilisés sur réessai. Une interruption au milieu
des écritures de métriques peut répéter certains points de leur historique,
mais pas créer volontairement un second run/version pour le même entraînement.
Il n'y a pas de transaction distribuée PostgreSQL/MLflow/S3.

Si MLflow est indisponible, les résultats terminés restent `completed` avec
publication `pending`. Le worker réessaie au prochain passage et attend leur
synchronisation avant un nouvel entraînement de la catégorie pour éviter
d'utiliser une référence obsolète. Un conflit d'intégrité ou de référence
nécessite une investigation ; il n'est pas contourné automatiquement.

Un seul worker à la fois doit posséder le verrou PostgreSQL d'une catégorie.
La récupération par recherche MLflow limite les doublons en cas de réponse
perdue ; elle n'est pas une primitive d'idempotence distribuée garantie par
MLflow face à des partitions arbitraires ou à des écritures manuelles concurrentes.

```sql
SELECT category, candidate_version, status, release_decision,
       publication_status, mlflow_run_id, mlflow_model_version
FROM training_runs ORDER BY started_at;
```

## Tests

```powershell
docker compose -p group09-publication-tests -f cloud/compose.publication-tests.yaml up --build -d
docker compose -p group09-publication-tests -f cloud/compose.publication-tests.yaml logs publication-tests
```

Base applicative `p4_publication_tests`, base MLflow `p4_mlflow_publication_tests`
et bucket `p4-publication-tests` sont isolés. Les fixtures nettoient seulement
les tables applicatives de cette base de test. Les expériences MLflow restent
présentes et les réessais sont testés sans effacer les versions du registre.

Les cas déterministes accepté/rejeté utilisent des artefacts de test aux
métriques contrôlées pour exercer chaque branche. Deux autres tests exécutent
réellement le kit, un pour cons et un pour pros, puis vérifient la traçabilité
et l'application exacte de sa décision. Les prévisions des batches de test sont
volontairement dégradées : ce n'est pas l'évaluation finale du replay.

Le roll-out Nuvla, l'acceptation locale par les edges et le replay F2 complet
restent hors de ce bloc. Une publication est disponible dans l'API ; elle ne
constitue pas encore une proposition de version transmise par Nuvla.

## Sources techniques

- https://mlflow.org/docs/latest/tracking/server/
- https://mlflow.org/docs/latest/api_reference/python_api/mlflow.client.html

## Résultats du 1 novembre 2026

- F1 après ajout du client MLflow : 30/30 vecteurs réussis, écart nul ;
  `pip check` du trainer ne détecte aucun conflit.
- Suite publication : 11 tests réussis en 50,45 secondes, dont deux vrais
  entraînements du kit, cons et pros, suivis automatiquement dans MLflow.
- Régression du bloc trainer : 19 tests réussis en 39,09 secondes.
- Le serveur de développement est healthy et `/health` répond 200.
- Les modèles initiaux cons/pros sont enregistrés dans MLflow avec l'alias
  `released`. Les artefacts ont été retéléchargés via MLflow et leurs SHA-256
  sont identiques à ceux du kit et du registre API.
- Les versions publiées restent à 1 et aucun batch réel n'a été injecté :
  `training_runs` de développement reste vide.
- Le conteneur trainer a été recréé pour activer la synchronisation. Les piles
  publication-tests et trainer-tests ont été arrêtées après validation, sans
  suppression de leurs volumes. Aucun commit ni push n'a été effectué.
