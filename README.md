# Atelier routines

Serveur MCP local en Python pour les étapes mécaniques : inspections, recettes déclaratives, suivi durable et collecte vérifiée. Deux outils seulement, `routines_find` et `routines_run` ; les descriptions du catalogue ne sont pas chargées dans leurs définitions.

Le catalogue contient 109 procédures et trois propositions exclues. 38 opérations sont implémentées ; les autres restent `guided`, avec leurs prérequis. Une opération peut être `unavailable` si son client, son format ou son environnement manque.

## Utilisation

Utiliser directement un ID connu. Sinon appeler `routines_find(query, limit=3)`, puis `routines_run`. Garder la CLI en repli lorsque le MCP est indisponible ; ne pas faire les deux recherches. Une question simple ne lance aucune opération.

```json
{"id":"git.status","params":{"project":"/absolute/project"}}
```

Une clé facultative stabilise une demande à travers une interruption :

```json
{"id":"git.status","params":{"project":"/absolute/project"},"request_id":"review-branch-1"}
```

Le même identifiant retrouve le suivi. Un retour `historical=true, facts_current=false` atteste une exécution passée ; il ne réévalue ni service, ni ressource, ni autorisation. Un changement d’entrées ou de version avec la même clé exige une nouvelle décision.

| Groupe | IDs implémentés |
| --- | --- |
| Projet | `system.context`, `project.manifest`, `project.tree`, `project.outline`, `project.search`, `project.symbol`, `project.instructions`, `project.errors` |
| Git | `git.status`, `git.diff`, `git.staged`, `git.log`, `git.blame`, `git.branches`, `git.remote` |
| Fichiers et rapports | `files.json`, `files.csv`, `files.logs`, `files.archive`, `checks.coverage`, `checks.failure`, `audio.inspect`, `reports.validate`, `reports.compare` |
| État local | `system.preflight`, `system.memory`, `system.storage`, `system.processes`, `web.ports`, `web.api-health` |
| Recettes | `routines.register`, `routines.list`, `routines.manage` |
| Suivi | `executions.inspect`, `executions.wait`, `executions.resume`, `executions.resolve` |
| Calcul autorisé | `checks.regression` : suite complète de ce projet via le client Atelier VPS installé |

Les schémas exacts sont renvoyés par `routines_find`. Les recherches sont littérales. Les diffs Git sont statistiques. ZIP et PCM/WAV sont les formats autonomes d’archives et d’audio. La couverture est celle d’un rapport existant ; elle ne lance pas de tests. Un endpoint de santé accessible ne valide pas un scénario E2E.

## Routines confiées par les agents

[examples/project-review.json](examples/project-review.json) regroupe statut/diffs Git et recherche ciblée. L’enregistrer avec `routines_run(id="routines.register", params={"project":"/absolute/project", ...définition})`, puis :

```json
{"id":"custom.atelier-project-review","params":{"project":"/absolute/project","term":"service"}}
```

Une recette déclare au plus huit paramètres simples et huit étapes. Une liaison `{"$input":"term"}` fournit un paramètre ; il n’y a ni interpolation de commande, ni boucle, ni récursion. Le projet est imposé par l’appel. Les étapes utilisent uniquement les inspections autorisées ; les gestionnaires et lanceurs de calcul ne peuvent pas être imbriqués. Le premier résultat non réussi arrête la recette.

`routines.list` donne les recettes et leur dernier état. `routines.manage` accepte `name` et `action=show|enable|disable`. Une définition différente ne remplace jamais silencieusement une recette existante. Désactiver conserve l’historique.

## Exécution, preuves et comparaison

Chaque opération reçoit un `execution_id` enregistré avant son lancement. `executions.inspect` le retrouve sans relancer le travail ; `detail=errors|steps`, `start` et `limit` lisent une page ciblée du reçu. Des demandes identiques actives retrouvent leur suivi. Après une interruption incertaine, aucune nouvelle exécution n’est lancée automatiquement. `executions.resolve` peut seulement clore le suivi d’une inspection locale dont le serveur propriétaire est arrêté, avec un motif explicite ; elle n’arrête aucun service et ne valide rien.

Les résultats de certaines inspections de fichiers peuvent être réutilisés après vérification SHA256 des entrées, du code, de Python, du verrou de dépendances et des preuves. Un reçu absent ou altéré exige une décision. Git, services, ressources, autorisations et tests ne sont pas mis en cache comme vérités actuelles. Une nouvelle révision ne récupère pas un ancien test vert.

`reports.validate` vérifie des assertions JSON explicites, par exemple `assertions=[{"pointer":"/passed","equals":true}]`. Son résultat décrit ces assertions, pas une nouvelle exécution de tests. `reports.compare` prend `reference` et `current`, deux IDs d’exécution compatibles. Les journaux utilisent le SHA256 de la ligne exacte avant masquage : erreurs nouvelles, persistantes et résolues restent distinctes. Une erreur persistante n’est pas considérée comme expliquée. Une référence incompatible ou un index incomplet bloque la comparaison.

