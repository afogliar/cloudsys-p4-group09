# Accès aux modèles publiés

Ce bloc ajoute la lecture des modèles et l'import initial. Il ne réalise ni
réentraînement, ni publication d'un challenger, ni MLflow, ni roll-out Nuvla.

## Contrat avec l'edge

Les deux routes exigent un token de compteur valide. Un compteur authentifié
peut consulter les deux catégories, comme les autres routes de lecture du
projet ; aucune clé S3 ne lui est transmise.

- `GET /v1/models/cons/latest` renvoie un JSON avec `category`, `version`
  (entier), `sha256` et `size_bytes`. Remplacer `cons` par `pros` pour l'autre
  catégorie. Il s'agit de la version publiée, pas de la version active localement.
- `GET /v1/models/cons/1` renvoie les octets du modèle LightGBM en texte,
  avec un nom de téléchargement `model_cons_v0001.txt`, `ETag`,
  `X-Model-SHA256` et `X-Model-Version`.
- Catégorie inconnue ou version non entière/inférieure à 1 : 422.
- Version inconnue, `pending` ou `rejected`, ou aucune version publiée : 404.
- PostgreSQL/S3 indisponible, fichier publié absent ou intégrité incorrecte :
  503, message générique et `Retry-After: 5`.
- Token absent/invalide : 401.

La route statique `/latest` est déclarée avant la route de version. La lecture
vérifie la taille et le hash avant d'envoyer le moindre octet du modèle. Les
fichiers actuels font environ 2,6 Mo et 1,1 Mo : une lecture complète en mémoire
est suffisante pour cette échelle. L'edge devra lui aussi vérifier le hash.

## Registre PostgreSQL minimal

`model_versions` contient catégorie, version, clé objet, hash, taille, état et
date de création. `model_releases` indique la version actuellement publiée de
chaque catégorie. La version publiée est donc une décision explicite et non
simplement le plus grand numéro existant. Les anciennes versions publiées
restent téléchargeables par leur numéro. Les futures intégrations MLflow et
trainer devront respecter cette convention ; elles sont hors de ce bloc.

## Import initial reproductible

Le kit doit être dans le dossier frère `cloudsys-p4-students-main/kit`, comme
sur le PC actuel. Le montage est en lecture seule et refuse de créer un dossier
source absent. Adapter ce chemin avec l'étudiant 3 sur la VM.

Depuis la racine du dépôt, l'import utilise les services existants sans les
remplacer, et conserve son conteneur à la fin :

```powershell
docker compose --env-file cloud/.env -f cloud/compose.yaml run --no-deps --build models-init
```

Le programme lit `meters.json`, vérifie les hashes des deux modèles avant toute
écriture, puis crée les tables et importe les versions 1 aux emplacements :

```text
models/cons/v0001/model.txt
models/pros/v0001/model.txt
```

L'import réserve chaque version en `pending`, écrit le fichier et passe la
version à `released` dans une transaction verrouillée. Un réessai reprend une
écriture interrompue. Une version 1 déjà publiée est vérifiée sans réécriture.
Un contenu différent est refusé. Une version plus récente déjà publiée n'est
jamais rétrogradée vers la version 1. L'import des deux catégories n'est pas une
transaction distribuée ; un échec intermédiaire se reprend en relançant l'import.

L'API déjà lancée doit ensuite être reconstruite et son conteneur recréé pour
exposer les nouvelles routes. Cette commande remplace uniquement le conteneur
API et conserve les volumes PostgreSQL/MinIO :

```powershell
docker compose --env-file cloud/.env -f cloud/compose.yaml up --build -d --no-deps api
```

## Tests sans suppression

```powershell
docker compose -p group09-model-tests -f cloud/compose.model-tests.yaml build model-tests
docker compose -p group09-model-tests -f cloud/compose.model-tests.yaml up -d postgres-model-test minio-model-test
docker compose -p group09-model-tests -f cloud/compose.model-tests.yaml run --no-deps model-tests
```

La pile de test a ses propres volumes, une base `p4_model_tests`, un bucket
`p4-model-tests` et des identifiants fictifs. Elle ne supprime aucun objet ni
aucune ligne : ses ressources restent présentes après exécution. Aucun ancien
test d'intégration de batches (avec nettoyage de fixtures) n'est exécuté ici.

## Vérification du 1 novembre 2026

23 tests réussis dans Docker sous Python 3.11 : 13 cas des nouvelles routes et
de l'import, plus les 10 tests HTTP du stockage des batches. Les vérifications
réelles utilisent PostgreSQL, MinIO et les deux modèles officiels montés en
lecture seule : octets téléchargés identiques aux sources, hashes, import
répétable, refus d'une version initiale altérée, versions non publiées masquées.
Un premier essai avait un chemin de montage incorrect ; il a été corrigé et la
suite a ensuite réussi intégralement. Le dossier vide créé par Docker à
`cloudsys-p4-students-main/kit` dans le dépôt reste présent, sans suppression.

Le warning du client de test est une dépréciation, pas un échec. Les tests ne
valident pas encore l'adoption locale par les edges ni le roll-out.

L'import initial et l'activation ont également été effectués dans l'environnement
de développement. Sur l'API active : latest=1 pour les deux catégories,
téléchargements et hashes conformes au kit, accès sans token rejeté avec 401,
version inexistante rejetée avec 404. Les volumes existants ont été conservés.
