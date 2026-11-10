# Atelier routines

## Décisions locales d1,16 bits

Le modèle sélectionne des réponses bornées, sans autorité d'exécution. Employer les règles déterministes et les attentes backend quand elles suffisent. Pour une décision ambiguë nécessaire, `decision.evaluate` accepte directement le contexte et les candidats en un appel MCP, sans fichier intermédiaire :

**d1 est prioritaire avant Sol pour les décisions fermées éligibles.** `routines_find` consulte automatiquement le service local quand plusieurs routines exécutables restent ambiguës ; le champ `local_decision` indique sa confiance, la recommandation acceptée ou le repli. Un ID connu, une correspondance déterministe ou un seul candidat évitent l'inférence. Aucun appel cloud ou VPS automatique. Le routage local est borné à2 s, le seuil reste0,99, et chaque tentative est comptée sous `source=local-router`, `operation=decision.evaluate`. Ce choix ne lance aucune routine.

Les hooks et le skill installés imposent aussi d1 avant Sol pour les autres choix fermés. Une ancienne connexion MCP ne recharge pas son code en mémoire : jusqu'à sa reconnexion, si `local_decision` manque lors d'un choix ambigu, appeler explicitement `decision.evaluate`. Les tests de régression isolés désactivent le vrai modèle avec `ATELIER_ROUTINES_DECISION=off` ; les tests d'intégration du routage utilisent le vrai service et vérifient le compteur d'usage.

```json
{"id":"decision.evaluate","params":{"project":"/chemin/absolu/projet","input":{"state":"Tests verts en local, échecs en CI après mise à jour des dépendances.","questions":{"triage":{"type":"choice","instructions":"Quelle piste vérifier en premier ?","criteria":{"environment":"Comparer versions et dépendances local/CI.","logic":"Examiner la logique métier."}}}}}}
```