Pour le cycle complet autorisé de ce projet :

```json
{"id":"checks.regression","params":{"project":"/absolute/atelier-routines","wait":true},"request_id":"validation-revision-1"}
```

L’adapter contrôle les prérequis et la capacité globale, envoie les sources filtrées, attend le reçu, collecte les rapports, vérifie SHA256 et assertions, vérifie la révision puis libère uniquement le calcul terminé dont les preuves sont conservées. Il appelle le client Atelier VPS existant ; aucun accès arbitraire à d’autres connecteurs n’est exposé. Les configurations et clés personnelles restent locales.

Avec `wait=false`, le MCP persistant renvoie le suivi immédiatement. `executions.wait` attend le cycle sans nouveau lancement. `executions.resume` reprend uniquement l’attente ou la collecte d’un calcul distant identifié et non libéré, sans soumission supplémentaire. Un état inconnu ou des preuves manquantes préservent le calcul. La CLI ne détache pas de processus et exige `wait=true`. Les autres projets restent guidés tant qu’aucun adapter autorisé ne couvre leurs vérifications.

## Installation

Python 3.12, `rtk`, `rg` et Git, sur macOS ou Linux. Réutiliser un Python possédant déjà MCP 1.30.0. Sinon, installer seulement le SDK et ses dépendances verrouillées :

```sh
rtk proxy uv venv --python 3.12 .venv
rtk proxy uv pip install --python .venv/bin/python --require-hashes -r requirements.lock
rtk proxy .venv/bin/python scripts/install.py
```

`--replace` met à jour cette installation reconnue uniquement. L’installateur conserve les autres réglages et secrets de la configuration personnelle, vérifie le TOML et installe un point d’entrée de skill détectable. Les modifications locales du skill sont préservées. Le transport est stdio ; le délai d’outil couvre l’attente VPS. Voir la [configuration MCP officielle](https://learn.chatgpt.com/docs/extend/mcp?surface=cli). Recharger les connexions ou ouvrir un nouveau chat après installation.

Deux hooks utilisateur ajoutent des rappels courts : `SessionStart` et `PostToolUse` limité à `routines_run`. Ils ne lancent aucun outil, ne lisent aucun projet et restent silencieux après un succès courant. Leur script installé porte l’empreinte de son contenu ; les autres hooks restent présents. Examiner puis approuver leurs définitions avec `/hooks` dans Codex : une nouvelle définition est ignorée tant qu’elle n’est pas approuvée, conformément au [contrat des hooks](https://learn.chatgpt.com/docs/hooks). L’installateur ne modifie jamais le registre de confiance.

Les racines autorisées sont configurées dans `ATELIER_ROUTINES_ROOTS`, une liste JSON ; le défaut est le dossier parent de l’installation. Le registre privé est sous `~/.local/share/atelier-routines`, le suivi dans le dossier voisin `atelier-routines-executions`. La CLI et le MCP partagent le catalogue et le moteur :

```sh
rtk proxy python scripts/routines.py run git.status --params '{"project":"/absolute/project"}'
rtk proxy python scripts/routines.py run git.status --params '{"project":"/absolute/project"}' --request-id review-branch-1
```

Les anciennes commandes `match`, `list`, `show`, `context`, `tree`, `outline` et les gardes restent disponibles. `run` sort avec 0 uniquement pour `ok` ; les résultats partiels, guidés, incertains ou échoués ne passent pas pour une validation.

## Vérification

La suite teste de vrais transports MCP, les recettes, l’interruption, les doublons, les assertions, les références périmées, l’invalidation et les preuves manquantes. Les services HTTP nécessaires sont démarrés dans les fixtures et leurs assertions s’exécutent réellement. `scripts/verify.py` mesure trois scénarios identiques avec les anciennes commandes, puis le MCP, en séparant réussite, échec et réutilisation vérifiée. Les appels outil rejoués, octets et durées sont distincts des commandes internes ; aucune inférence, aucun token réel ou pourcentage d’économie n’est inventé. Le regroupement peut réduire des appels et le volume ; les petits retours peuvent grossir.

Les contrôles portables se lancent sur Atelier VPS avec `arch=any` et un pic complet de 0,8 Gio. La CI macOS vérifie les branches natives et réutilise les gardes canoniques ; les actions, outils et dépendances sont épinglés. La CI conserve les preuves et bloque l’intégration sur échec. `scripts/native_cycle_check.py` vérifie sur le Mac l’entrée personnelle exacte et un vrai cycle VPS, avec une collecte interrompue puis reprise sans nouveau calcul ; il nécessite les clients locaux et son propre espace de recette gardé.

Python est conservé : les traces disponibles ne démontrent pas un problème qui exige une réécriture en Rust. Les limites et le modèle de confiance sont détaillés dans [SECURITY.md](SECURITY.md).
