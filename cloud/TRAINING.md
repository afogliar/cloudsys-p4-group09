# Réentraînement automatique par catégorie

Ce worker orchestre le kit fourni. Il ne modifie ni le kit, ni les données
pédagogiques, ni la décision de release du kit. Si `MLFLOW_TRACKING_URI` est
configuré, il synchronise aussi les résultats et publications dans MLflow
(voir MLFLOW.md). Le roll-out Nuvla reste un bloc séparé.

## Démarrage et commandes

L'image utilise Python 3.11, installe exactement `kit/requirements.txt`, applique
ces mêmes contraintes aux dépendances additionnelles et exécute les 30 vecteurs
F1 au build. `libgomp1` est nécessaire à LightGBM. Kit et données sont copiés
dans l'image depuis le dossier frère `cloudsys-p4-students-main`, sans les
modifier. Le contexte additionnel `materials` du Compose indique ce dossier.

Les modèles initiaux doivent déjà être importés (voir MODEL_ACCESS.md).

```powershell
docker compose --env-file cloud/.env -f cloud/compose.yaml up --build -d trainer
docker compose --env-file cloud/.env -f cloud/compose.yaml logs --tail 30 trainer
```

Le service appartient au profil `training` : un démarrage générique du Compose
ne l'active pas par inadvertance. Le démarrage explicite de `trainer` suffit.
Le worker vérifie chaque catégorie toutes les 5 secondes réelles par défaut.
Il traite les catégories séquentiellement dans un conteneur, mais possède un
verrou indépendant par catégorie pour permettre plusieurs workers sans lancer
deux entraînements d'une même catégorie en parallèle.

Le volume `trainer_state` conserve manifestes, entrées, modèle de référence,
sorties et logs sous `/state/runs/<run_id>/`. Rien n'est nettoyé automatiquement.

Pour vérifier une catégorie une seule fois (le même verrou protège le daemon) :

```powershell
docker compose --env-file cloud/.env -f cloud/compose.yaml run --no-deps trainer python -m trainer.worker --once --category cons
```

Options : `--poll-seconds` (défaut 5), `--timeout-seconds` (défaut 600).
Le délai maximal est un délai réel d'exécution technique, pas une règle métier.

## Temps simulé et déclenchement

La progression observée est le lendemain de la dernière journée reçue dans
`daily_metrics` ou dans un batch `stored`, limitée à la période B. Par exemple,
un bilan du 14 avril implique un temps simulé observé au 15 avril à minuit.
Avant la première donnée de B, l'horloge vaut le 1 avril 2026. Les dates de
réception réelles et `sent_at` ne pilotent jamais le repli de 14 jours.

**Contrat à partager avec les étudiants 1 et 3 :** les bilans doivent être
envoyés après la clôture d'une journée, et les edges doivent partager le même
replay. Le cloud observe le plus avancé des bilans reçus, sans barrière attendant
les dix compteurs. Il ne connaît pas l'avancement non reçu pendant une coupure
totale ; le repli reprend quand les données quotidiennes reviennent. Les dates
hors B et les jours de warm-up sont ignorés pour cette horloge.

Depuis le dernier entraînement terminé de la catégorie, le worker déclenche si :

- 8 nouvelles journées-compteurs signalées proviennent d'au moins 2 compteurs ;
- ou 14 jours simulés ont passé et au moins une nouvelle journée est disponible.

Une catégorie sans historique de formation utilise le début de B comme origine.
Les seuils sont lus dans `meters.json`. Les doublons `(device_id, day)` ne gonflent
pas le compteur : le premier batch finalisé est la référence. Les états `pending`
ne sont jamais comptés ni téléchargés pour l'entraînement.

## Snapshot et entrées

À chaque lancement, la liste des batches est figée. Le déclencheur compte les
nouveaux jours, mais le kit reçoit **tous les jours signalés disponibles** pour
la catégorie à cette date, plus A1. Les batches arrivant pendant la formation
restent nouveaux pour le prochain déclenchement. Les réceptions tardives de
journées anciennes ne sont pas perdues.

Chaque JSON est téléchargé depuis la clé indexée en PostgreSQL et son SHA-256
est vérifié. Le modèle de référence est celui actuellement publié par l'API,
avec contrôle de son intégrité. Les commandes du kit utilisent le seed 42 et
le nombre d'arbres imposé (879 cons, 388 pros), sans réduction pour les tests.
Le kit réserve les derniers 20 % des batches et produit la décision de release.