Le mode existant `{"project":"/chemin/absolu/projet","file":"decision.json"}` reste disponible : choisir exactement `input` ou `file`. Voir [l'exemple](examples/decision.json). Les deux modes partagent la même validation ; l'objet inline est limité à64 Kio. `questions` est un objet indexé par identifiant, et `criteria` un objet de candidats décrits. `state_file` remplace `state` pour éviter de recopier un contexte déjà sur disque.1–4 questions `choice` (2–8 critères) ou `noul` ; `images` contient au plus un chemin PNG relatif (2 Mio,2048×2048). Les gardes de chemins et racines restent applicables. La réponse compacte contient valeurs, confiance, acceptation, latence et empreintes, sans entrée/image brute ni clé d'API. `cloud_tokens_used=0` n'est pas une mesure de tokens économisés.

Seuil0,99 par défaut : `usable=false` et `fallback_required=true` demandent un repli LLM, sans appel cloud automatique. `status=ok` confirme seulement une inférence complète ; il ne valide pas le choix. Ni la confiance ni le modèle ne valident des tests, une permission, une fusion ou un déploiement.

`decision.status` inspecte le service déjà chargé. Installation explicite sur Apple Silicon, sans copie ni téléchargement des poids :

```sh
rtk proxy python3 scripts/install_decision.py --assets /chemin/assets-verifies
```

Les assets fournissent le manifeste vérifié du benchmark et les fichiers BF16 officiels. L'installateur vérifie leurs empreintes, conserve leurs chemins et crée le LaunchAgent utilisateur `fr.atelier.routines.d1` : localhost authentifié, quatre threads et Metal, une inférence à la fois. Démarrage à la connexion, sans boucle de redémarrage. Arrêt : `rtk proxy launchctl bootout gui/UID/fr.atelier.routines.d1` ; redémarrage : `rtk proxy launchctl bootstrap gui/UID ~/Library/LaunchAgents/fr.atelier.routines.d1.plist`. Le service reste chargé pour éviter le coût d'initialisation à chaque décision. Journaux runtime tournants3×512 Kio ; aucune requête brute journalisée par l'intégration.

### Déporter via le MCP Atelier VPS

À l'installation, fournir `--vps-weights-job ID --vps-runtime-job ID`, les jobs conservés sur Préprod. Pas de téléchargement automatique ni de routage vers le petit VPS.

1. `decision.prepare-vps` avec le même fichier prépare un paquet déterministe et renvoie les arguments exacts de `mcp__atelier_vps__vps_task`. Aucun calcul soumis ; entrée non sensible uniquement. Les motifs de secrets reconnus sont refusés, sans garantie de détection exhaustive. Le paquet contient le code fixé, l'entrée et une recette, jamais les poids ou les sources du projet.
2. Appeler le MCP Atelier VPS avec ces arguments : Préprod x86_64,CPU2,RAM10 Gio,disque0,2 Gio,délai300 s. La tâche charge les poids conservés, décide et ferme son serveur. Attendre sa référence sans resoumettre un calcul incertain.
3. Récupérer `receipt.json` et `work/decision-result.json` via `vps_fetch`. Après validation du reçu, utiliser `decision.result` : `file` vers le résultat récupéré, `request` vers l'`input.json` du paquet d'origine. Le seuil local reste0,99 par défaut même si l'artefact propose un seuil inférieur.
4. Préserver les preuves puis libérer cette seule tâche avec `vps_release`. Les jobs de poids/runtime restent nécessaires.

Paquets limités à64 et64 Mio par projet ; nettoyer seulement ses paquets devenus inutiles, après préservation. Capacité refusée, service ou poids absents : blocage explicite, aucune bascule ni relance implicite. Le CPU VPS peut demander plusieurs secondes : déport de capacité, sans promesse d'accélération. Les deux outils MCP restent `routines_find` et `routines_run`.

Version **1.2.3** : `rtk proxy atelier-routines --version`. La CLI, l’initialisation MCP et les métadonnées installées partagent la même version.

Serveur MCP local en Python pour les étapes mécaniques : inspections, recettes déclaratives, suivi durable et collecte vérifiée. Deux outils seulement, `routines_find` et `routines_run` ; les descriptions du catalogue ne sont pas chargées dans leurs définitions.

Le catalogue contient120 procédures et trois propositions exclues.54 opérations sont implémentées ; les autres restent `guided`, avec leurs prérequis. Une opération peut être `unavailable` si son client, son format ou son environnement manque.

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
| GitHub | `github.context`, `github.prs`, `git.pr-create`, `git.pr-checks`, `git.protection`, `git.integration`, `git.merge` |
| Fichiers et rapports | `files.json`, `files.csv`, `files.logs`, `files.archive`, `checks.coverage`, `checks.failure`, `audio.inspect`, `reports.validate`, `reports.compare` |
| État local | `system.preflight`, `system.memory`, `system.storage`, `system.processes`, `web.ports`, `web.api-health` |
| Recettes | `routines.register`, `routines.list`, `routines.manage` |
| Suivi | `jobs.wait`, `executions.inspect`, `executions.wait`, `executions.resume`, `executions.resolve` |
| Calcul autorisé | `checks.regression` : suite complète de ce projet via le client Atelier VPS installé |

Les schémas exacts sont renvoyés par `routines_find`. Les recherches sont littérales. Les diffs Git sont statistiques. ZIP et PCM/WAV sont les formats autonomes d’archives et d’audio. La couverture est celle d’un rapport existant ; elle ne lance pas de tests. Un endpoint de santé accessible ne valide pas un scénario E2E.

## GitHub et les deux comptes

`github.context` résout le remote `origin` (ou `remote` explicite), le compte attendu, le login réellement authentifié et les permissions. Tout dépôt sous `guillaume34110/` utilise `guillaume34110` ; tout dépôt sous `guillaume-rebaze/` utilise `guillaume-rebaze`. Une configuration contradictoire bloque l'opération. Pour une organisation ou un autre propriétaire, associer une fois le dépôt avec `rtk proxy git config --local atelier.githubAccount guillaume-rebaze` ou `guillaume34110`.

Les deux connexions doivent exister dans `gh` sur le Mac. Un compte absent ou expiré retourne `unavailable` avec le compte à connecter. Chaque appel applique sa credential au seul sous-processus concerné, après vérification de `/user` : aucune bascule globale, aucun recours à l'autre compte. L'identité de commit reste celle configurée dans Git. Les secrets et comptes personnels restent locaux.

```json
{"id":"github.context","params":{"project":"/absolute/project"}}
{"id":"github.prs","params":{"project":"/absolute/project","scope":"accounts","page":1,"limit":20}}
{"id":"git.pr-create","params":{"project":"/absolute/project","branch":"guillaume/sc-123/fix","head_sha":"0123456789012345678901234567890123456789","title":"[sc-123] Correction","body_file":"pr-body.md"},"request_id":"pr-sc-123"}
```

`github.prs` liste par défaut les PR ouvertes du dépôt ; `scope=accounts` liste celles des dépôts possédés par chacun des deux comptes. Un compte inaccessible rend ce résultat `partial`. Les pages restent explicites. Les dépôts d'organisation sont consultables dans leur contexte de projet.

`git.pr-create` travaille sur une branche **déjà publiée**, dont le SHA distant doit correspondre exactement à `head_sha`. Lire les workflows, exécuter leur validation complète puis publier la branche avec son identité Git vérifiée reste préalable à la création. La routine ne pousse aucun commit. Elle retrouve une PR existante et refuse une destination de poussée différente ou ambiguë. `body_file` est relatif au projet et soumis aux protections de lecture habituelles.

`git.pr-checks`, `git.integration` et `git.merge` exigent `number` et `head_sha`. Les contrôles sont relus sur cette révision, avec leur application GitHub attendue. `git.integration` contrôle aussi les protections et le retard sur la base ; `git.merge` reprend ces contrôles et transmet le SHA attendu à GitHub. Les contrôles requis viennent des protections, complétés par les éventuels `atelier.requiredCheck` locaux ; ce dépôt exige toujours `verify` et `security`. Des protections illisibles, une CI manquante, annulée ou rouge et une branche en retard bloquent la fusion. Aucun mode administrateur n'est exposé.

`wait` attend les contrôles pendant au plus 3 600 secondes côté routine. Après un résultat `partial` en attente, `executions.wait` poursuit la même référence. Après une écriture incertaine, `executions.resume` vérifie uniquement son effet distant et ne soumet aucune nouvelle écriture. Une absence d'effet établi reste `decision_required`. Les écritures GitHub sont exclues des recettes génériques d'inspection.

## Routines confiées par les agents

[examples/project-review.json](examples/project-review.json) regroupe statut/diffs Git et recherche ciblée. L’enregistrer avec `routines_run(id="routines.register", params={"project":"/absolute/project", ...définition})`, puis :

```json
{"id":"custom.atelier-project-review","params":{"project":"/absolute/project","term":"service"}}
```

Une recette déclare au plus huit paramètres simples et huit étapes. Une liaison `{"$input":"term"}` fournit un paramètre ; il n’y a ni interpolation de commande, ni boucle, ni récursion. Le projet est imposé par l’appel. Les étapes utilisent uniquement les inspections autorisées ; les gestionnaires et lanceurs de calcul ne peuvent pas être imbriqués. Le premier résultat non réussi arrête la recette.

`routines.list` donne les recettes et leur dernier état. `routines.manage` accepte `name` et `action=show|enable|disable`. Une définition différente ne remplace jamais silencieusement une recette existante. Désactiver conserve l’historique.

## Exécution, preuves et comparaison

Chaque opération reçoit un `execution_id` enregistré avant son lancement. `executions.inspect` le retrouve sans relancer le travail ; `detail=errors|steps`, `start` et `limit` lisent une page ciblée du reçu. Des demandes identiques actives retrouvent leur suivi. Après une interruption incertaine, aucune nouvelle exécution n’est lancée automatiquement. `executions.resolve` peut seulement clore le suivi d’une inspection locale dont le serveur propriétaire est arrêté, avec un motif explicite ; elle n’arrête aucun service et ne valide rien.

Le suivi est global à l’installation. Dès 1 024 entrées courantes, les plus anciens suivis terminés sont déplacés atomiquement dans `atelier-routines-executions/history`, sans modifier leurs octets ni leurs preuves. Les opérations actives, incertaines et aux effets non confirmés restent en place. Les anciens `execution_id` et `request_id` restent consultables et participent à la déduplication ; un reçu historique ne valide pas les faits actuels. Le plafond total reste de **16 Mio**, écritures atomiques comprises, avec au plus **16 384 entrées** courantes et historiques. Atteindre ce plafond refuse un nouveau travail mais laisse la consultation disponible ; aucune purge ni nouvelle soumission automatique. Les reçus par projet gardent leur propre plafond.

Les preuves propres à chaque projet tournent séparément depuis la version 1.2.2 : au seuil de 1 024 reçus courants, les plus anciens reçus terminés vont dans `output/routines-evidence/history`, sous le même verrou. Les octets, empreintes, identifiants et clés de déduplication sont conservés. Les reçus actifs ou incertains restent courants. Courant et historique restent bornés ensemble à **16 Mio / 16 384 reçus**. Les anciens chemins logiques restent lisibles par la CLI et le MCP ; `executions.inspect` renvoie l’emplacement actuel. Un ancien lien direct dans le système de fichiers peut nécessiter cette consultation après déplacement.

Pour reprendre après le blocage des versions précédentes, recharger la connexion MCP sur la version 1.2.2, consulter l’`execution_id` connu avec `executions.inspect`, puis seulement demander l’inspection nécessaire. Une exécution incertaine reste à résoudre explicitement ; ne pas changer sa clé ou relancer un calcul pour éviter son suivi. La migration conserve aussi l’éventuelle 1 025e entrée admise par l’ancien contrôle du suivi. Un plafond cumulé ou des reçus tous actifs reste un refus explicite, jamais une purge automatique.

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

Passer `--root /chemin/du/code` plusieurs fois pour couvrir plusieurs dossiers de projets ; sans cette option, la racine est le dossier parent du dépôt. `--replace` met à jour cette installation reconnue uniquement. L’installateur conserve les autres réglages et secrets de la configuration personnelle, vérifie le TOML et installe un point d’entrée de skill détectable. Les modifications locales du skill sont préservées. Le transport est stdio ; le délai d’outil couvre l’attente VPS. Voir la [configuration MCP officielle](https://learn.chatgpt.com/docs/extend/mcp?surface=cli). Recharger les connexions ou ouvrir un nouveau chat après installation.

Les hooks utilisateur rappellent la priorité MCP au démarrage, à chaque demande et au démarrage d’un sous-agent. `PreToolUse` redirige les inspections shell simples couvertes : statut Git, résumés de diff, branches locales, historique court, inventaire et recherche littérale avec `rg -F`. La commande équivalente est refusée avant exécution avec l’identifiant à appeler ; le hook ne lance pas la routine. `PostToolUse` rappelle les limites d’un retour incomplet ou ancien. Les lectures exactes de source/patch pour éditer, les mutations Git, les recherches regex et les programmes shell complexes restent directs ; ce routeur ciblé ne prétend pas contrôler tout programme arbitraire.

La priorité s’applique à chaque étape couverte, même dans un travail ponctuel. Les rappels ne lancent rien sur une question. Le routage ne lit ni transcript ni contenu de projet et reste limité aux racines autorisées du MCP ; il vérifie seulement les chemins et le marqueur de racine Git. Chaque script installé porte l’empreinte de son contenu ; les autres hooks restent présents. Les deux définitions existantes sont conservées à l’identique lors de l’ajout du routage. Examiner puis approuver les trois nouvelles définitions avec `/hooks` dans Codex : elles sont ignorées tant qu’elles ne sont pas approuvées, conformément au [contrat des hooks](https://learn.chatgpt.com/docs/hooks). L’installateur ne modifie jamais le registre de confiance.

`jobs.wait` surveille un processus local existant pendant 15 minutes par défaut (jusqu’à 1 h), sans retours de progression au modèle. Il exige un PID appartenant à l’utilisateur et travaillant dans le projet, ou détecte un unique garde `workspace-budget.py run` canonique. La référence `execution_id` permet de reprendre avec `executions.wait` ; le PID et son identité de démarrage restent liés. Un délai rend `running`, un PID remplacé `decision_required`, une disparition `partial` : récupérer le code de sortie de la session connue et vérifier les artefacts reste nécessaire. Aucun lancement ou arrêt de calcul, accès aux arguments dans le résultat, ni succès de test déduit du silence.

Deux hooks supplémentaires « attente longue » prolongent les attentes MCP compatibles et surveillent avant les sondages shell de `output/task-tmp/stdout.log` lorsqu’un garde local unique est actif. Cette attente s’exécute dans le hook (900 s maximum, retour anticipé à la fin du processus), sans rendre la main au modèle à chaque progression. `write_stdin` est explicitement exempté de `PreToolUse` par Codex : son délai de 5 minutes reste un repli explicite, pas une interception garantie. Approuver les deux définitions `PreToolUse attente longue` et `PostToolUse attente longue` via `/hooks` ; les cinq précédentes restent inchangées. Le contrôle ne couvre ni le raisonnement interne de Codex ni tous les programmes arbitraires. Il n’ajoute ni modèle, ni démon et ne relance jamais un calcul.

Les racines autorisées sont configurées dans `ATELIER_ROUTINES_ROOTS`, une liste JSON ; le défaut est le dossier parent de l’installation. Le registre privé est sous `~/.local/share/atelier-routines`, le suivi dans le dossier voisin `atelier-routines-executions`. La CLI et le MCP partagent le catalogue et le moteur :

```sh
rtk proxy python scripts/routines.py run git.status --params '{"project":"/absolute/project"}'
rtk proxy python scripts/routines.py run git.status --params '{"project":"/absolute/project"}' --request-id review-branch-1
```

Les anciennes commandes `match`, `list`, `show`, `context`, `tree`, `outline` et les gardes restent disponibles. `run` sort avec 0 uniquement pour `ok` ; les résultats partiels, guidés, incertains ou échoués ne passent pas pour une validation.

`code.find` crée ou rafraîchit à la première recherche l'annuaire privé `atelier-routines-code` pour chaque utilisateur ; `code.index` force son rafraîchissement. Il référence les fichiers source et les déclarations repérées sous les racines autorisées, sans copier le contenu des sources ni modifier les sources scannées. Les chemins protégés, liens, dépendances et `CLAUDE.md` sont exclus. Les résultats portent une empreinte de version et `qa=not_assessed` : les tests et droits de réutilisation doivent être vérifiés pour le projet cible. Les langues hors extensions reconnues et les sources de plus de 2 Mio n'ont pas de recherche de contenu complète.

### Savoir de R&D entre projets

Avant une nouvelle recherche, `knowledge.find` retrouve par thème les conclusions consignées dans les projets autorisés. Après une expérience, `knowledge.record` conserve le thème, la question, les conditions d'application, la conclusion et le résultat **déclaré** (`failed`, `inconclusive`, `promising`, `worked` ou `invalidated`). `evidence_file` peut désigner un petit rapport local relatif au projet ; seul son chemin et son SHA256 sont conservés. Une recherche recontrôle l'empreinte et indique `evidence_current=false` si la preuve a changé ou disparu. `supersedes` relie une correction à l'identifiant précédent et retire l'ancienne conclusion des recherches ordinaires. Deux enregistrements identiques sont idempotents.

Le registre reste privé et propre à chaque utilisateur, sous les racines qu'il a configurées ; il ne copie ni données, ni poids, ni journaux. Les anciennes R&D ne peuvent pas être reconstituées automatiquement avec fiabilité : les consigner après examen de leurs preuves. Une fiche sans `evidence_file` est une piste non vérifiée. Pour le machine learning, noter dans les conditions les versions de données, le découpage validation/test, le modèle, les ressources et le critère d'arrêt ; rechercher les échecs antérieurs avant de décider d'un lot d'entraînement cohérent. Le registre n'autorise aucun entraînement et ne garantit pas qu'un essai unique suffira.

## Vérification

La suite teste de vrais transports MCP, les recettes, l’interruption, les doublons, les assertions, les références périmées, l’invalidation et les preuves manquantes. Les services HTTP nécessaires sont démarrés dans les fixtures et leurs assertions s’exécutent réellement. `scripts/verify.py` mesure trois scénarios identiques avec les anciennes commandes, puis le MCP, en séparant réussite, échec et réutilisation vérifiée. Les appels outil rejoués, octets et durées sont distincts des commandes internes ; aucune inférence, aucun token réel ou pourcentage d’économie n’est inventé. Le regroupement peut réduire des appels et le volume ; les petits retours peuvent grossir.

Les contrôles portables se lancent sur Atelier VPS avec `arch=any` et un pic complet de 0,8 Gio. La CI macOS vérifie les branches natives et réutilise les gardes canoniques ; les actions, outils et dépendances sont épinglés. La CI conserve les preuves et bloque l’intégration sur échec. `scripts/native_cycle_check.py` vérifie sur le Mac l’entrée personnelle exacte et un vrai cycle VPS, avec une collecte interrompue puis reprise sans nouveau calcul ; il nécessite les clients locaux et son propre espace de recette gardé.

Python est conservé : les traces disponibles ne démontrent pas un problème qui exige une réécriture en Rust. Les limites et le modèle de confiance sont détaillés dans [SECURITY.md](SECURITY.md).

## Collecte descriptive

La collecte sert à décider si des recettes et règles suffisent avant d’envisager un modèle. Aucun modèle ni entraînement n’est ajouté. Les appels MCP et CLI enregistrent début, fin, refus, durée et métadonnées du reçu. Cinq hooks silencieux ajoutent la phrase de demande, les outils observables et la collecte des compteurs lors des demandes, outils, fins et interruptions de tours, dans les racines déjà configurées. Les définitions nouvelles ou modifiées doivent être approuvées dans `/hooks`. La collecte de tokens n’exige pas un nouvel outil MCP.

### Compaction assistée par d1

L’installateur ajoute trois hooks. `PreCompact` soumet jusqu’à quatre reçus éligibles du chat au modèle d1 local avant un compactage manuel ou automatique. Il conserve les références en cas de doute ou si le modèle est indisponible. Les échecs, validations et exécutions en cours sont prioritaires. Après `PostCompact`, `SessionStart` transmet une fois la note de reprise. `executions.inspect` permet de retrouver les preuves.

Le hook utilise les métadonnées des reçus dans le journal local. Codex continue de produire son résumé natif. La commande `atelier-routines usage` compte ces sélections sous `local-compaction`. [Hooks Codex](https://learn.chatgpt.com/docs/hooks#precompact).

Les données restent dans `~/.local/share/atelier-routines-usage/events.sqlite3`, dossier 0700 et fichier 0600, avec un plafond SQLite de 64 Mio et un pic de journal temporaire pouvant approcher 128 Mio. `ATELIER_ROUTINES_STORE` déplace aussi ce store dans un dossier frère `-usage`. Aucune purge, aucun envoi de données et aucun processus de surveillance. `ATELIER_ROUTINES_USAGE=off` désactive les écritures pour le processus concerné ; les tests ordinaires les désactivent, les tests de collecte utilisent un store isolé.

La phrase conservée est filtrée puis limitée à 280 caractères. Les paramètres et identifiants de session sont hachés avec une clé locale ; les commandes, contenus de fichiers et réponses brutes ne sont pas sauvegardés. Le filtrage de texte libre ne garantit pas de reconnaître tout secret : ne pas mettre de données sensibles dans une consigne de tâche. Les charges de hook trop grandes sont signalées comme perdues. Une écriture refusée est signalée sur stderr sans masquer le résultat de l’opération ni la relancer.

```sh
rtk proxy atelier-routines usage
rtk proxy atelier-routines usage --limit 10
rtk proxy atelier-routines usage --date 2026-11-08
```

Le bilan distingue les sources, les fins d’outils observées, les débuts sans fin, les paramètres répétés dans un tour, les séquences récurrentes et les embranchements. L’analyse des séquences porte sur les 2 000 dernières fins d’outils des hooks et signale sa troncature. MCP, CLI et hooks peuvent observer le même appel : ne pas additionner leurs compteurs comme des actions indépendantes. Un tour de demande n’est pas forcément une tâche métier complète ; sans identifiant de session/appel exposé, la corrélation reste absente. `write_stdin` n’est pas garanti dans `PreToolUse`.

Les vrais compteurs de Codex sont extraits des événements numériques `token_count` de son journal local : entrées, entrées en cache, écritures de cache si exposées, sorties, sous-compteur de raisonnement et total. Le cache fait partie des entrées ; le raisonnement fait partie des sorties. Ils ne sont jamais ajoutés une seconde fois au total. `uncached_input_tokens` vaut entrées moins cache. Ce sont des compteurs déclarés par le client, pas une estimation par caractères ni une facture.

Le bilan contient les totaux par tour de tâche et par étape LLM, ainsi que les étapes et tâches les plus consommatrices. Une tâche correspond ici à un `turn_id` Codex, pas nécessairement à tout l’objectif métier. Une étape est une mise à jour positive des compteurs ; lorsque le delta correspond à la dernière inférence déclarée, elle est marquée `single_inference`. Un intervalle regroupant des appels est signalé `aggregate_interval`, sans inventer une répartition. Plusieurs outils d’une même inférence partagent un seul coût ; le détail ne répartit pas artificiellement ce coût entre eux. Les lots `exec` restent des lots, sans attribuer des tokens à leurs commandes internes.

Le classement utilise entrées hors cache + sorties, sans pondération monétaire ; tous les sous-compteurs sont conservés. Le résumé affiche trois tâches et trois étapes par défaut, jusqu’à vingt avec `--limit`, et indique sa troncature ; toutes les mesures restent dans SQLite. Une fermeture de tour n’est pas une validation de l’objectif utilisateur.

La lecture est incrémentale et dédupliquée, limitée à 8 Mio par passage, avec lignes bornées à 1 Mio. Seuls les journaux `.jsonl` réguliers appartenant à l’utilisateur sous `$CODEX_HOME/sessions` sont admissibles, sans liens symboliques ou physiques. Les messages, arguments, sorties d’outils et textes de raisonnement ne sont pas conservés par cet adaptateur ; seuls les compteurs, noms d’outils et identités pseudonymisées sont utilisés. Les événements en double, lignes partielles et copies rejouées ne sont pas additionnés. Un journal remplacé, un compteur révisé, une catégorie incohérente ou une attribution manquante rendent la couverture partielle ou indisponible, jamais nulle par supposition.

Les hooks utilisent le chemin de journal exposé par Codex. La commande `usage` synchronise aussi le journal du chat courant lorsqu’un `CODEX_THREAD_ID` UUIDv7 permet de le retrouver sur sa date de création, sans parcourir tous les historiques. Les journaux des autres chats sont actualisés par leurs hooks ; aucun démon ni interrogation périodique du modèle n’est ajouté. La fraîcheur, les lacunes et les sources observées sont indiquées : le bilan ne prétend pas mesurer tout le compte ou tous les clients LLM. L’adaptateur correspond au format local de Codex vérifié lors de l’installation ; un changement de format doit être qualifié, sans déduire de tokens absents. La [documentation App Server](https://learn.chatgpt.com/docs/app-server) décrit également les notifications officielles de compteurs pour les clients qui les consomment directement.

Les répétitions sont des pistes pour des recettes, les embranchements des cas à examiner ; aucun de ces nombres ne justifie seul un réseau neuronal. La [spécification du moteur décisionnel](SPEC_DECISION_ENGINE.md) décrit la cible ultérieure et son protocole ; seule cette collecte descriptive est implémentée.