## Verrou et récupération

Un verrou consultatif PostgreSQL de session `(94009, 1)` pour cons ou
`(94009, 2)` pour pros couvre le snapshot et toute l'exécution. Une requête
concurrente renvoie `locked`. La session est vérifiée pendant le sous-processus
et avant sa finalisation ; en cas de perte détectée, le sous-processus est arrêté.
La fermeture de la connexion libère le verrou. Il ne s'agit pas d'une garantie
de disponibilité en cas de partition réseau arbitraire : une perte de session
est détectée au prochain contrôle, et les erreurs restent réessayables.

`training_runs` conserve le run, ses versions candidate et de référence,
la date simulée, le motif, les batches du snapshot et son état :

- `running` : en cours ;
- `completed` : kit et upload des artefacts terminés, décision acceptée ou rejetée ;
- `failed` : erreur technique ; aucun batch n'est consommé ;
- `interrupted` : ancien run en cours retrouvé sans verrou survivant.

Un essai suivant utilise une nouvelle version et un nouveau dossier, même après
échec, pour respecter l'interdiction d'écraser les modèles. Un run terminé mais
dont le candidat est rejeté consomme son snapshot : on attend de nouvelles données.
En cas d'indisponibilité PostgreSQL lors du marquage d'échec, le run restant
`running` sera récupéré après reconnexion. Les erreurs exposées par le worker
contiennent seulement leur type, sans secrets ni données brutes.

## Artefacts et frontière avec le bloc suivant

Les sorties sont conservées localement et dans le bucket existant :

```text
models/<category>/v<NNNN>/model.txt
models/<category>/v<NNNN>/metrics.json
models/<category>/v<NNNN>/manifest.json
```

Le manifeste référence tous les batches et le modèle de base ; `metrics.json`
contient la lignée réellement utilisée/réservée et les métriques du kit.
Le worker vérifie le hash de sortie et la correspondance de la lignée.

Le numéro de version est réservé dans `training_runs`. Sans configuration
MLflow, les candidats restent en attente de publication. Avec MLflow configuré,
le module `trainer.publication` traite les résultats avant tout entraînement
suivant, met à jour le registre de lecture uniquement pour les candidats
acceptés et conserve les rejets. Une panne de suivi ne transforme jamais un
entraînement terminé en échec technique. Voir MLFLOW.md pour les garanties et
la reprise de cette seconde phase.

```sql
SELECT category, candidate_version, base_version, simulated_at,
       trigger_reason, status, release_decision, artifact_prefix
FROM training_runs ORDER BY started_at;
```

## Tests

```powershell
docker compose -p group09-trainer-tests -f cloud/compose.trainer-tests.yaml up --build -d
docker compose -p group09-trainer-tests -f cloud/compose.trainer-tests.yaml logs trainer-tests
```

La pile a une base `p4_trainer_tests` et un bucket `p4-trainer-tests` séparés de
l'environnement courant. Les fixtures nettoient uniquement les tables de cette
base après vérification des noms. Les mesures et historiques de test viennent
des Parquet fournis, mais les prévisions sont volontairement dégradées pour
forcer les déclenchements : ces essais **ne constituent pas une évaluation de
performance ni le test F2 complet du replay**.

Deux tests exécutent réellement `p4kit.train`, un pour chaque catégorie, sans
changer les paramètres. Les autres couvrent les seuils, le repli, le verrou,
les réceptions pendant la formation, la reprise après échec/interruption, les
doublons, les batches incomplets, l'intégrité et le timeout du sous-processus.

## Résultats du 1 novembre 2026

- F1 dans l'image trainer : 30/30 vecteurs réussis, écart maximal nul.
- Suite finale : 19 tests réussis en 42,86 secondes, code de sortie 0.
- Deux entraînements réels, cons et pros, ont produit modèle, métriques et
  manifeste, avec les nombres d'arbres imposés, puis leurs artefacts ont été
  retrouvés dans MinIO. Chaque snapshot de 8 batches est réparti en 6 utilisés
  et 2 réservés par le kit.
- Le service de développement `cloud-trainer-1` est lancé et signale `idle`
  pour les deux catégories, à la date simulée observée du 2 avril 2026.
  Aucun batch réel n'étant disponible, `training_runs` y contient zéro run.
- Les versions publiées restent cons=1 et pros=1. Les données de test sont
  exclusivement dans la pile `group09-trainer-tests`.
- Aucun commit ni push n'avait été effectué à la fin du bloc d'entraînement.
